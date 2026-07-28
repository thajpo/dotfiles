---
description: Audit an actual diff against a contract and evidence
argument-hint: "<contract-path> <range>"
---
Audit Git range `$2` against contract `$1`.

Inspect the actual diff, repository instructions, changed interfaces, and verification evidence—not worker summaries alone. Check correctness, scope, compatibility, security, concurrency/ownership, numerical behavior, and regression coverage as applicable. Return exactly one disposition as the first line:
- `CERTIFY` — contract satisfied with adequate evidence.
- `REPAIR` — bounded defects can be fixed without redesign.
- `ESCALATE` — architecture, contract, or human decision remains unresolved.
Then list only concrete evidence and required actions.
