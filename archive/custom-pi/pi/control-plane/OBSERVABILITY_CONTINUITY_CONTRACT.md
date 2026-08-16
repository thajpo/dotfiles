# Pi Harness Observability And Continuity Contract

Owns: evidence envelopes, user attention, and restart continuity.

Every evidence envelope binds schema version, source/build/fixture identity,
catalog action IDs, one catalog scenario, one declared tier, assertions,
commands, before/after observations, capability, and fault seed. It explicitly
records `installedProductActionObserved`, `productionMutationPerformed`, and
`remoteProviderContacted`. Evidence is immutable and retained outside the
repository.

An envelope cannot prove more than it observed. Help, syntax, import, direct
module, idle-pane, planned, skipped, no-op, or fabricated-runtime observations
are not installed product evidence. Missing or unobservable assertions fail
closed. Digests and redaction preserve identity without storing secrets.

Durable secretary, personal, workstream, and integration conversations retain
their controller identity, session identity, working copy, task, messages,
pending decisions, and attention across restart. Restart creates a new process
and run. Temporary investigators are interrupted and are not automatically
resumed; completed results remain durable.

The project work index is derived from controller records and durable messages,
not pane text, timestamps, process names, container names, or marker files.
Unknown liveness is shown as attention and does not grant writer authority.
