#!/usr/bin/env bash

set -euo pipefail

readonly delivery_paths=(
  .github/workflows/deploy-prod.yml
  .github/workflows/deploy-staging.yml
  deploy
)
readonly retired_references=(
  "031244""176128"
  "us-""east-1"
  "arn:aws:""iam::"
  "sts.""amazonaws.com"
  "dkr.ecr.""amazonaws.com"
)

status=0
for reference in "${retired_references[@]}"; do
  if grep -RFn --exclude="$(basename "$0")" -- "$reference" "${delivery_paths[@]}"; then
    printf 'Retired Commercial AWS reference found: %s\n' "$reference" >&2
    status=1
  fi
done

if [[ -e .github/workflows/eusc-sync.yml ]]; then
  printf 'Obsolete cross-partition sync workflow still exists\n' >&2
  status=1
fi

exit "$status"
