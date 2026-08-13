# Pi Harness Cutover And Rollback Contract

Owns: atomic cutover and rollback transaction.

OpenCode and its configuration remain unchanged before final activation
approval. Pre-activation builds use a sibling staging path and explicit staged
entrypoint; they do not replace live commands or read historical Pi state.

Cutover requires one exact approved build and plan. Under a launch lock, the
controller verifies bytes and evidence, renames the current command generation
to a rollback path, renames staging into the final path, syncs the parent, and
runs a bounded installed smoke. Pi production state is fresh. Any
identity change invalidates approval.

On failure, rollback stops only processes and containers proven to belong to
the exact new generation, restores the prior command generation atomically,
and verifies OpenCode remains usable. It preserves the Pi database,
sessions, worktrees, Git objects/refs, changes, messages, and evidence. It does
not start an earlier Pi product, import historical data, delete new work, move
unrelated refs, contact remotes, or mutate production services.

Historical Pi files, unrelated tmux sessions, unmanaged worktrees, OpenCode,
and remote Git state are protected surfaces before, during, and after cutover.
