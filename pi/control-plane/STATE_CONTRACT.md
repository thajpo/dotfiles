# Pi System State Contract

The active state root is `~/.local/state/pi-system/` with mode `0700` and a
WAL SQLite database. New installations start at schema epoch one. The fresh
schema is defined in `scripts/pi_control/greenfield_schema.py` and is never
populated from another controller database.

Authorities:

- SQLite owns projects, working copies, conversations, workstreams, runs,
  messages, requests, reviews, operations, attention, and package records.
- Git owns source objects, submitted immutable refs, branches, and files.
- Session JSONL owns conversation history.
- A process-lifetime kernel lock plus the database writer generation owns
  active writer fencing.
- The target branch moves only through an exact expected-old-object operation.
- Tmux owns no lifecycle identity.

Every cross-resource operation checks project identity, resource versions,
exact revisions, writer generations, and idempotency keys. Message delivery
and acknowledgement are separate states. A repeated idempotency key with
different content fails.

The schema includes durable `project_messages`, exact `command_requests`,
`dependency_changes`, `package_security_reviews`, and per-working-copy
`package_environments`. Uncertain recovery is retained as attention; it is not
repaired by deletion or guessed liveness.
