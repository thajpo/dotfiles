# Pi System Acceptance Plan

Acceptance is cumulative across the nine implementation slices in the
greenfield plan. A slice is accepted only when focused tests, cumulative
contract checks, a final diff inspection, and independent evidence pass.

Release gates cover contract, schema, installed process, host read-only
scope, coding sandbox, network, packages, asynchronous work, restart,
review, integration, faults, tmux presentation, installation, rollback,
OpenCode comparison, and human-readable project index/approval cards.

No gate is satisfied by a planned response, `--help`, syntax check, direct
import, package count, or a no-op fixture. Missing evidence is a failed gate.
