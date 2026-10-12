#!/bin/bash
set -euo pipefail
set -x

worker_pids=()

stop_workers() {
  trap - INT TERM
  if ((${#worker_pids[@]} > 0)); then
    kill -TERM "${worker_pids[@]}" 2>/dev/null || true
    wait "${worker_pids[@]}" 2>/dev/null || true
  fi
}

wait_for_worker_exit() {
  local worker_pid
  local worker_status
  while true; do
    for worker_pid in "${worker_pids[@]}"; do
      if ! kill -0 "$worker_pid" 2>/dev/null; then
        wait "$worker_pid"
        worker_status=$?
        return "$worker_status"
      fi
    done
    sleep 1
  done
}

trap 'stop_workers; exit 130' INT
trap 'stop_workers; exit 143' TERM

scripts/start/install_extensions.sh

# Thread pools: their tasks wait on Postgres, providers and Svix, not on CPU. Without
# --concurrency Celery uses the CPU count, which is 2 on a small container.
io_concurrency="${CELERY_IO_CONCURRENCY:-16}"
webhook_priority_concurrency="${CELERY_WEBHOOK_PRIORITY_CONCURRENCY:-4}"

echo "Starting I/O worker..."
# Also lists webhook_priority, as a second consumer; the bulk webhook_outgoing queue lives here only.
uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads --concurrency="$io_concurrency" -Q webhook_priority,default,sdk_sync,garmin_sync,webhook_sync,webhook_outgoing -n io@%h &
worker_pids+=("$!")

echo "Starting webhook priority worker..."
# A worker of its own: Celery consumes a worker's queues round-robin, so a backlog on
# webhook_outgoing would otherwise delay connection/sleep/workout/sync.completed events.
uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads --concurrency="$webhook_priority_concurrency" -Q webhook_priority -n webhook@%h &
worker_pids+=("$!")

echo "Starting CPU worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=prefork --concurrency=2 -Q xml_sync -n cpu@%h &
worker_pids+=("$!")

# Every worker is required for a healthy container. If one exits (including
# an OOM kill), stop the siblings and let the container exit so ECS/Docker can
# replace it instead of leaving half the queues without a consumer.
set +e
wait_for_worker_exit
worker_status=$?
set -e

echo "A required Celery worker exited with status ${worker_status}; stopping the container." >&2
stop_workers
exit "$worker_status"
