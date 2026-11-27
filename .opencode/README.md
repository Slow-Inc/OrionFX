# .opencode — skills for the OpenCode PR review

`.github/workflows/opencode-review.yml` runs OpenCode in normal mode on each PR. OpenCode discovers project skills in `.opencode/skills/<id>/SKILL.md`, and its review prompt loads them in this order: `code-review`, `scrutinize`, `simplify`, `t4-gates`.

These skills are read from the **PR head**, so a PR can change its own reviewer. Review edits to this folder with that in mind.

## Provenance

| Skill | Source | Licence |
|---|---|---|
| `code-review` | Written for this repo. It is not a copy of Claude Code's bundled `/code-review`. | Repo |
| `scrutinize` | Written for this repo, following the method of the "scrutinize" skill from the 9arm skill set. That source has no licence file, so its text is not copied. | Repo |
| `simplify` | Written for this repo. It is not a copy of Claude Code's bundled `/simplify`. | Repo |
| `t4-gates` | Written for this repo from `CLAUDE.md`. | Repo |
| `karpathy-guidelines` | Vendored verbatim from `xenodeve/xeno-skills` (v0.1.0, `skills/karpathy-guidelines`), which derives from `forrestchang/andrej-karpathy-skills` (MIT). | MIT, `karpathy-guidelines/LICENSE` (xenodeve) |

To update `karpathy-guidelines`, re-copy it from upstream; do not edit it here.
