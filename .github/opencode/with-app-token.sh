#!/usr/bin/env bash
# Run one command with GH_TOKEN set to a fresh OpenCode App installation token
# (see app-token.sh), then revoke the token, whatever the command does.
#
# Exit status: 3 = the token was not scoped to exactly this repo (security),
# 4 = no token could be minted (OpenCode unavailable), otherwise the command's.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
rc=0
token=$(bash "$here/app-token.sh") || rc=$?
if [ "$rc" -eq 3 ]; then
  exit 3
elif [ "$rc" -ne 0 ]; then
  exit 4
fi
echo "::add-mask::$token"
export GH_TOKEN="$token"
unset token
trap 'gh api --method DELETE installation/token > /dev/null || true' EXIT
"$@"
