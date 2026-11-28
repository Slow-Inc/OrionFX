# PR shepherd brief (Slow-Inc/OrionFX)

You own one branch, from opening its PR until it is ready to merge. Reply to the coordinator in Thai. Code, commits and PR text are in English, and PR bodies are bilingual: English first, then a full Thai mirror.

Hard rules:
- No force-push, no `reset --hard`, no `branch -D`, no `--no-verify`. Do not merge the PR; the coordinator merges.
- Never weaken a hook, a ruleset or a check.
- Always use `--body-file` for PR bodies and comments. Because of a hook, never put the literal text "gh pr merge" in a command line.

Steps:
1. Read CLAUDE.md, docs/agents/workflow.md and the linked issue (`gh issue view <n> --comments`).
2. Check out the branch in your worktree (`git fetch origin && git checkout -B <branch> origin/<branch>`). Merge origin/main into it with `git merge`, resolve any conflicts, run the full verify (`uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`), and push.
3. Open the PR against main.
   - Title: conventional-commit style containing `(#<issue>)`.
   - Body: bilingual. Cover summary, changes, testing evidence, limitations and follow-ups, and say honestly who reviewed (OpenCode pending, plus whether this is a risk path).
   - The body ends with:

     🤖 Generated with [Claude Code](https://claude.com/claude-code)

     https://claude.ai/code/session_01CwrtSFLzjZWgXTGJuEV9hN
4. **Review loop.** `opencode-agent[bot]` reviews automatically; wait for its review at your head SHA (watch the `review` check). For every inline thread:
   - fix it (TDD where it is a behaviour change), or reply with the reason;
   - then resolve it, using GraphQL `addPullRequestReviewThreadReply` and then `resolveReviewThread`.

   Push, and the bot reviews again. Repeat until the latest bot review at the head has the summary last line `Verdict: 0 blocker, 0 major` and there are 0 unresolved bot threads.
   - The cap is 5 rounds. If the same finding repeats 3 times without converging, stop and report.
   - If the OpenCode job reports "OpenCode unavailable", report it; do not fall back silently.
5. Watch the CI checks (guards, lint, typecheck, test, frontend) until all are green.
6. Report: the PR number, the head SHA, the verdict line, the unresolved count, the check states, and anything deferred (open a bilingual follow-up issue for deferred minors and link it).
