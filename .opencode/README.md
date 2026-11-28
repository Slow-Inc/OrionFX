# .opencode — skills for the OpenCode reviewer

The OpenCode workflows (`.github/workflows/opencode-*.yml`, via `.github/actions/opencode-run`) run the OpenCode CLI read-only in the repo checkout. OpenCode discovers project skills in `.opencode/skills/<id>/SKILL.md`. Together they carry the **review half** of `t4-dev-workflow` (owner map: xenodeve/xeno-skills#440). Every skill is read-only and produces findings only.

| Skill | Runs |
|---|---|
| `code-review` | Every PR review |
| `scrutinize` | Every PR review |
| `simplify` (applies `karpathy-guidelines`) | Every PR review |
| `security-review` | When the diff touches `.env*`, `backend/app/core/{secrets,config}.py`, `backend/app/api/auth.py`, `.github/`, `.claude/`, `.githooks/` or `.opencode/` |
| `test-quality` | When the diff changes code |
| `ui-audit` | When the diff changes `frontend/` |
| `t4-gates` | Every PR review (last) |
| `architecture-audit` | Weekly audit only |

`opencode-review.yml` works out the conditional triggers from the changed-file list, then tells the model which skills to run and which to skip. The review lists each skipped skill as `Skipped: - <skill> — <reason>`. The posting script then appends the gate line `Verdict: <n> blocker, <n> major`, computed by `post.py`. It counts every `[blocker]`/`[major]` tag anywhere in the summary and in every inline comment body, including inline blocks that were not posted. A failed t4-gates row counts as a major, or as a blocker for gate 10 (the never-list). It overcounts and never undercounts. A review missing a `### <skill>` section for any skill that ran, or missing the `Skipped:` line, is not posted (cause `unavailable: malformed review`). Any model-written `Verdict:` is renamed `Model-verdict:`.

These skills are read from the **PR head**, so a PR can change its own reviewer. Review edits to this folder with that in mind. The scripts that hold tokens run from the base revision; see `docs/agents/workflow.md`.

## Provenance

| Skill | Source | Licence |
|---|---|---|
| `code-review` | Written for this repo. It is not a copy of Claude Code's bundled `/code-review`. | Repo |
| `scrutinize` | Written for this repo, following the method of the "scrutinize" skill from the 9arm skill set. That source has no licence file, so its text is not copied. | Repo |
| `simplify` | Written for this repo. It is not a copy of Claude Code's bundled `/simplify`. | Repo |
| `security-review` | Written for this repo. It is not a copy of Claude Code's bundled `/security-review`. | Repo |
| `test-quality` | Written for this repo. | Repo |
| `ui-audit` | Written for this repo from `DESIGN.md` and `PRODUCT.md`. The impeccable skill was not copied, because its licence was not confirmed. | Repo |
| `architecture-audit` | Written for this repo. It is not a copy of `improve-codebase-architecture`. | Repo |
| `t4-gates` | Written for this repo from `CLAUDE.md` and `docs/agents/workflow.md`. | Repo |
| `karpathy-guidelines` | Vendored verbatim from `xenodeve/xeno-skills` (v0.1.0, `skills/karpathy-guidelines`), which derives from `forrestchang/andrej-karpathy-skills` (MIT). | MIT, `karpathy-guidelines/LICENSE` (xenodeve) |

To update `karpathy-guidelines`, re-copy it from upstream; do not edit it here.
