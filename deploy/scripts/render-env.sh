#!/usr/bin/env bash
# Render the staging runtime environment from AWS Secrets Manager.
#
# OpenWearables credentials live in exactly one place — `lucie-staging/ow/*` in
# the EU Sovereign account, next to the `lucie-prod/ow/*` family that ECS reads
# for production — and this host fetches them with its own instance role. The
# deploy workflow ships code, image tags and the public hostname; it never sees
# a credential, so nothing crosses a run log or SSM command history.
#
# Reads:
#   deploy/staging/ow.env.base  committed, non-secret backend configuration
#   /app/.env.deploy            written by the deploy workflow, non-secret
# Writes (0600, atomically, and only once every required secret resolved):
#   /app/ow.env                 env_file for app, celery-worker, beat, svix
#   /app/.env                   compose interpolation, adds the DB password
#
# Both outputs are rebuilt from scratch on every deploy and on every boot
# (ow-render-env.service), so a rotated secret needs nothing but a restart —
# and a render that fails leaves the previous, working files in place.
#
# This script never prints a secret value: only names, and AWS's own error text.

set -euo pipefail
set +x # never trace this script — the values would land in CloudWatch

APP_DIR="${APP_DIR:-/app}"
SECRET_PREFIX="${OW_SECRET_PREFIX:-lucie-staging/ow}"
BASE_ENV="${OW_ENV_BASE:-$APP_DIR/deploy/staging/ow.env.base}"
CI_ENV="${OW_CI_ENV:-$APP_DIR/.env.deploy}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-eusc-de-east-1}"

log()  { printf '\033[1;36m[render-env]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[render-env]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[render-env]\033[0m %s\n' "$*" >&2; exit 1; }

# env var                         secret name under $SECRET_PREFIX             requirement
#
# `required` fails the deploy when the entry is missing or empty. That is the
# point of the table: an unset GitHub secret used to expand to an empty string
# and break a provider silently, days later, in a way only a user noticed.
# `optional` is for credentials that genuinely are not provisioned yet — no
# entry, an empty one, or one still holding the REPLACE_ME skeleton: Garmin
# has never had a staging application (its GitHub secrets were empty, so the
# old workflow wrote GARMIN_CLIENT_ID= into ow.env), Strava's is pending, and
# Sentry is switched off on staging.
read -r -d '' SECRET_TABLE <<'TABLE' || true
SECRET_KEY                        core/secret-key                            required
SVIX_JWT_SECRET                   core/svix-jwt-secret                       required
OPEN_WEARABLES_API_KEY            core/api-key                               required
ADMIN_EMAIL                       admin/email                                required
ADMIN_PASSWORD                    admin/password                             required
SENTRY_DSN                        core/sentry-dsn                            optional
RESEND_API_KEY                    core/resend-api-key                        optional
GARMIN_CLIENT_ID                  providers/garmin-client-id                 optional
GARMIN_CLIENT_SECRET              providers/garmin-client-secret             optional
OURA_CLIENT_ID                    providers/oura-client-id                   required
OURA_CLIENT_SECRET                providers/oura-client-secret               required
OURA_WEBHOOK_VERIFICATION_TOKEN   providers/oura-webhook-verification-token   required
WHOOP_CLIENT_ID                   providers/whoop-client-id                  required
WHOOP_CLIENT_SECRET               providers/whoop-client-secret              required
SUUNTO_CLIENT_ID                  providers/suunto-client-id                 required
SUUNTO_CLIENT_SECRET              providers/suunto-client-secret             required
POLAR_CLIENT_ID                   providers/polar-client-id                  required
POLAR_CLIENT_SECRET               providers/polar-client-secret              required
STRAVA_CLIENT_ID                  providers/strava-client-id                 optional
STRAVA_CLIENT_SECRET              providers/strava-client-secret             optional
TABLE

[[ -r "$BASE_ENV" ]] || die "missing non-secret base file: $BASE_ENV"
[[ -r "$CI_ENV" ]] || die "missing deploy-provided file: $CI_ENV (has a deploy ever run?)"

aws_error=$(mktemp)
trap 'rm -f "$aws_error"' EXIT

# Print a secret value on stdout, or return non-zero with AWS's message in
# $aws_error. Only the secret NAME is ever safe to log.
fetch_secret() {
  aws secretsmanager get-secret-value \
    --secret-id "$SECRET_PREFIX/$1" \
    --query SecretString --output text 2>"$aws_error"
}

# Reject a value we can obviously not use, before it reaches a container.
# $1 = env var name, $2 = secret path, $3 = value.
validate_secret() {
  case "$3" in
    *$'\n'*)
      die "$1: $SECRET_PREFIX/$2 contains a newline; an env file cannot carry it"
      ;;
  esac
}

# An entry that exists but still holds the skeleton value. The prod family
# keeps entries like this for credentials that are provisioned but not yet
# issued, so staging will too: for an `optional` one that means "not
# provisioned", exactly as a missing entry does. Writing it through would put
# the literal REPLACE_ME into a client_id and break the provider quietly.
is_placeholder() {
  [[ "$1" == *REPLACE_ME* ]]
}

umask 077
tmp_ow=$(mktemp "$APP_DIR/.ow.env.XXXXXX")
tmp_env=$(mktemp "$APP_DIR/.env.XXXXXX")
trap 'rm -f "$aws_error" "$tmp_ow" "$tmp_env"' EXIT

{
  printf '# Rendered by deploy/scripts/render-env.sh at %s — do not edit.\n' "$(date -u +%FT%TZ)"
  printf '# Settings below come from deploy/staging/ow.env.base; credentials\n'
  printf '# come from AWS Secrets Manager under %s/.\n\n' "$SECRET_PREFIX"
  cat "$BASE_ENV"
  printf '\n# --- credentials, fetched from AWS Secrets Manager ---\n'
} >"$tmp_ow"

resolved=0
skipped=()
while read -r var path requirement; do
  [[ -z "${var:-}" || "$var" == \#* ]] && continue

  if ! value=$(fetch_secret "$path"); then
    if [[ "$requirement" == "required" ]]; then
      die "$var: cannot read $SECRET_PREFIX/$path — $(tr -d '\n' <"$aws_error")"
    fi
    skipped+=("$var")
    continue
  fi

  if [[ -z "$value" || "$value" == "None" ]]; then
    if [[ "$requirement" == "required" ]]; then
      die "$var: $SECRET_PREFIX/$path is empty"
    fi
    skipped+=("$var")
    continue
  fi

  if is_placeholder "$value"; then
    if [[ "$requirement" == "required" ]]; then
      die "$var: $SECRET_PREFIX/$path still holds a placeholder — set the real value first"
    fi
    skipped+=("$var")
    continue
  fi

  validate_secret "$var" "$path" "$value"
  printf '%s=%s\n' "$var" "$value" >>"$tmp_ow"
  resolved=$((resolved + 1))
done <<<"$SECRET_TABLE"

# The compose file interpolates OW_DB_PASSWORD into POSTGRES_PASSWORD and the
# backend's DB_PASSWORD. svix parses a full postgres:// URL instead, so any
# /, + , @ or = in the password has to be percent-encoded there or it fails
# with InvalidPort/InvalidHost.
db_password=$(fetch_secret db/password) ||
  die "cannot read $SECRET_PREFIX/db/password — $(tr -d '\n' <"$aws_error")"
[[ -n "$db_password" && "$db_password" != "None" ]] ||
  die "$SECRET_PREFIX/db/password is empty"
if is_placeholder "$db_password"; then
  die "OW_DB_PASSWORD: $SECRET_PREFIX/db/password still holds a placeholder — set the real value first"
fi
validate_secret OW_DB_PASSWORD db/password "$db_password"
db_password_encoded=$(
  OW_DB_PASSWORD_RAW="$db_password" python3 -c \
    'import os, urllib.parse; print(urllib.parse.quote(os.environ["OW_DB_PASSWORD_RAW"], safe=""))'
)

{
  printf '# Rendered by deploy/scripts/render-env.sh at %s — do not edit.\n' "$(date -u +%FT%TZ)"
  printf '# Everything above the credential marker was shipped by the deploy\n'
  printf '# workflow as %s.\n\n' "$CI_ENV"
  cat "$CI_ENV"
  printf '\n# --- credentials, fetched from AWS Secrets Manager ---\n'
  printf 'OW_DB_PASSWORD=%s\n' "$db_password"
  printf 'OW_DB_PASSWORD_ENCODED=%s\n' "$db_password_encoded"
} >"$tmp_env"

chmod 0600 "$tmp_ow" "$tmp_env"
mv "$tmp_ow" "$APP_DIR/ow.env"
mv "$tmp_env" "$APP_DIR/.env"
rm -f "$aws_error"
trap - EXIT

log "Rendered $APP_DIR/ow.env with $((resolved + 1)) credentials from $SECRET_PREFIX/"
if ((${#skipped[@]})); then
  warn "not provisioned, left unset: ${skipped[*]}"
fi
