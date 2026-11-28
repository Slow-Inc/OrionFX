---
name: security-review
description: OrionFX security review of a PR diff. Run only when the diff touches security paths (secrets/config, API auth, .github/, .claude/, .githooks/). Reports high-confidence, exploitable issues only.
---

# security-review (OrionFX)

## Trigger

Run this skill only when the diff touches at least one of these paths:
- `.env*`, `backend/app/core/secrets.py`, `backend/app/core/config.py`
- `backend/app/api/auth.py`
- `.github/**`, `.claude/**`, `.githooks/**`

Otherwise skip it with the reason "no security paths touched".

## Look for (high confidence only)

- **Secrets in the diff:** keys, tokens, passwords, broker or account credentials, and real account data. Credential IDs that only work with OIDC are not secrets.
- **Workflows:**
  - `pull_request_target`, or `workflow_run` that checks out PR code;
  - untrusted event text (titles, bodies, comments, branch names) interpolated with `${{ }}` directly into `run:`;
  - `permissions` broader than the job needs, such as `contents: write` on a job that only reads;
  - secrets or tokens reachable by fork PRs;
  - third-party actions not pinned by full SHA;
  - a token held by a script that a PR can change.
- **Auth:** bypasses in `backend/app/api/auth.py`, missing checks on new endpoints, and session or cookie flags.
- **Hooks and guards:** changes in `.claude/` or `.githooks/` that weaken a gate, for example skipping the issue reference, the `T4-Gates:` trailer or the dangerous-git block.

Report a finding only with a concrete attack path, stating who the attacker is, what they control and what they gain. Do not report DoS, missing hardening, or theoretical issues.

## Output section

```
### security-review: secrets / workflows / auth / hooks
- [severity] path:line — issue — attack path — fix
```

Severity is `blocker` for an exploitable secret leak or a write/escalation path, `major` for an exploit that needs specific conditions, and `minor` for a weak boundary that is not exploitable today. If there are no findings, write `- no findings`.
