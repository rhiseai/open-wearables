#!/bin/bash
set -euo pipefail

scripts/start/install_extensions.sh

# Run this service at fixed capacity, independently of the ordinary worker fleet.
# Redis disable-prefetch keeps waiting tasks in the broker until a slot is free.
exec uv run celery -A app.main:celery_app worker --loglevel=info --pool=threads \
  --concurrency="${CELERY_HISTORICAL_CONCURRENCY:-2}" --disable-prefetch --prefetch-multiplier=1 \
  -Q historical_sdk,historical_vendor -n history@%h
