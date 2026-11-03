# Staging provider credentials

The staging host renders its runtime configuration with
`deploy/scripts/render-env.sh`. Provider credentials belong in AWS Secrets Manager
under `lucie-staging/ow/providers/` in `eusc-de-east-1`; the host's instance role
reads them on deploy and boot. Do not put credential values in GitHub Actions,
committed configuration, or SSM command parameters.

## Google Health

Provision these secrets before deploying a renderer that requires them:

| Secret name | Runtime variable |
| --- | --- |
| `lucie-staging/ow/providers/google-client-id` | `GOOGLE_CLIENT_ID` |
| `lucie-staging/ow/providers/google-client-secret` | `GOOGLE_CLIENT_SECRET` |

Staging uses the existing `Web App` OAuth client in the `Lucie`
(`lucie-465111`) Google Cloud project. Its credentials are already stored for
Lucie's Google integrations; provision the same client ID and client secret in
the two OpenWearables entries above. The project needs the Google Health API
enabled and the read scopes described in the
[Google Health integration guide](../../docs/providers/google-api-integration.mdx).
Register this exact authorized redirect URI for the current staging configuration:

```text
https://ow-staging.getlucie.ai/api/v1/oauth/google/callback
```

The `/google/` callback is intentional: `GOOGLE_LEGACY_OAUTH_PATH` defaults to
`true`, and existing clients use that compatibility path. If this setting is
changed to `false`, register `/api/v1/oauth/google_health/callback` in Google
Cloud first.

Deploying recreates the API and worker containers with the rendered values. A
missing, empty, or placeholder Google credential fails rendering before either
runtime file is replaced. After deployment, start a Google Health connection from
Lucie staging and verify that Google displays sign-in or consent without a missing
`client_id` or `redirect_uri_mismatch` error. Complete consent with a Google Health
test account to verify the callback and data sync.
