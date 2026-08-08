# Pre-Activation Acceptance Runbook

Run the complete installed journey from the greenfield implementation plan
twice in clean disposable environments. Record the exact source, build,
packages, extensions, tools, manifests, process/container observations,
working-copy deltas, messages, approvals, reviews, refs, faults, and rollback
evidence outside the repository.

Before activation, require all release gates to pass, confirm OpenCode remains
usable, and verify that no remote Git branch or production service changed.
If any gate fails, preserve evidence and work, leave the current environment
active, and repair the staged build.
