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

echo "Starting I/O worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads -Q default,sdk_sync,garmin_sync,webhook_sync,webhook_outgoing -n io@%h &
worker_pids+=("$!")

echo "Starting CPU worker..."
uv run celery -A app.main:celery_app worker --loglevel=info --pool=prefork --concurrency=2 -Q xml_sync -n cpu@%h &
worker_pids+=("$!")

# Both workers are required for a healthy container. If either exits (including
# an OOM kill), stop the sibling and let the container exit so ECS/Docker can
# replace it instead of leaving half the queues without a consumer.
set +e
wait_for_worker_exit
worker_status=$?
set -e

echo "A required Celery worker exited with status ${worker_status}; stopping the container." >&2
stop_workers
exit "$worker_status"
