---
description: Repair or reject a patch against its contract
argument-hint: "<contract-path> <diff-or-branch>"
---
Evaluate and repair `$2` against the approved contract `$1`.

Inspect intent, actual diff, tests, and worker assumptions. Preserve no implementation merely because it exists. Return exactly one disposition, then evidence:
- `REPAIRED` — bounded defects fixed locally and verified.
- `ESCALATE` — contract/design/human decision is inadequate.
- `REIMPLEMENT` — patch should be discarded and rebuilt.
Avoid unrelated changes; rerun relevant acceptance checks.
