#!/usr/bin/env bash
# Tests for .claude/hooks/orionfx-opencode-merge-gate (issue #149).
# Runs the hook against a fake `gh` on PATH that replays JSON fixtures.
# Portable: bash + perl (JSON::PP), like the hook. Run: bash tests/hooks/test-orionfx-opencode-merge-gate.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$ROOT/.claude/hooks/orionfx-opencode-merge-gate"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/proj/.claude"
export FIX="$TMP/fix"

# Fake gh: log the call, fail if fail.<kind> exists, else print <kind>.json.
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$FIX/calls.log"
case "$1 $2" in
  "pr view") k=view ;;
  "api graphql") k=threads ;;
  *) case "$*" in
       */reviews*) k=reviews ;;
       */files*) k=files ;;
       */comments*) k=comments ;;
       *) echo "fake gh: unexpected call: $*" >&2; exit 99 ;;
     esac ;;
esac
if [ -f "$FIX/fail.$k" ]; then cat "$FIX/fail.$k" >&2; exit 1; fi
cat "$FIX/$k.json"
STUB
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"
export CLAUDE_PROJECT_DIR="$TMP/proj"

HEAD=1111111111111111111111111111111111111111
OLD=2222222222222222222222222222222222222222
URL=https://github.com/Slow-Inc/OrionFX/pull/7

set_cfg() {  # $1 = true|false, $2 = bot login
  sed -e "s/\"requireOpenCodeReview\": *[a-z]*/\"requireOpenCodeReview\": $1/" \
      -e "s/\"opencodeBotLogin\": *\"[^\"]*\"/\"opencodeBotLogin\": \"${2:-opencode-agent[bot]}\"/" \
      "$ROOT/.claude/t4.json" > "$TMP/proj/.claude/t4.json"
}

# Fixture builders. reset() gives a PR at HEAD touching docs only, with a resolved
# OpenCode review at HEAD and no comments: the passing baseline.
reset() {
  rm -rf "$FIX"; mkdir -p "$FIX"
  set_cfg true
  files '[{"filename":"docs/x.md"}]'
  reviews "[{\"id\":101,\"user\":{\"login\":\"opencode-agent[bot]\",\"type\":\"Bot\"},\"commit_id\":\"$HEAD\",\"state\":\"COMMENTED\"}]"
  threads '{"isResolved":true,"path":"docs/x.md","line":3,"comments":{"nodes":[{"pullRequestReview":{"databaseId":101}}]}}'
  comments '[]'
}
files() {
  printf '%s' "$1" > "$FIX/files.json"
  local n; n="$(perl -MJSON::PP -0777 -ne 'print scalar @{decode_json($_)}' "$FIX/files.json")"
  printf '{"number":7,"headRefOid":"%s","url":"%s","changedFiles":%s}' "$HEAD" "$URL" "${2:-$n}" > "$FIX/view.json"
}
reviews()  { printf '%s' "$1" > "$FIX/reviews.json"; }
threads()  { printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":%s},"nodes":[%s]}}}}}' "${2:-false}" "$1" > "$FIX/threads.json"; }
comments() { printf '%s' "$1" > "$FIX/comments.json"; }
comment()  {  # $1 = body, $2 = association (default OWNER), $3 = login, $4 = type
  perl -MJSON::PP -e 'print JSON::PP->new->canonical->encode({body=>$ARGV[0],author_association=>$ARGV[1],user=>{login=>$ARGV[2],type=>$ARGV[3]}})' \
    "$1" "${2:-OWNER}" "${3:-top00}" "${4:-User}"
}
IND="<!-- t4:independent-review head=$HEAD -->
Subagent review: 2 findings, both fixed in abc123."
FB="<!-- t4:opencode-fallback head=$HEAD reason=\"model 429 FreeUsageLimitError\" -->"

pass=0; fail=0
# run <expect allow|block> <name> <command> [stderr-regex] [tool_name]
run() {
  local expect="$1" name="$2" cmd="$3" pat="${4:-}" tool="${5:-Bash}" input rc got
  err=""  # global: no_escalation reads the last message
  input="$(perl -MJSON::PP -e 'print encode_json({tool_name=>$ARGV[0],tool_input=>{command=>$ARGV[1]}})' "$tool" "$cmd")"
  err="$(printf '%s' "$input" | bash "$HOOK" 2>&1 >/dev/null)"; rc=$?
  case "$rc" in 0) got=allow ;; 2) got=block ;; *) got="exit-$rc" ;; esac
  if [ "$got" = "$expect" ] && { [ -z "$pat" ] || printf '%s' "$err" | grep -Eq -- "$pat"; }; then
    pass=$((pass + 1)); echo "ok   - $name"
  else
    fail=$((fail + 1)); echo "FAIL - $name (expected $expect${pat:+ matching /$pat/}, got $got)"
    printf '%s\n' "$err" | sed 's/^/       /'
  fi
}
called() {  # $1 = name, $2 = regex the calls log must match ("" = gh must not be called)
  if { [ -z "$2" ] && [ ! -s "$FIX/calls.log" ]; } || { [ -n "$2" ] && grep -Eq -- "$2" "$FIX/calls.log" 2>/dev/null; }; then
    pass=$((pass + 1)); echo "ok   - $1"
  else
    fail=$((fail + 1)); echo "FAIL - $1 (calls: $(tr '\n' ';' < "$FIX/calls.log" 2>/dev/null))"
  fi
}

M="gh pr merge 7 --squash"

# --- scope ------------------------------------------------------------------------
reset; run allow "non-merge command is not touched" "git status"; called "non-merge: gh never called" ""
reset; set_cfg false; reviews '[]'; run allow "opt-in off allows" "$M"; called "opt-in off: gh never called" ""
reset; run allow "merge mentioned inside a quoted body" 'gh issue comment 5 --body "then (gh pr merge 5) later"'; called "quoted mention: gh never called" ""
reset; run allow "merge mentioned inside a heredoc" "cat > f <<'EOF'
gh pr merge 5 --squash
EOF"
called "heredoc mention: gh never called" ""

# --- normal pass ----------------------------------------------------------------
reset; run allow "review at head, all threads resolved" "$M"
reset; reviews '[]'; run block "no review" "$M" "no review by opencode-agent\[bot\] at the head SHA"
reset; reviews "[{\"id\":101,\"user\":{\"login\":\"opencode-agent[bot]\",\"type\":\"Bot\"},\"commit_id\":\"$OLD\"}]"
run block "review only on an old SHA" "$M" "no review by"
reset; reviews "[{\"id\":101,\"user\":{\"login\":\"github-actions[bot]\",\"type\":\"Bot\"},\"commit_id\":\"$HEAD\"}]"
run block "review at head by a different bot" "$M" "no review by"
reset; reviews "[{\"id\":101,\"user\":{\"login\":\"opencode-agent\",\"type\":\"User\"},\"commit_id\":\"$HEAD\"}]"
run block "user account named like the bot does not count" "$M" "no review by"
reset; threads '{"isResolved":false,"path":"docs/x.md","line":3,"comments":{"nodes":[{"pullRequestReview":{"databaseId":101}}]}}'
run block "unresolved OpenCode thread" "$M" "1 OpenCode review thread.*not resolved: docs/x.md:3"
reset; threads '{"isResolved":false,"path":"a","line":1,"comments":{"nodes":[{"pullRequestReview":{"databaseId":999}}]}}'
run allow "unresolved thread from another review does not count" "$M"
reset; threads '' true; run block "more than 100 threads" "$M" "more than 100 review threads"
reset; set_cfg true opencode-agent; run allow "bot login configured without [bot]" "$M"

# --- risk paths -----------------------------------------------------------------
reset; files '[{"filename":"backend/app/risk/veto.py"}]'
run block "risk path without independent record" "$M" "risk paths \(backend/app/risk/veto.py\)"
reset; files '[{"filename":"backend/app/risk/veto.py"}]'; comments "[$(comment "$IND")]"
run allow "risk path with independent record" "$M"
reset; files '[{"filename":"frontend/.env.local"}]'; run block "nested .env file is a risk path" "$M" "risk paths"
reset; files '[{"filename":"docs/moved.md","previous_filename":".githooks/pre-push"}]'
run block "rename out of a risk path still counts" "$M" "risk paths \(.githooks/pre-push\)"
reset; files '[{"filename":"backend/app/riskless.py"}]'; run allow "near-miss path is not a risk path" "$M"
reset; files '[{"filename":".claude/hooks/x"}]'; comments "[$(comment "$IND" NONE stranger)]"
run block "record from an untrusted commenter" "$M" "risk paths"
reset; files '[{"filename":".claude/hooks/x"}]'; comments "[$(comment "$IND" OWNER opencode-agent[bot] Bot)]"
run block "record from a bot" "$M" "risk paths"
reset; files '[{"filename":".claude/hooks/x"}]'; comments "[$(comment "${IND//$HEAD/$OLD}")]"
run block "record at an old SHA" "$M" "risk paths"
reset; files '[{"filename":".claude/hooks/x"}]'; comments "[$(comment "<!-- t4:independent-review head=$HEAD -->")]"
run block "record with no written summary" "$M" "risk paths"
reset; files '[{"filename":"docs/a.md"}]' 3; run block "file list shorter than changedFiles" "$M" "listed 1 of 3 changed files"

# --- fallback -------------------------------------------------------------------
reset; reviews '[]'; comments "[$(comment "$FB"),$(comment "$IND" COLLABORATOR)]"
run allow "fallback with both markers" "$M"
reset; reviews '[]'; comments "[$(comment "$FB")]"
run block "fallback without the self-review record" "$M" "t4:independent-review head=$HEAD.*\[missing\]"
reset; reviews '[]'; comments "[$(comment "<!-- t4:opencode-fallback head=$HEAD reason=\"\" -->"),$(comment "$IND")]"
run block "fallback with an empty reason" "$M" "opencode-fallback.*\[missing\]"
reset; threads '{"isResolved":false,"path":"a","line":1,"comments":{"nodes":[{"pullRequestReview":{"databaseId":101}}]}}'
comments "[$(comment "$FB"),$(comment "$IND")]"
run block "fallback does not excuse unresolved threads when the review exists" "$M" "not resolved"

# --- round cap (opencodeMaxRounds, default 5) ----------------------------------------
# rounds <n> [head-resolved]: n OpenCode reviews on distinct commits, the last one at HEAD
# (id 101) with an unresolved thread, unless $2 = resolved.
rounds() {
  local i list=""
  for ((i = 1; i < $1; i++)); do list+="{\"id\":$i,\"user\":{\"login\":\"opencode-agent[bot]\",\"type\":\"Bot\"},\"commit_id\":\"$(printf '%040d' "$i")\"},"; done
  reviews "[$list{\"id\":101,\"user\":{\"login\":\"opencode-agent[bot]\",\"type\":\"Bot\"},\"commit_id\":\"$HEAD\"}]"
  [ "${2:-}" = resolved ] && return
  threads '{"isResolved":false,"path":"a.py","line":9,"comments":{"nodes":[{"pullRequestReview":{"databaseId":101}}]}}'
}
no_escalation() {  # $1 = name; the last block message must not say STOP
  if printf '%s' "$err" | grep -q "STOP AND ASK"; then fail=$((fail + 1)); echo "FAIL - $1"; else pass=$((pass + 1)); echo "ok   - $1"; fi
}
reset; rounds 3; run block "below the cap: plain block" "$M" "not resolved"; no_escalation "below the cap: no escalation"
reset; rounds 5; run block "at the cap (5 rounds): plain block" "$M" "not resolved"; no_escalation "at the cap: no escalation yet"
reset; rounds 6; run block "cap exceeded: stop and ask the owner" "$M" "STOP AND ASK THE OWNER.*reviewed 6 commits.*cap of 5"
reset; rounds 6 resolved; run allow "cap exceeded but the gate passes: merge allowed" "$M"
reset; perl -0777 -i -pe 's/"opencodeMaxRounds": 5/"opencodeMaxRounds": 2/' "$TMP/proj/.claude/t4.json"; rounds 3
run block "configured cap of 2 exceeded" "$M" "cap of 2"
reset; perl -0777 -i -pe 's/"opencodeMaxRounds": 5/"opencodeMaxRounds": 0/' "$TMP/proj/.claude/t4.json"
run block "invalid opencodeMaxRounds" "$M" "opencodeMaxRounds.*positive integer"

# --- fail closed ----------------------------------------------------------------
reset; echo "HTTP 502: Bad Gateway" > "$FIX/fail.reviews"; run block "API error on reviews" "$M" "pulls/7/reviews.*fail closed"
reset; echo "no pull requests found" > "$FIX/fail.view"; run block "API error on pr view" "$M" "fail closed"
reset; reviews '[]'; echo "HTTP 403" > "$FIX/fail.comments"; run block "API error on comments" "$M" "fail closed"
reset; echo "not json" > "$FIX/reviews.json"; run block "garbled API reply" "$M" "could not parse"
reset; perl -0777 -i -pe 's/"riskPaths": \[.*?\]/"riskPaths": []/s' "$TMP/proj/.claude/t4.json"
run block "empty riskPaths while opted in" "$M" "riskPaths"
reset; run block "merge via the GitHub MCP tool" "" "GitHub MCP tool" mcp__github__merge_pull_request

# --- PR selector parsing (xeno-skills #437) ------------------------------------------
reset; run allow "flag before the number" "gh pr merge --squash 42"; called "flag before the number -> PR 42" "^pr view 42 "
reset; run allow "number inside a quoted flag value" 'gh pr merge --subject "Release 2026" 12 --squash'
called "quoted value skipped -> PR 12" "^pr view 12 "
reset; run allow "PR URL" "gh pr merge $URL --merge"; called "PR URL passed to pr view" "^pr view $URL "
reset; run allow "no selector" "gh pr merge --squash --delete-branch"; called "no selector -> current branch" "^pr view --json"
reset; run allow "--repo flag" "gh pr merge -R Slow-Inc/OrionFX 9"; called "--repo passed through" "^pr view 9 --repo Slow-Inc/OrionFX "
reset; reviews '[]'; run block "quoted absolute gh path is gated" '"/c/Program Files/GitHub CLI/gh.exe" pr merge 7 --squash' "no review by"
reset; reviews '[]'; run block "merge after cd && is gated" "cd /x && gh pr merge 7" "no review by"
reset; run block "env-prefixed merge is refused" "GH_REPO=other/repo gh pr merge 7" "environment prefix"
reset; run block "env prefix + quoted gh path is refused" 'GH_REPO=x "/usr/bin/gh" pr merge 7' "environment prefix"
reset; run block "two merges in one command" "gh pr merge 1; gh pr merge 2" "one .gh pr merge. per command"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
