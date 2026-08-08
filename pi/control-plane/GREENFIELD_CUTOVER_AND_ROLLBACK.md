# Greenfield Cutover and Rollback

Build into a sibling staging directory. Verify the exact file set, hashes,
first-party package bytes, Pi version, staged process journey, Docker matrix,
permission matrix, and evidence before cutover.

Acquire the launch lock, rename the old installed build to a rollback path,
rename the verified staging build into place, sync the parent directory, and
run the installed smoke test. A new production database is created only
after smoke passes. Historical Pi files are preserved and are not lifecycle
input.

Rollback stops only exact managed Pi processes, preserves the new database,
sessions, worktrees, refs, changes, and evidence, disables the new launchers,
restores the previous command state, and proves the existing OpenCode setup,
historical files, and unrelated Git state are unchanged. It does not start an
older Pi controller or delete new work.
