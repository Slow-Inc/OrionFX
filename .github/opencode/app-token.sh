#!/usr/bin/env bash
# Print an OpenCode GitHub App installation token for this repo, so trusted
# posting steps write as the OpenCode app bot instead of github-actions[bot].
#
# The job's OIDC token (audience "opencode-github-action", which differs from
# the Console model sign-in) is exchanged at api.opencode.ai, the same flow
# that dceoy/opencode-action and `opencode github run` use. Callers must mask
# it (echo "::add-mask::..."). It is only ever held by trusted posting steps,
# never by the model. Callers revoke it with `gh api --method DELETE installation/token` when done.
set -euo pipefail
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
  echo "::error::No OIDC token available for the OpenCode App exchange (needs id-token: write)." >&2
  exit 1
fi
oidc=$(curl -sSf --oauth2-bearer "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=opencode-github-action" | jq -r .value)
token=$(curl -sSf -X POST -A opencode-action -H "Authorization: Bearer $oidc" \
  https://api.opencode.ai/exchange_github_app_token | jq -r .token)
if [ -z "$token" ] || [ "$token" = "null" ]; then
  echo "::error::OpenCode App token exchange returned no token (is the OpenCode app installed on this repo?)." >&2
  exit 1
fi
printf '%s' "$token"
