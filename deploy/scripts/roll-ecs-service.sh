#!/usr/bin/env bash
# Roll one service and verify the requested revision, including circuit-breaker rollback.
set -euo pipefail

service=${1:?service name required}
expected=${2:?task definition ARN required}
expected_count=${3:-}
: "${ECS_CLUSTER:?ECS_CLUSTER required}"

aws ecs update-service --cluster "$ECS_CLUSTER" --service "$service" \
  --task-definition "$expected" --query 'service.serviceName' --output text

deadline=$(( $(date +%s) + 1800 ))
seen_expected=false
while true; do
  snapshot=$(aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$service" --output json)
  if ! jq -e '.failures | length == 0' <<< "$snapshot" >/dev/null; then
    echo "::error::Cannot describe $service"
    exit 1
  fi
  primary=$(jq -c '.services[0].deployments[] | select(.status == "PRIMARY")' <<< "$snapshot")
  revision=$(jq -r '.taskDefinition' <<< "$primary")
  state=$(jq -r '.rolloutState' <<< "$primary")
  echo "[$service] revision=$revision rollout=$state"

  if [[ "$revision" == "$expected" ]]; then
    seen_expected=true
    if [[ "$state" == "FAILED" ]]; then
      echo "::error::$service failed to deploy $expected"
      exit 1
    fi
    if [[ "$state" == "COMPLETED" ]] && ! jq -e --arg count "$expected_count" '
      .services[0] | .desiredCount > 0 and ($count == "" or .desiredCount == ($count | tonumber))
    ' <<< "$snapshot" >/dev/null; then
      echo "::error::$service has unexpected desired capacity (expected ${expected_count:-at least one})"
      exit 1
    fi
    if [[ "$state" == "COMPLETED" ]] && jq -e '
      .services[0] | .runningCount == .desiredCount and .pendingCount == 0 and (.deployments | length) == 1
    ' <<< "$snapshot" >/dev/null; then
      exit 0
    fi
  elif [[ "$seen_expected" == true ]]; then
    echo "::error::$service rolled back from $expected to $revision"
    exit 1
  fi
  if (( $(date +%s) >= deadline )); then
    echo "::error::$service did not become ready on $expected within 30 minutes"
    exit 1
  fi
  sleep 20
done
