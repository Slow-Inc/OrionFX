#!/usr/bin/env bash
# Adversarial probes for .claude/hooks/orionfx-opencode-merge-gate (the PR #150 reviews).
# Each probe has an expected decision, DENY unless stated. The first block runs with NO
# OpenCode review on the PR, so any gated merge must be denied. The last block runs with
# a passing review, so only the argument rules can deny, and its CONTROL rows must ALLOW.
# Each probe goes through two runners, and the decision is read ONLY from stdout JSON:
#   cmd  = cmd.exe -> the real .claude/hooks/run-hook.cmd (Windows only; n/a elsewhere)
#   shim = bash -c 'bash hook; exit 0'  (the exit code is thrown away)
# Usage: bash tests/hooks/probe-orionfx-opencode-merge-gate.sh [repo root]  (exit 1 on a mismatch)
# Not run in CI; the CI suite is tests/hooks/test-orionfx-opencode-merge-gate.sh.
set -uo pipefail
ROOT="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
HOOK="$ROOT/.claude/hooks/orionfx-opencode-merge-gate"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/proj/.claude" "$TMP/fix"
export FIX="$TMP/fix"
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$FIX/calls.log"
case "$1 $2" in
  "pr view") cat "$FIX/view.json" ;;
  "api graphql") echo '{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false},"nodes":[]}}}}}' ;;
  *) case "$*" in
       *reviews*) cat "$FIX/reviews.json" ;;
       */contents/.claude/t4.json) perl -MMIME::Base64 -0777 -ne '(my $b = encode_base64($_)) =~ s/\n/\\n/g; print qq({"content":"$b"})' "$CLAUDE_PROJECT_DIR/.claude/t4.json" ;;
       *) echo '[]' ;;
     esac ;;
esac
STUB
chmod +x "$TMP/bin/gh"
H=1111111111111111111111111111111111111111
printf '{"number":7,"headRefOid":"%s","url":"https://github.com/Slow-Inc/OrionFX/pull/7","changedFiles":0}' "$H" > "$FIX/view.json"
sed 's/"requireOpenCodeReview": *false/"requireOpenCodeReview": true/' "$ROOT/.claude/t4.json" > "$TMP/proj/.claude/t4.json"
export PATH="$TMP/bin:$PATH" CLAUDE_PROJECT_DIR="$TMP/proj"
MHC="--match-head-commit $H"
echo "[]" > "$FIX/reviews.json"
OKREV="[{\"id\":101,\"user\":{\"login\":\"opencode-agent[bot]\",\"type\":\"Bot\"},\"commit_id\":\"$H\",\"body\":\"x\n\nVerdict: 0 blocker, 0 major\"}]"

decide() {  # stdin = hook stdout; prints DENY / ALLOW / BAD
  perl -MJSON::PP -0777 -ne '
    if ($_ eq "") { print "ALLOW"; exit }
    my $v = eval { decode_json($_) };
    print((ref $v eq "HASH" && ($v->{hookSpecificOutput}{permissionDecision} // "") eq "deny") ? "DENY" : "BAD");'
}
CMDHOOK="$(cygpath -w "$ROOT/.claude/hooks/run-hook.cmd" 2>/dev/null || true)"
n=0; bad=0
probe() {  # probe <label> <command> [tool] [expected DENY|ALLOW]
  local in a b want="${4:-DENY}"
  in="$(perl -MJSON::PP -e 'print encode_json({tool_name=>$ARGV[0],tool_input=>{command=>$ARGV[1]}})' "${3:-Bash}" "$2")"
  b="$(printf '%s' "$in" | bash -c 'bash "$1"; exit 0' _ "$HOOK" 2>/dev/null | decide)"
  if [ -n "$CMDHOOK" ] && command -v cmd.exe >/dev/null; then
    a="$(printf '%s' "$in" | MSYS_NO_PATHCONV=1 cmd.exe /d /c "$CMDHOOK" orionfx-opencode-merge-gate 2>/dev/null | decide)"
  else a=n/a; fi
  n=$((n + 1))
  [ "$a" = "$want" ] || [ "$a" = n/a ] || bad=$((bad + 1))
  [ "$b" = "$want" ] || bad=$((bad + 1))
  printf '| %2d | %-44s | %-5s | %-5s | %-5s |\n' "$n" "$1" "$want" "$a" "$b"
}
echo "| #  | probe                                        | want  | cmd   | shim  |"
echo "|----|----------------------------------------------|-------|-------|-------|"
# --- original review probes (review150/probe.sh) ---
probe "plain merge (Bash)"                    "gh pr merge 7"
probe "plain merge (PowerShell)"              "gh pr merge 7" PowerShell
probe "brace group"                           "{ gh pr merge 7; }"
probe "if/then"                               "if true; then gh pr merge 7; fi"
probe "bash -c"                               'bash -c "gh pr merge 7"'
probe "eval"                                  "eval 'gh pr merge 7'"
probe "time prefix"                           "time gh pr merge 7"
probe "command prefix"                        "command gh pr merge 7"
probe "env prefix"                            "env gh pr merge 7"
probe "backslash-newline"                     $'gh pr \\\n merge 7'
probe "gh -R before pr"                       "gh -R Slow-Inc/OrionFX pr merge 7"
probe "gh api PUT .../merge"                  "gh api -X PUT repos/Slow-Inc/OrionFX/pulls/7/merge"
probe "graphql mergePullRequest"              "gh api graphql -f query='mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}'"
probe "graphql enablePullRequestAutoMerge"    "gh api graphql -f query='mutation{enablePullRequestAutoMerge(input:{pullRequestId:\"x\"}){clientMutationId}}'"
probe "alias"                                 "gh alias set m 'pr merge' && gh m 7"
probe "--auto"                                "gh pr merge 7 --auto --squash $MHC"
probe "&&-chained"                            "true && gh pr merge 7"
probe "subshell"                              "(gh pr merge 7)"
probe "xargs"                                 "echo 7 | xargs gh pr merge"
probe "MCP merge tool"                        "" mcp__github__merge_pull_request
probe "Monitor tool"                          "gh pr merge 7 $MHC" Monitor
probe "env VAR= prefix"                       "GH_REPO=x/y gh pr merge 7 $MHC"
probe "two merges"                            "gh pr merge 1 $MHC; gh pr merge 2 $MHC"
probe "valid form, no review"                 "gh pr merge 7 --squash $MHC"
probe "quoted gh path"                        "\"/c/Program Files/GitHub CLI/gh.exe\" pr merge 7 $MHC"
# --- bypasses from the security re-review ---
probe "single-quoted merge"                   "gh pr 'merge' 7"
probe "double-quoted merge"                   'gh pr "merge" 7'
probe "m''erge"                               "gh pr m''erge 7"
probe 'me\rge'                                'gh pr me\rge 7'
probe "heredoc into bash"                     $'bash <<X\ngh pr merge 7\nX'
probe "heredoc into sh"                       $'sh <<\'EOF\'\ngh pr merge 7\nEOF'
probe "here-string | iex"                     $'@\'\ngh pr merge 7\n\'@ | iex' PowerShell
probe "here-string | Invoke-Expression"       $'@\'\ngh pr merge 7\n\'@ | Invoke-Expression' PowerShell
probe "cat heredoc | bash"                    $'cat <<EOF | bash\ngh pr merge 7\nEOF'
probe "cat heredoc | sh"                      $'cat <<EOF | sh\ngh pr merge 7\nEOF'
probe "here-string | pwsh"                    $'@\'\ngh pr merge 7\n\'@ | pwsh -Command -' PowerShell
probe "here-string | powershell"              $'@\'\ngh pr merge 7\n\'@ | powershell -' PowerShell
probe "--auto=true"                           "gh pr merge 7 --auto=true --squash $MHC"
probe "--auto=1"                              "gh pr merge 7 --auto=1 --squash $MHC"
# --- found while fixing ---
probe "ANSI-C \$'merge'"                      "gh pr \$'merge' 7"
probe "hex \$'\\x6derge'"                     "gh pr \$'\\x6derge' 7"
probe "mid-word continuation mer\\<LF>ge"     $'gh pr mer\\\nge 7'
probe "gh pr -R x merge"                      "gh pr -R Slow-Inc/OrionFX merge 7"
probe "code after <<EOF on the same line"     $'cat <<EOF; gh pr merge 7\nx\nEOF'
probe "<<EOF inside quotes"                   $'echo "<<EOF"\ngh pr merge 7\nEOF'
probe "<<EOF inside a comment"                $'echo hi # <<EOF\ngh pr merge 7\nEOF'
probe "<< as arithmetic shift"                $'echo $((1<<x))\ngh pr merge 7\nx'
probe "@' inside a PS string"                 $'echo "@\'"\ngh pr merge 7\n\'@"' PowerShell
probe "ANSI-C \$'\\x70r' merge"                "gh \$'\\x70r' merge 7"
probe "ANSI-C p\$'\\x72' merge"                "gh p\$'\\x72' merge 7"
probe "ANSI-C octal \$'\\160\\162' merge"      "gh \$'\\160\\162' merge 7"
probe "heredoc into \$SHELL"                    $'$SHELL <<EOF\ngh pr merge 7\nEOF'
probe "heredoc into /bin/ash"                  $'/bin/ash <<EOF\ngh pr merge 7\nEOF'
probe "heredoc into make -f -"                 $'make -f - <<EOF\nall:\n\tgh pr merge 7\nEOF'
probe "gh api --input - merge mutation"        $'gh api graphql --input - <<EOF\n{"query":"mutation{mergePullRequest(input:{pullRequestId:\\"x\\"}){clientMutationId}}"}\nEOF'
# --- passing review at the head: only the argument rules can deny these ---
printf '%s' "$OKREV" > "$FIX/reviews.json"
probe "CONTROL: valid merge, passing review"   "gh pr merge 7 --squash $MHC" Bash ALLOW
probe "--auto=true (passing review)"            "gh pr merge 7 --squash $MHC --auto=true"
probe "--auto=1 (passing review)"               "gh pr merge 7 --squash $MHC --auto=1"
probe "--aut\$'o' (passing review)"            "gh pr merge 7 --squash $MHC --aut\$'o'"
probe "--{auto,squash} (passing review)"        "gh pr merge 7 --squash $MHC --{auto,squash}"
probe "-\\<LF>-auto (passing review)"           "gh pr merge 7 --squash $MHC -\\"$'\n'"-auto"
probe "--subject {x,--auto} (passing review)"   "gh pr merge 7 --squash $MHC --subject {x,--auto}"
probe "\$(echo --auto) (passing review)"        "gh pr merge 7 --squash $MHC \$(echo --auto)"
probe "PS ('--au'+'to') (passing review)"      "gh pr merge 7 --squash $MHC ('--au'+'to')" PowerShell
probe "PS @splat (passing review)"              "gh pr merge 7 --squash $MHC @a" PowerShell
probe "--admin (passing review)"                "gh pr merge 7 --squash $MHC --admin"
probe "ANSI-C \$'\\x70r' merge (passing review)" "gh \$'\\x70r' merge 7 --squash $MHC"
probe "export PATH=...; merge (passing review)" "export PATH=/tmp/evil:/usr/bin; gh pr merge 7 --squash $MHC"
probe "hash -p ...; merge (passing review)"     "hash -p /tmp/evil/gh gh; gh pr merge 7 --squash $MHC"
probe "alias gh=...; merge (passing review)"    "alias gh=/tmp/evil/gh; gh pr merge 7 --squash $MHC"
probe "cd /x && merge (passing review)"         "cd /x && gh pr merge 7 --squash $MHC"
probe "merge | cat (passing review)"            "gh pr merge 7 --squash $MHC | cat"
probe "absolute gh path (passing review)"      "/usr/bin/gh pr merge 7 --squash $MHC"
probe "CONTROL: trailing # comment"            "gh pr merge 7 --squash $MHC # note" Bash ALLOW
probe "CONTROL: quoted 'merge' (gated, passes)" "gh pr 'merge' 7 --squash $MHC" Bash ALLOW
echo
echo "$n probes, $bad runner results that differ from the expected decision"
[ "$bad" -eq 0 ]
