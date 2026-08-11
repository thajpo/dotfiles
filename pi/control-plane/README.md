# Pi Greenfield Control Plane Documents

Owns: document catalog and conflict routing only.

The canonical program is
[`../../PI_GREENFIELD_IMPLEMENTATION_PLAN.md`](../../PI_GREENFIELD_IMPLEMENTATION_PLAN.md).
It alone records program phase state.

| Document | Normative surface |
|---|---|
| [`PRODUCT_CONTRACT.md`](PRODUCT_CONTRACT.md) | User-visible roles, capabilities, and release scope |
| [`STATE_CONTRACT.md`](STATE_CONTRACT.md) | State identity, authority, freshness, and transitions |
| [`EXECUTION_CONTRACT.md`](EXECUTION_CONTRACT.md) | Process topology, tools, manifests, Docker, packages, and approvals |
| [`CHANGE_INTEGRATION_CONTRACT.md`](CHANGE_INTEGRATION_CONTRACT.md) | Git ownership, submissions, reviews, and local integration |
| [`OBSERVABILITY_CONTINUITY_CONTRACT.md`](OBSERVABILITY_CONTINUITY_CONTRACT.md) | Evidence, attention, and continuity |
| [`SYSTEM_INTEGRATION_TEST_PLAN.md`](SYSTEM_INTEGRATION_TEST_PLAN.md) | Scenario and tier coverage |
| [`ACCEPTANCE_PLAN.md`](ACCEPTANCE_PLAN.md) | Gate evaluation and release-verifier rules |
| [`GREENFIELD_CUTOVER_AND_ROLLBACK.md`](GREENFIELD_CUTOVER_AND_ROLLBACK.md) | Cutover and rollback transaction |
| [`PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md`](PRE_ACTIVATION_ACCEPTANCE_RUNBOOK.md) | Human pre-activation procedure |

Each contract is authoritative only for its listed surface. A cross-document
conflict fails validation and must be resolved in the owning document; no
consumer may select the more permissive statement.
