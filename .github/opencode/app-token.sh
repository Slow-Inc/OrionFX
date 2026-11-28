#!/usr/bin/env bash
# Print an OpenCode GitHub App installation token for this repo, so trusted
# posting steps write as the OpenCode app bot instead of github-actions[bot].
#
# The job's OIDC token (audience "opencode-github-action", which differs from
# the Console model sign-in) is exchanged at api.opencode.ai, the same flow
# that dceoy/opencode-action and `opencode github run` use. The endpoint
# scopes the token to this one repository, but it takes no parameters, so it
# cannot narrow permissions: the token carries the App installation's full
# permissions (including contents: write). Callers must mask it
# (echo "::add-mask::..."). Only trusted posting steps hold it, never the
# model. Callers revoke it with `gh api --method DELETE installation/token`.
set -euo pipefail
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ]; then
  echo "::error::No OIDC token available for the OpenCode App exchange (needs id-token: write)." >&2
  exit 1
fi
oidc=$(curl -sSf --oauth2-bearer "$ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=opencode-github-action" | jq -r .value) || {
  echo "::error::Could not mint the OIDC token for the OpenCode App exchange." >&2
  exit 1
}
response=$(curl -sS -X POST -A opencode-action -H "Authorization: Bearer $oidc" \
  -w '\n%{http_code}' https://api.opencode.ai/exchange_github_app_token) || {
  echo "::error::OpenCode App token exchange request failed (network)." >&2
  exit 1
}
status=${response##*$'\n'}
token=$(printf '%s' "${response%$'\n'*}" | jq -r '.token // empty' 2> /dev/null || true)
if [ "$status" != "200" ] || [ -z "$token" ]; then
  error=$(printf '%s' "${response%$'\n'*}" | jq -r '.error // empty' 2> /dev/null || true)
  echo "::error::OpenCode App token exchange failed (HTTP $status): ${error:-no token returned}." >&2
  exit 1
fi
printf '%s' "$token"
