#!/usr/bin/env bash
# Staging credentials come from AWS Secrets Manager, and only from there.
#
# The staging host reads `lucie-staging/ow/*` with its own instance role
# (RHISE-3839). Before that, the deploy workflow built an env file out of
# GitHub Actions secrets and shipped it as one base64 blob — which GitHub
# cannot mask, because it masks by literal value, and which `ssm send-command`
# keeps in command history in the same decodable form.
#
# Nothing enforces that but this check: adding `${{ secrets.FOO }}` back to the
# staging workflow would look entirely ordinary in review.

set -euo pipefail

readonly staging_workflow=.github/workflows/deploy-staging.yml
readonly committed_env=deploy/staging/ow.env.base

status=0

if grep -n 'secrets\.' "$staging_workflow"; then
  printf '%s must not read GitHub Actions secrets: the host fetches credentials from Secrets Manager\n' \
    "$staging_workflow" >&2
  status=1
fi

# A value assigned in the committed file is a value in git history.
if grep -nE '^[A-Z0-9_]*(SECRET|PASSWORD|API_KEY|TOKEN|DSN)[A-Z0-9_]*=.+' "$committed_env"; then
  printf '%s is committed and must hold no credentials — add the entry to the table in render-env.sh instead\n' \
    "$committed_env" >&2
  status=1
fi

exit "$status"
