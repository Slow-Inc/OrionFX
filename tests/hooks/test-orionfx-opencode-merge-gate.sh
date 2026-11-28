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
       */contents/.claude/t4.json)  # the default branch's config: remote.json, else a mirror of the local one
         if [ -f "$FIX/fail.contents" ]; then cat "$FIX/fail.contents" >&2; exit 1; fi
         src="$FIX/remote.json"; [ -f "$src" ] || src="$CLAUDE_PROJECT_DIR/.claude/t4.json"
         [ -f "$src" ] || { echo "HTTP 404: Not Found" >&2; exit 1; }
         perl -MMIME::Base64 -0777 -ne '(my $b = encode_base64($_)) =~ s/\n/\\n/g; print qq({"type":"file","encoding":"base64","content":"$b"})' "$src"
         exit 0 ;;
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
OK_BODY='OpenCode review of x (advisory)\n\n- no findings\n\nVerdict: 0 blocker, 0 major'

set_cfg() {  # $1 = true|false, $2 = bot login
  sed -e "s/\"requireOpenCodeReview\": *[a-z]*/\"requireOpenCodeReview\": $1/" \
      -e "s/\"opencodeBotLogin\": *\"[^\"]*\"/\"opencodeBotLogin\": \"${2:-opencode-agent[bot]}\"/" \
      "$ROOT/.claude/t4.json" > "$TMP/proj/.claude/t4.json"
}

# rev <id> <commit> [body] [login] [type]: one REST review object.
rev() {
  printf '{"id":%s,"user":{"login":"%s","type":"%s"},"commit_id":"%s","state":"COMMENTED","body":"%s"}' \
    "$1" "${4:-opencode-agent[bot]}" "${5:-Bot}" "$2" "${3-$OK_BODY}"
}
# thr <resolved> <outdated> <reviewId> [reply|untrusted-reply]: one GraphQL thread
# opened by the bot, optionally with a human reply.
thr() {
  local c="{\"authorAssociation\":\"NONE\",\"author\":{\"login\":\"opencode-agent\",\"__typename\":\"Bot\"},\"pullRequestReview\":{\"databaseId\":$3}}"
  case "${4:-}" in
    reply) c+=",{\"authorAssociation\":\"OWNER\",\"author\":{\"login\":\"top00\",\"__typename\":\"User\"},\"pullRequestReview\":{\"databaseId\":555}}" ;;
    untrusted-reply) c+=",{\"authorAssociation\":\"NONE\",\"author\":{\"login\":\"stranger\",\"__typename\":\"User\"},\"pullRequestReview\":{\"databaseId\":556}}" ;;
  esac
  printf '{"isResolved":%s,"isOutdated":%s,"path":"a.py","line":3,"comments":{"nodes":[%s]}}' "$1" "$2" "$c"
}

# reset(): a PR at HEAD touching docs only, with a clean OpenCode review at HEAD whose
# one thread is resolved and answered, and no comments -- the passing baseline.
reset() {
  rm -rf "$FIX"; mkdir -p "$FIX"
  set_cfg true
  files '[{"filename":"docs/x.md"}]'
  reviews "[$(rev 101 $HEAD)]"
  threads "$(thr true false 101 reply)"
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
# decide <exit code> <stdout file>: how Claude Code reads the hook's answer.
#   allow = exit 0 and empty stdout; block = exit 0 and stdout is exactly ONE JSON object
#   that is a PreToolUse deny (its reason is written to $TMP/reason). Anything else --
#   exit 2, a non-zero exit, junk or two objects on stdout -- is reported as itself, so
#   a test expecting allow or block fails. Exit 2 is NOT a block here: on Windows
#   run-hook.cmd turns it into 0 (#150 review).
decide() {
  perl -MJSON::PP -e '
    my ($rc, $f, $rf) = @ARGV;
    open my $fh, "<", $f or die; my $o = do { local $/; <$fh> };
    if ($rc != 0) { print "exit-$rc"; exit }
    if ($o eq "") { print "allow"; exit }
    my $v = eval { JSON::PP->new->decode($o) };
    my $h = ref $v eq "HASH" ? $v->{hookSpecificOutput} : undef;
    if (ref $h eq "HASH" && keys %$v == 1 && ($h->{hookEventName} // "") eq "PreToolUse"
        && ($h->{permissionDecision} // "") eq "deny" && ($h->{permissionDecisionReason} // "") =~ /\S/) {
      open my $r, ">", $rf or die; print $r $h->{permissionDecisionReason}; print "block"; exit;
    }
    print "bad-stdout";
  ' "$1" "$2" "$TMP/reason"
}
# run <expect allow|block> <name> <command> [message-regex] [tool_name]
run() {
  local expect="$1" name="$2" cmd="$3" pat="${4:-}" tool="${5:-Bash}" input rc got
  err=""  # global: no_escalation reads the last message (deny reason + stderr)
  input="$(perl -MJSON::PP -e 'print encode_json({tool_name=>$ARGV[0],tool_input=>{command=>$ARGV[1]}})' "$tool" "$cmd")"
  : > "$TMP/reason"
  printf '%s' "$input" | bash "$HOOK" >"$TMP/out" 2>"$TMP/err"; rc=$?
  got="$(decide "$rc" "$TMP/out")"
  err="$(cat "$TMP/reason" "$TMP/err")"
  if [ "$got" = "$expect" ] && { [ -z "$pat" ] || printf '%s' "$err" | grep -Eq -- "$pat"; }; then
    pass=$((pass + 1)); echo "ok   - $name"
  else
    fail=$((fail + 1)); echo "FAIL - $name (expected $expect${pat:+ matching /$pat/}, got $got)"
    { printf '%s\n' "$err"; [ "$got" = bad-stdout ] && cat "$TMP/out"; } | sed 's/^/       /'
  fi
}
called() {  # $1 = name, $2 = regex the calls log must match ("" = gh must not be called)
  if { [ -z "$2" ] && [ ! -s "$FIX/calls.log" ]; } || { [ -n "$2" ] && grep -Eq -- "$2" "$FIX/calls.log" 2>/dev/null; }; then
    pass=$((pass + 1)); echo "ok   - $1"
  else
    fail=$((fail + 1)); echo "FAIL - $1 (calls: $(tr '\n' ';' < "$FIX/calls.log" 2>/dev/null))"
  fi
}
no_escalation() {  # $1 = name; the last block message must not say STOP
  if printf '%s' "$err" | grep -q "STOP AND ASK"; then fail=$((fail + 1)); echo "FAIL - $1"; else pass=$((pass + 1)); echo "ok   - $1"; fi
}

MHC="--match-head-commit $HEAD"
M="gh pr merge 7 --squash $MHC"
UNPARSED="form the gate cannot check"

# --- scope ------------------------------------------------------------------------
reset; run allow "non-merge command is not touched" "git status"; called "non-merge: gh never called" ""
reset; run allow "git merge is not a PR merge" "git merge --ff-only origin/main"; called "git merge: gh never called" ""
reset; set_cfg false; reviews '[]'; run allow "opt-in off (local and default branch) allows" "$M"
called "opt-in off: only the default-branch config is read" "^api repos/\{owner\}/\{repo\}/contents/\.claude/t4\.json$"
if grep -qv "contents/" "$FIX/calls.log"; then fail=$((fail + 1)); echo "FAIL - opt-in off: no other gh call"; else pass=$((pass + 1)); echo "ok   - opt-in off: no other gh call"; fi
reset; set_cfg false; run allow "opt-in off: git merge never calls gh" "git merge --ff-only origin/main"; called "opt-in off, git merge: gh never called" ""
reset; set_cfg false; run allow "opt-in off: git status never calls gh" "git status"; called "opt-in off, git status: gh never called" ""
# The switch is not read only from the working tree (#150 review): the default branch's
# .claude/t4.json counts too, so a local edit or an old worktree cannot turn the gate off.
on_remote() { cp "$ROOT/.claude/t4.json" "$FIX/remote.json"; perl -0777 -i -pe "s/\"requireOpenCodeReview\": *[a-z]+/\"requireOpenCodeReview\": $1/" "$FIX/remote.json"; }
reset; set_cfg false; on_remote true; reviews '[]'; run block "local false, default branch true: gated" "$M" "no review by"
reset; rm "$TMP/proj/.claude/t4.json"; on_remote true; reviews '[]'; run block "no local t4.json, default branch true: gated" "$M" "no review by"
reset; rm "$TMP/proj/.claude/t4.json"; run allow "no local t4.json: git status is untouched" "git status"; called "no local t4.json, git status: gh never called" ""
reset; rm "$TMP/proj/.claude/t4.json"; run block "no t4.json anywhere: a merge fails closed" "$M" "contents.*fail closed"
reset; set_cfg false; echo "HTTP 502" > "$FIX/fail.contents"; run block "local off, default branch unreadable: a merge fails closed" "$M" "fail closed"
reset; on_remote false; reviews '[]'; run block "local true, default branch false: still gated" "$M" "no review by"
reset; on_remote true; perl -0777 -i -pe 's/"riskPaths": \[.*?\]/"riskPaths": ["nothing\/**"]/s' "$TMP/proj/.claude/t4.json"
files '[{"filename":"backend/app/risk/veto.py"}]'; run block "default-branch riskPaths win over a narrowed local list" "$M" "risk paths"
reset; echo "HTTP 502" > "$FIX/fail.contents"; run block "local on, default branch unreadable: fail closed" "$M" "fail closed"
reset; run allow "merge mentioned inside a heredoc" "cat > f <<'EOF'
gh pr merge 5 --squash
EOF"
called "heredoc mention: gh never called" ""
reset; run allow "merge mentioned inside a PowerShell here-string" "\$b = @'
then gh pr merge 5
'@" "" PowerShell
reset; run block "merge mentioned in a quoted body fails closed" 'gh issue comment 5 --body "then (gh pr merge 5) later"' "$UNPARSED"
called "quoted mention: gh never called" ""

# --- normal pass ----------------------------------------------------------------
reset; run allow "review at head, verdict 0/0, thread resolved and answered" "$M"
reset; reviews '[]'; run block "no review" "$M" "no review by opencode-agent\[bot\] at the PR head"
reset; reviews "[$(rev 101 $OLD)]"; run block "review only on an old SHA" "$M" "no review by"
reset; reviews "[$(rev 101 $HEAD "$OK_BODY" github-actions[bot])]"; run block "review at head by a different bot" "$M" "no review by"
reset; reviews "[$(rev 101 $HEAD "$OK_BODY" opencode-agent User)]"; run block "user account named like the bot does not count" "$M" "no review by"
reset; set_cfg true opencode-agent; run allow "bot login configured without [bot]" "$M"
reset; threads "$(thr false false 101)"; run block "unresolved OpenCode thread" "$M" "1 OpenCode review thread.*not resolved: a.py:3"
reset; threads "$(thr true false 101)"; run block "resolved with no reply and not outdated" "$M" "resolved with no fix and no reply"
reset; threads "$(thr true false 101 untrusted-reply)"; run block "resolved with only an untrusted reply" "$M" "no fix and no reply"
reset; threads "$(thr true true 101)"; run allow "resolved and outdated (line changed)" "$M"
reset; threads '{"isResolved":false,"isOutdated":false,"path":"a","line":1,"comments":{"nodes":[{"author":{"login":"github-actions","__typename":"Bot"},"pullRequestReview":{"databaseId":999}}]}}'
run allow "unresolved thread from another reviewer does not count" "$M"
reset; threads '' true; run block "more than 100 threads" "$M" "more than 100 review threads"
reset; reviews "[$(rev 101 $HEAD 'OpenCode review\n\n- [major] x\n\nVerdict: 0 blocker, 1 major')]"; run block "verdict with a major finding" "$M" "0 blocker and 1 major"
reset; reviews "[$(rev 101 $HEAD 'OpenCode review\n\n- no findings')]"; run block "review without a Verdict line" "$M" "no .Verdict"
reset; reviews "[$(rev 100 $HEAD 'Verdict: 1 blocker, 0 major'),$(rev 101 $HEAD)]"; run allow "only the latest review at head counts for the verdict" "$M"

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
reset; files '[{"filename":".claude/hooks/x"}]'; comments "[$(comment "Notes first.
$IND")]"
run block "record that is not on the first line" "$M" "risk paths"
reset; files '[{"filename":"docs/a.md"}]' 3; run block "file list shorter than changedFiles" "$M" "listed 1 of 3 changed files"

# --- fallback -------------------------------------------------------------------
reset; reviews '[]'; comments "[$(comment "$FB"),$(comment "$IND" COLLABORATOR)]"
run allow "fallback with both markers" "$M"
reset; reviews '[]'; comments "[$(comment "$FB")]"
run block "fallback without the self-review record" "$M" "t4:independent-review head=<sha>.*\[missing\]"
reset; reviews '[]'; comments "[$(comment "<!-- t4:opencode-fallback head=$HEAD reason=\"\" -->"),$(comment "$IND")]"
run block "fallback with an empty reason" "$M" "opencode-fallback.*\[missing\]"
reset; reviews '[]'; comments "[$(comment "<!-- t4:opencode-fallback head=$HEAD reason=\"...\" -->"),$(comment "$IND")]"
run block "fallback with the placeholder reason" "$M" "opencode-fallback.*\[missing\]"
reset; threads "$(thr false false 101)"; comments "[$(comment "$FB"),$(comment "$IND")]"
run block "fallback does not excuse unresolved threads when the review exists" "$M" "not resolved"
reset; reviews '[]'; run block "block message never prints the real SHA in a marker" "$M" "head=<sha> reason=\"\.\.\.\""
if printf '%s' "$err" | grep -q "head=$HEAD"; then fail=$((fail + 1)); echo "FAIL - real SHA leaked into a marker template"; else pass=$((pass + 1)); echo "ok   - real SHA not in any marker template"; fi

# --- round cap (opencodeMaxRounds, default 5) ----------------------------------------
# rounds <n> [resolved]: n OpenCode reviews on distinct commits, the last one at HEAD
# (id 101) with an unresolved thread, unless $2 = resolved.
rounds() {
  local i list=""
  for ((i = 1; i < $1; i++)); do list+="$(rev "$i" "$(printf '%040d' "$i")"),"; done
  reviews "[$list$(rev 101 $HEAD)]"
  [ "${2:-}" = resolved ] && return
  threads "$(thr false false 101)"
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
reset; printf '{"errors":[{"message":"boom"}],"data":null}' > "$FIX/threads.json"; run block "GraphQL errors" "$M" "GraphQL reply has errors"
reset; perl -0777 -i -pe 's/"riskPaths": \[.*?\]/"riskPaths": []/s' "$TMP/proj/.claude/t4.json"
run block "empty riskPaths while opted in" "$M" "riskPaths"
reset; run block "merge via the GitHub MCP tool" "" "can merge without this gate" mcp__github__merge_pull_request

# --- PR selector and merge flags ----------------------------------------------------
reset; run allow "flag before the number" "gh pr merge --squash 42 $MHC"; called "flag before the number -> PR 42" "^pr view 42 "
reset; run allow "number inside a quoted flag value" "gh pr merge --subject \"Release 2026\" 12 --squash $MHC"
called "quoted value skipped -> PR 12" "^pr view 12 "
reset; run allow "PR URL" "gh pr merge $URL --merge $MHC"; called "PR URL passed to pr view" "^pr view $URL "
reset; run block "no selector is refused" "gh pr merge --squash $MHC" "name the PR explicitly"; called "no selector: gh never called" ""
reset; run block "branch selector is refused" "gh pr merge feature/x $MHC" "name the PR explicitly"
reset; run allow "--repo flag" "gh pr merge -R Slow-Inc/OrionFX 9 $MHC"; called "--repo passed through" "^pr view 9 --repo Slow-Inc/OrionFX "
reset; run allow "-R<repo> without a space" "gh pr merge -RSlow-Inc/OrionFX 9 $MHC"; called "-R<repo> passed through" "^pr view 9 --repo Slow-Inc/OrionFX "
reset; run block "missing --match-head-commit" "gh pr merge 7 --squash" "add .--match-head-commit"
reset; run block "--match-head-commit of another commit" "gh pr merge 7 --match-head-commit $OLD" "is not the PR head"
reset; run allow "--match-head-commit=<sha> form" "gh pr merge 7 --squash --match-head-commit=$HEAD"
reset; run allow "redirection after the merge" "gh pr merge 7 --squash $MHC 2>&1"
reset; reviews '[]'; run block "quoted absolute gh path is gated" "\"/c/Program Files/GitHub CLI/gh.exe\" pr merge 7 $MHC" "no review by"
reset; reviews '[]'; run block "merge after cd && is gated" "cd /x && gh pr merge 7 $MHC" "no review by"
reset; run block "two merges in one command" "gh pr merge 1 $MHC; gh pr merge 2 $MHC" "$UNPARSED"
reset; run block "env-prefixed merge is refused" "GH_REPO=other/repo gh pr merge 7 $MHC" "$UNPARSED"
reset; reviews '[]'; run block "PowerShell call operator is gated" "& 'C:\\Program Files\\GitHub CLI\\gh.exe' pr merge 7 $MHC" "no review by" PowerShell
reset; reviews '[]'; run block "Monitor command is gated" "$M" "no review by" Monitor

# --- probes from the independent review of PR #150 (review150/probe.sh) -------------
# Every probe must block: reviews '[]' means any gated merge blocks, so "allow" = bypass.
P() { reset; reviews '[]'; run block "probe: $1" "$2" "" "${3:-Bash}"; }
P "Bash tool, plain merge"     "gh pr merge 7"
P "PowerShell tool name"       "gh pr merge 7" PowerShell
P "brace group"                "{ gh pr merge 7; }"
P "if/then"                    "if true; then gh pr merge 7; fi"
P "bash -c"                    'bash -c "gh pr merge 7"'
P "eval"                       "eval 'gh pr merge 7'"
P "time prefix"                "time gh pr merge 7"
P "command prefix"             "command gh pr merge 7"
P "env prefix cmd"             "env gh pr merge 7"
P "backslash-newline"          $'gh pr \\\n merge 7'
P "escaped letter (probe.sh literal)" $'gh pr \\n merge 7'
P "gh -R before pr"            "gh -R Slow-Inc/OrionFX pr merge 7"
P "gh api PUT merge"           "gh api -X PUT repos/Slow-Inc/OrionFX/pulls/7/merge"
P "graphql mergePullRequest"   "gh api graphql -f query='mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}'"
P "graphql auto-merge"         "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{pullRequestId:\"x\"}){clientMutationId}}'"
P "alias"                      "gh alias set m 'pr merge' && gh m 7"
P "--auto"                     "gh pr merge 7 --auto --squash $MHC"
P "&&-chained"                 "true && gh pr merge 7"
P "subshell"                   "(gh pr merge 7)"
P "xargs"                      "echo 7 | xargs gh pr merge"
reset; reviews '[]'; comments "[$(comment "Gate output:
\`\`\`
      <!-- t4:opencode-fallback head=$HEAD reason=\"...\" -->   (state the cause)  [missing]
      <!-- t4:independent-review head=$HEAD -->   followed by the findings and how each was handled  [missing]
\`\`\`")]"
run block "probe: pasted gate message unlocks fallback" "$M"
reset; reviews '[]'; comments "[$(comment "> <!-- t4:opencode-fallback head=$HEAD reason=\"x\" -->"),$(comment "> $IND")]"
run block "probe: blockquoted markers" "$M"
reset; reviews "[$(rev 101 $HEAD '- [blocker] bad\n\nVerdict: 1 blocker, 0 major')]"; threads ''
run block "probe: summary-only blocker (Verdict: 1 blocker)" "$M" "1 blocker"
reset; reviews "[$(rev 100 $OLD),$(rev 101 $HEAD)]"; threads "$(thr false false 100)"
run block "probe: unresolved thread from previous round" "$M" "not resolved"
reset; reviews '[{"id":1,"user":"x","commit_id":"1"}]'; run block "probe: malformed user field" "$M"
reset; printf '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false},"nodes":["x"]}}}},"errors":[{"message":"partial"}]}' > "$FIX/threads.json"
run block "probe: graphql partial data + errors" "$M" "errors"
# Out of scope by design: a direct push to main is blocked by the GitHub ruleset, not by this hook.
reset; reviews '[]'; run allow "probe: git push to main is the ruleset's job (documented)" "git push origin HEAD:main"

# --- security re-review of PR #150 ---------------------------------------------------
# 1. The decision is the deny JSON on stdout, not the exit code. On Windows,
#    run-hook.cmd's `exit /b %ERRORLEVEL%` sits in a `( ... )` block, expands at parse
#    time and returns 0 -- so an exit-2-only hook blocks nothing there.
stdin_for() { perl -MJSON::PP -e 'print encode_json({tool_name=>"Bash",tool_input=>{command=>$ARGV[0]}})' "$1"; }
# shim <name> <expect> <runner...>: run the hook through <runner>, stdin = a gated merge.
shim() {
  local name="$1" expect="$2" rc got; shift 2
  : > "$TMP/reason"
  stdin_for "$M" | "$@" >"$TMP/out" 2>"$TMP/err"; rc=$?
  got="$(decide "$rc" "$TMP/out")"
  if [ "$got" = "$expect" ]; then pass=$((pass + 1)); echo "ok   - $name"
  else fail=$((fail + 1)); echo "FAIL - $name (expected $expect, got $got)"; sed 's/^/       /' "$TMP/err" "$TMP/out"; fi
}
reset; reviews '[]'; shim "exit code lost (bash hook; exit 0): still denied" block bash -c 'bash "$1"; exit 0' _ "$HOOK"
reset; shim "exit code lost, passing merge: still allowed" allow bash -c 'bash "$1"; exit 0' _ "$HOOK"
if command -v cmd.exe >/dev/null 2>&1; then  # Windows only: the real wrapper
  reset; reviews '[]'
  shim "Windows: through run-hook.cmd, a failing merge is denied" block \
    env MSYS_NO_PATHCONV=1 cmd.exe /d /c "$(cygpath -w "$ROOT/.claude/hooks/run-hook.cmd")" orionfx-opencode-merge-gate
else
  pass=$((pass + 1)); echo "ok   - Windows: run-hook.cmd check skipped (no cmd.exe)"
fi
# Fail closed when the checker itself breaks: every path prints the deny JSON.
mkdir -p "$TMP/badperl" "$TMP/noperl"
printf '#!/usr/bin/env bash\nexit 3\n' > "$TMP/badperl/perl"; chmod +x "$TMP/badperl/perl"
reset; shim "perl crashes: deny JSON, not a silent exit" block env PATH="$TMP/badperl:$PATH" bash "$HOOK"
grep -q "internal error" "$TMP/reason" && { pass=$((pass + 1)); echo "ok   - perl crash: reason says internal error"; } \
  || { fail=$((fail + 1)); echo "FAIL - perl crash: reason says internal error"; }
reset; shim "perl missing: deny JSON" block env PATH="$TMP/noperl" "$BASH" "$HOOK"
# The gate must stay silent (exit 0, empty stdout) where it has no opinion.
reset; run allow "silent on a plain command" "ls -la"
reset; run allow "silent on gh pr view" "gh pr view 7 --json headRefOid"
reset; run allow "silent on gh pr checks" "gh pr checks 7 --watch"

# 2. Quoting and escaping inside the keyword. Each one is a real merge to the shell.
P "single-quoted merge"        "gh pr 'merge' 7"
P "double-quoted merge"        'gh pr "merge" 7'
P "empty quotes inside merge"  "gh pr m''erge 7"
P "backslash inside merge"     'gh pr me\rge 7'
P "ANSI-C quoted merge"        "gh pr \$'merge' 7"
P "hex-escaped merge"          "gh pr \$'\\x6derge' 7"
P "mid-word line continuation" $'gh pr mer\\\nge 7'
P "flag between pr and merge"  "gh pr -R Slow-Inc/OrionFX merge 7"
P "--repo= between pr and merge" "gh pr --repo=Slow-Inc/OrionFX merge 7"
P "quoted gh"                  "'gh' pr merge 7"
P "PowerShell backtick in merge" 'gh pr me`rge 7' PowerShell
P "variable subcommand"        'gh pr $SUB 7'
reset; reviews '[]'; run block "quoted merge with a head SHA is gated, not skipped" "gh pr 'merge' 7 $MHC" "no review by"
reset; run allow "quoted merge that passes the gate" "gh pr \"merge\" 7 --squash $MHC"

# 3. Heredoc / here-string bodies fed to an interpreter are code, not data.
P "heredoc into bash"          $'bash <<X\ngh pr merge 7\nX'
P "quoted heredoc into sh"     $'sh <<\'EOF\'\ngh pr merge 7\nEOF'
P "heredoc into bash -s"       $'bash -s <<EOF\ngh pr merge 7\nEOF'
P "heredoc piped to bash"      $'cat <<EOF | bash\ngh pr merge 7\nEOF'
P "heredoc piped to sh"        $'cat <<EOF | sh\ngh pr merge 7\nEOF'
P "heredoc written then run"   $'cat > m.sh <<EOF\ngh pr merge 7\nEOF\nbash m.sh'
P "here-string into bash"      "bash <<< 'gh pr merge 7'"
P "xargs fed by a heredoc"     $'xargs gh pr <<EOF\nmerge 7\nEOF'
P "here-string piped to iex"   $'@\'\ngh pr merge 7\n\'@ | iex' PowerShell
P "here-string to Invoke-Expression" $'@"\ngh pr merge 7\n"@ | Invoke-Expression' PowerShell
P "here-string var to Invoke-Expression" $'$s = @\'\ngh pr merge 7\n\'@\nInvoke-Expression $s' PowerShell
P "here-string piped to pwsh"  $'@\'\ngh pr merge 7\n\'@ | pwsh -Command -' PowerShell
P "here-string piped to powershell" $'@\'\ngh pr merge 7\n\'@ | powershell -' PowerShell
P "here-string piped to bash"  $'@\'\ngh pr merge 7\n\'@ | bash' PowerShell
reset; run allow "heredoc body to a non-interpreter stays data" $'gh pr comment 7 --body-file - <<EOF\nthen gh pr merge 7\nEOF'
reset; run allow "quoted heredoc delimiter with a space stays data" $'gh pr comment 7 --body-file - << \'EOF\'\nthen gh pr merge 7\nEOF'
# Text that only looks like a heredoc / here-string opener is not one: the lines after it run.
P "code after <<EOF on the same line" $'cat <<EOF; gh pr merge 7\nx\nEOF'
P "<<EOF inside double quotes" $'echo "<<EOF"\ngh pr merge 7\nEOF'
P "<<EOF inside single quotes" $'echo \'<<EOF\'\ngh pr merge 7\nEOF'
P "<<EOF inside a comment"     $'echo hi # <<EOF\ngh pr merge 7\nEOF'
P "<< as an arithmetic shift"  $'echo $((1<<x))\ngh pr merge 7\nx'
P "@' inside a PowerShell string" $'echo "@\'"\ngh pr merge 7\n\'@"' PowerShell
P "heredoc with no end line"   $'cat <<EOF\ngh pr merge 7'

# 4. --auto in any spelling queues a merge after the gate has run.
# The baseline review passes, so only the --auto rule can block these.
reset; run block "--auto=true is refused" "gh pr merge 7 --auto=true --squash $MHC" "--auto. queues a merge"
reset; run block "--auto=1 is refused" "gh pr merge 7 --auto=1 --squash $MHC" "--auto. queues a merge"
reset; run block "--auto=false is refused too" "gh pr merge 7 --auto=false --squash $MHC" "--auto. queues a merge"
# The shell can BUILD --auto from text the lexer sees differently; only known flags
# with plain spelling pass. Again on the passing baseline.
reset; run block "--auto built by ANSI-C quoting" "gh pr merge 7 --squash $MHC --aut\$'o'" "cannot check|unknown"
reset; run block "--auto built by brace expansion" "gh pr merge 7 --squash $MHC --{auto,squash}" "cannot check|unknown"
reset; run block "--auto split by a line continuation" $'gh pr merge 7 --squash '"$MHC"$' -\\\n-auto' "cannot check|unknown"
reset; run block "--auto hidden in a flag value's brace expansion" "gh pr merge 7 --squash $MHC --subject {x,--auto}" "cannot check"
reset; run block "glob in the merge arguments" "gh pr merge 7 --squash $MHC --subject *" "cannot check"
reset; run block "command substitution in the merge arguments" "gh pr merge 7 --squash $MHC \$(echo --auto)" "cannot check"
reset; run block "PowerShell expression argument" "gh pr merge 7 --squash $MHC ('--au'+'to')" "cannot check" PowerShell
reset; run block "PowerShell splatting" "gh pr merge 7 --squash $MHC @a" "cannot check" PowerShell
reset; run block "unknown flag" "gh pr merge 7 --squash $MHC --frobnicate" "unknown"
reset; run block "--admin is refused" "gh pr merge 7 --squash $MHC --admin" "--admin"
# OpenCode round 2 on PR #150: ANSI-C escapes in the `pr` word, trailing comments, riskPaths.
P "ANSI-C hex-encoded pr"      "gh \$'\\x70r' merge 7"
P "ANSI-C hex in part of pr"   "gh p\$'\\x72' merge 7"
P "ANSI-C octal-encoded pr"    "gh \$'\\160\\162' merge 7"
reset; run block "ANSI-C pr with a passing review is still refused" "gh \$'\\x70r' merge 7 --squash $MHC" "cannot check"
reset; run allow "valid merge with a trailing comment" "$M # merging after review"
reset; files '[{"filename":".github/opencode/post.py"}]'; run block "the OpenCode posting scripts are a risk path" "$M" "risk paths \(.github/opencode/post.py\)"
reset; run allow "quoted subject with parens and braces" "gh pr merge 7 --squash $MHC --subject 'fix(hooks): {x} (#1)'"
reset; run allow "combined short flags and --delete-branch=false" "gh pr merge 7 -sd --delete-branch=false $MHC"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
