# Agent Workflow

How agents plan and implement in this repo, and which skills to invoke automatically.

## Development workflow

When planning or implementing a feature, follow this order:

1. **`/grill-me`** — stress-test the concept first (interview-style)
2. **`/grill-with-docs`** — challenge the plan against existing ADRs in `docs/adr/` and `docs/plans/master_planV1.1.md`
3. **Survey the change sites** — enumerate every place the change touches, before the plan exists
4. **`/to-prd`** — create a PRD from the grilled plan (one PRD per epic; module PRDs follow the template in master plan §12 at `docs/prd/PRD-XX-*.md`), carrying the survey as its change inventory
5. **`/to-issues`** — break the PRD into GitHub issues on `Slow-Inc/OrionFX` with triage labels (one issue per deliverable)
6. **`/tdd`** — implement test-first, then make the tests pass

Hard ordering: **PRD → issues → PR**. Never open a PR without a referenced issue.

## Auto-triggered skills

| Trigger | Skill | Condition |
|---|---|---|
| Bug / error / stack trace | `/debug-mantra` | Start a debug session every time |
| After fixing a bug | `/post-mortem` | Record root cause + fix + validation |
| After writing or changing code | `/simplify` | Before committing — check over-engineering |
| Editing UI / frontend (M01) | `/impeccable` | Every time a component or CSS is touched |
| Before merge / ship | `/code-review` + `/scrutinize` | Correctness + outsider perspective |
| Touching a security boundary | `/security-review` | Every change crossing auth, secrets, broker credentials, the MT5/EA bridge, or LLM/news input handling |
| After implementation | `/verify` | Confirm the feature works in the app |

## Training-loop research

The agent may research and improve M04 models and the training process on its own, within PRD #120's protocol and ADR 0001. The limits are in `CLAUDE.md` → "Training-loop research autonomy"; the mechanism is `docs/agents/training-loop.md`. Each research change is still a normal unit of work: issue → TDD → gates → PR. Findings go to `docs/research/`.

## Verification mandate

Local verify is `.claude/t4.json` `"verify"` (backend: ruff, format, mypy, pytest); the CI `frontend` job adds `bun run lint`, `typecheck`, `test`, `build` in `frontend/`. Every M01 dashboard change is verified end-to-end in a real browser — unit tests can't see real layout or live WebSocket updates. Trading-path changes are verified only against fake broker, historical replay, or MT5 Demo — never real funds (master plan §0.2, §11).

## Review gates — OpenCode merge gate (#149, owner decision 2026-11-28)

This section **supersedes** the "Before merge / ship → `/code-review` + `/scrutinize`" row in the table above. It also supersedes any wording elsewhere in this file that calls the OpenCode review advisory only, or says it "does not replace" those gates.

**The hook is the real gate.** `.claude/hooks/orionfx-opencode-merge-gate` enforces this section on Bash, PowerShell and Monitor commands, and on GitHub MCP merge tools, when `.claude/t4.json` has `"requireOpenCodeReview": true`.

### Who reviews

| Case | Review | Record the gate checks |
|---|---|---|
| Every PR | OpenCode: the `review` check, posted by `opencodeBotLogin` (`opencode-agent[bot]`) | The latest review at the head SHA ends with `Verdict: 0 blocker, 0 major`. Every thread the bot opened on the PR, in any round, is resolved and either answered by you or outdated. |
| A changed file (or a rename's old path) matches `riskPaths` | OpenCode **plus** one independent subagent review | PR comment whose first line is `<!-- t4:independent-review head=<sha> -->`, followed by the findings and how each was handled |
| OpenCode is down: model 429, App token failure, timeout, or the review is missing for an infra reason | Fallback self-review with subagents: `code-review`, `scrutinize`, `simplify`, plus `security-review` when relevant | PR comment whose first line is `<!-- t4:opencode-fallback head=<sha> reason="<cause>" -->`, **and** the independent-review comment, both at the head SHA |

Outside these cases, the agent does not spawn subagents to review its own work. TDD, `verify`, and the in-session `simplify` / `security-review` triggers stay the agent's own work.

### The only merge command that passes

```
gh pr merge <number|PR URL> --squash --match-head-commit <head sha>
```

Run it as its own plain command. The gate refuses, fail-closed, any other text that looks like a merge. That text is `pr merge`, `/pulls/<n>/merge`, `mergePullRequest` or `enablePullRequestAutoMerge`. The gate counts these keywords three ways: in the raw text, in a copy with quotes and escapes stripped, and in the lexed words. So `gh pr 'merge'`, `m''erge`, `me\rge` and a mid-word line continuation all count. The refusal covers:
- wrappers and prefixes: `bash -c`, `eval`, `xargs`, `{ …; }`, `if …; then`, `time`, `env`, `command`, `VAR=x`;
- other routes: aliases, `gh -R x pr merge`, `gh api … merge`, two merges in one command;
- anything but a plain word after `gh [-R <repo>] pr`, such as `gh pr $'…'`, `gh pr -R x merge` or `gh pr $SUB`.

It also refuses a missing or branch selector, a missing or stale `--match-head-commit`, `--auto` in any spelling (`--auto`, `--auto=…`), and `--admin`. Only known `gh pr merge` flags pass: `--squash`/`--merge`/`--rebase`, `--delete-branch`, `--disable-auto`, `--subject`, `--body`, `--body-file`, `--author-email`, `--repo` and their short forms. The shell must not be able to build arguments the gate did not see. So, outside quotes, the merge command may not contain `$`, a backtick, `{ }`, `* ? [ ]`, `( )` or a PowerShell `@splat`. A line continuation that changes the words is refused too. Put a subject with parentheses in quotes.

`--match-head-commit` makes GitHub refuse the merge if someone pushed after the check. Text that only *mentions* a merge, such as a PR body or a commit message, belongs in a file (`--body-file`, `-F`) or a heredoc. A heredoc or here-string body is data only while no interpreter reads it. If the command also names one (`bash`, `sh`, `source`, `xargs`, `iex`, `Invoke-Expression`, `pwsh`, `powershell`, `python`, …), the bodies are scanned as code. The detection is quote- and comment-aware: `<<EOF` inside quotes or a comment is not a heredoc, and the rest of a `<<EOF` line is still code.

### The review → fix loop

1. **Push.** OpenCode reviews the new head. Wait for it: `gh pr checks <n> --watch`.
2. **Read the latest review at the current head:** `gh api repos/Slow-Inc/OrionFX/pulls/<n>/reviews`, author `opencode-agent[bot]`, `commit_id` = head SHA. Read its summary, its last line `Verdict: <n> blocker, <n> major`, and its inline threads. A finding can live only in the summary, because inline comments are capped at 15.
3. **Handle every finding.** Fix it if it is valid. Otherwise reply on its thread with the reason. Then **resolve the thread**. To pass, a resolved thread needs your reply, or it must be outdated because the fix changed its line. Get thread ids from `pullRequest.reviewThreads { id isResolved }` in GraphQL. Resolve with `gh api graphql -f query='mutation{resolveReviewThread(input:{threadId:"<id>"}){thread{isResolved}}}'`.
4. **If you pushed a fix, go back to step 1.** The new head needs its own review with `Verdict: 0 blocker, 0 major`. Threads from older rounds still count, so a new commit cannot hide an old finding.
5. **Merge only when the gate passes** with the command above:
   - the latest review at the current head has verdict 0/0;
   - no bot thread is unresolved or resolved silently;
   - on a risk-path PR, an independent-review comment names the current head. Re-post it after each push, covering the new commits.
6. **Round cap.** `opencodeMaxRounds` in `.claude/t4.json` (default 5) caps the loop. The gate counts OpenCode reviews on the PR, one per distinct `commit_id`. If the count is above the cap and the gate still fails, it blocks with **stop and ask the owner**. Do not push another round. Report the open findings to the owner and wait for a decision.

### Markers

Post each marker as the **first line** of its own comment, from your own account, with `gh pr comment <n> --body-file <file>`. The gate ignores a marker inside a code fence, a blockquote or later lines. It accepts a marker only when all of these hold:
- The author is not a bot, and their `author_association` is `OWNER`, `MEMBER` or `COLLABORATOR`. The repo is public, so anyone else's comment is ignored.
- The SHA is the full 40-character head SHA (`gh pr view <n> --json headRefOid`).
- The fallback `reason` is a real cause, not empty and not `...`.
- The independent-review comment has text below the marker line.

The gate's block message prints the markers with placeholders only. Pasting it into a comment satisfies nothing.

```
<!-- t4:independent-review head=<40-char head sha> -->
Independent review (subagent: code-review + scrutinize) of <sha>:
1. <finding> — fixed in <commit>
2. <finding> — not fixed: <reason>
```

```
<!-- t4:opencode-fallback head=<40-char head sha> reason="model 429 FreeUsageLimitError on run <id>" -->
```

### Fallback, CI and the ruleset

- **OpenCode infra failures finish green.** When OpenCode fails for an infrastructure reason, its job is meant to finish green with an "unavailable" notice, so the vendored `t4-gate` (`requireGreenCI`) does not block the fallback. That change belongs to PR #144. Until it lands, a red `review` check still trips `requireGreenCI`; re-run the check or ask the owner.
- **The `review` check is not a required check, and will not become one.** That would make the fallback unreachable. The hook is the gate.
- **The ruleset is live (checked 2026-11-28).** The repo is public. Ruleset `main-protection` (id 28272751) is active on the default branch with no bypass actors. It requires a PR and the `t4-verify` checks `guards`, `lint`, `typecheck`, `test` and `frontend`, and it blocks deletion and force-push. So `git push … :main` is refused server-side, not by this hook. Secret scanning and push protection are on.
- **Dependabot PRs get no OpenCode review**, because the review job skips bot authors. To merge one, run the fallback self-review and post both markers. No Dependabot exemption exists in the hook yet: Dependabot can change `.github/workflows/`, a risk path. An exemption that mirrors the `guards` one is an owner decision.

### `T4-Gates` trailer

`code-review=ran` (and `scrutinize=ran`) means OpenCode reviewed the work, or the fallback subagent review ran. The trailer is written when you commit, usually before OpenCode has run, so `not-run` is the honest value then. The merge gate enforces the review; the trailer does not.

### Limits

- The hook binds Claude only. A human merging on the web is held only by the ruleset's required checks, not by the review gate.
- **How it blocks.** The hook prints a PreToolUse `deny` JSON on stdout and exits 0, the same way `t4-gate` does. It never blocks by exit code. On Windows, the vendored `run-hook.cmd` runs `exit /b %ERRORLEVEL%` inside a `( … )` block, which expands at parse time to 0, so an exit-2 block would be lost. Allow means exit 0 with empty stdout.
- Fail-closed paths, all with the deny JSON:
  - a `gh` or API error, GraphQL `errors`, or a runtime error in the checker;
  - any unexpected exit or signal in the wrapper (a trap);
  - checker output that is not exactly one deny object;
  - perl missing;
  - the 100-second budget for `gh` calls running out;
  - the hook timing out (`onFailure: "block"`, timeout 150 s). `onFailure` needs Claude Code ≥ 2.1.295.
- **"Trusted" is identity-based.** A trusted thread reply or marker comes from an `OWNER`, `MEMBER` or `COLLABORATOR` account. The agent posts as the OWNER account too, so a trusted reply does not prove that a human wrote it.
- **What a text hook cannot close.** It cannot see indirect forms: `$(…)`, a variable holding the command, encoded commands (`pwsh -EncodedCommand`, `base64 -d | sh`), or a script written in one tool call and run in another. The ruleset on `main` is the server-side backstop.
- **`run-hook.cmd` fails open when bash is missing.** It exits 0 silently (an upstream defect in the vendored file). This is one more reason the gate relies on the deny JSON plus the ruleset, not on the exit code.
- A PR with more than 100 review threads is blocked, because the gate does not page them.
- If `.claude/t4.json` or its `requireOpenCodeReview` key is missing, the gate is off. It prints a notice on every merge-looking command, so it never turns off silently.
