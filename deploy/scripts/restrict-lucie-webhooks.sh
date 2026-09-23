#!/usr/bin/env bash
# Keep Lucie's webhook endpoint subscribed to the three event types it handles.
#
# This runs on the host, at the end of a deploy, rather than in CI: it needs
# the admin credentials, and the whole point of RHISE-3839 is that credentials
# reach the host from Secrets Manager and never travel through a workflow.
#
# The API is only published under its real hostname (Traefik routes by Host and
# serves the Let's Encrypt certificate for it), and EC2 cannot reach its own
# Elastic IP from inside, so resolve that name to the loopback interface and
# let TLS verify normally against the certificate Traefik already serves.

set -euo pipefail
set +x # the login response carries a bearer token

SECRET_PREFIX="${OW_SECRET_PREFIX:-lucie-staging/ow}"
OW_DOMAIN="${OW_DOMAIN:-ow-staging.getlucie.ai}"
OW_API_BASE="https://${OW_DOMAIN}/api"
LUCIE_ENDPOINT_PATTERN='^https://[^/]*getlucie\.ai/(api/v1/)?webhooks/ow/?$'
FILTER_TYPES='["sleep.created","sleep.updated","connection.created"]'
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-eusc-de-east-1}"

log()  { printf '\033[1;36m[webhooks]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[webhooks]\033[0m %s\n' "$*" >&2; exit 1; }

curl_api() { curl --fail-with-body --silent --show-error --resolve "${OW_DOMAIN}:443:127.0.0.1" "$@"; }

admin_email=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_PREFIX/admin/email" --query SecretString --output text) ||
  die "cannot read $SECRET_PREFIX/admin/email"
admin_password=$(aws secretsmanager get-secret-value \
  --secret-id "$SECRET_PREFIX/admin/password" --query SecretString --output text) ||
  die "cannot read $SECRET_PREFIX/admin/password"

token=$(curl_api \
  --data-urlencode "username=${admin_email}" \
  --data-urlencode "password=${admin_password}" \
  "${OW_API_BASE}/v1/auth/login" | jq -er '.access_token') ||
  die "admin login failed"

endpoints=$(curl_api -H "Authorization: Bearer ${token}" \
  "${OW_API_BASE}/v1/webhooks/endpoints") || die "could not list webhook endpoints"

mapfile -t endpoint_ids < <(
  jq -r --arg pattern "$LUCIE_ENDPOINT_PATTERN" \
    '.[] | select(.url | test($pattern)) | .id' <<<"$endpoints"
)
log "found $(jq 'length' <<<"$endpoints") endpoint(s), ${#endpoint_ids[@]} for Lucie"
((${#endpoint_ids[@]})) || die "Lucie webhook endpoint was not found"

for endpoint_id in "${endpoint_ids[@]}"; do
  curl_api -X PATCH \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d "{\"filter_types\":${FILTER_TYPES}}" \
    "${OW_API_BASE}/v1/webhooks/endpoints/${endpoint_id}" |
    jq -e --argjson want "$FILTER_TYPES" '(.filter_types | sort) == ($want | sort)' >/dev/null ||
    die "endpoint ${endpoint_id} did not accept the filter list"
done
log "restricted ${#endpoint_ids[@]} Lucie webhook endpoint(s)"
