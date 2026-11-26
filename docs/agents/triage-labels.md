# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.


## T4 delta — groups beyond the five roles

- **Component** — exactly one per issue (the module that owns it, master plan §2): `M01-dashboard`, `M02-market-data`, `M03-llm-news`, `M04-prediction`, `M05-decision`, `M06-risk-execution`, `M07-learning-memory`, `M08-system-core`, `X01-integration`, `X02-validation`, `repo-ops` (tooling, CI, docs infrastructure).
- **Type** — one or more: `bug`, `Feature`, `tech-debt`, `security`, `Optimization`, `Cleanup`, `Test`.
- **Severity** — one per bug/security issue: `critical`, `Major`, `Minor`.
- **Lifecycle** — `Latent` (exists in code, not yet manifested), `Dormant` (real but deprioritised).

## Conventions

- Every issue has ≥1 triage-role label and exactly one Component label.
- A `security` issue must carry `critical` or `Major` — a `Minor` security label is not valid.
- A `Latent` bug that activates is upgraded to a full `bug` issue with severity.
