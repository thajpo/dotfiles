# Pi Greenfield System Integration Test Plan

Owns: release scenario inventory and evidence-tier coverage.

All scenarios use disposable HOME, state, data, cache, runtime, Git, and
dependency roots. They use a deterministic local provider unless a separately
approved scenario states otherwise. They do not mutate production, contact a
remote model provider, install live files, or activate launchers during
pre-activation testing.

Evidence tiers are `contract`, `source-process`, `staged-installed`, `docker`,
`presentation`, `rollback`, and `activation`. A scenario records only the tier actually
executed. The action catalog is the machine-readable source of action IDs,
scenario IDs, allowed tiers, implementation state, and owning phase.

Required scenario groups:

| Group | Required observations |
|---|---|
| Fresh state | Explicit registration, distinct repositories, old sentinels unchanged, no adoption |
| Host roles | Real host Pi, inherited controller channel, derived role, scoped reads, forbidden mutation |
| Writer runtime | Final staged host Pi brokers real read/write/edit/shell to one attested container; Pi and credentials absent there; image/mount/env/network/Git/capabilities, second-writer rejection, PID split, expected delta, and exact cleanup are observed |
| Dependencies | Linux npm lock and Python uv/hash reproduction; unsupported manager refusal |
| Coordination | Workstreams, messages, acknowledgement, attention, restart |
| Approval | Model request only; separate TTY approve/reject; exact use and replay refusal |
| Change delivery | Controller commit/ref ownership, exact review, local fast path, integration path, no push |
| Recovery | Process/container/lock/ref failpoints, ambiguity retention, one writer |
| Installation | Exact staged bytes, two clean full journeys, atomic cutover simulation, rollback |

For every evidence envelope, the verifier checks that every action ID exists,
the scenario belongs to every referenced action, and the tier is declared by
every referenced action. Planned actions may be exercised as foundation work,
but their evidence cannot satisfy release verification.
