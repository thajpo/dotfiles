# Control-plane disposable fixtures

This directory documents the ownership and safety boundary for Phase 1 test
fixtures.  The fixture implementation lives in
`tests/control_plane/helpers.py`; it is test-only and is not installed or
imported by runtime/controller code.

## Ownership

Each `DisposableEnvironment` owns one temporary root created with Python's
`TemporaryDirectory`:

- `home/` is the fixture `HOME` (including synthetic `.pi/agent/sessions/`);
- `state/` is `XDG_STATE_HOME`;
- `runtime/` is `XDG_RUNTIME_DIR` and is mode `0700` where supported;
- `tmp/` is `TMPDIR`, `TMP`, and `TEMP` for code under test;
- `repository/` is a disposable SHA-1 Git repository;
- `remote.git/` is an inert local bare Git repository used only to test bare
  repository observations; no remote URL is configured and no fetch/push/network
  operation is performed;
- `worktrees/linked/` is a disposable linked worktree;
- an optional `sha256-repository/` is created only after an isolated Git
  object-format capability probe succeeds.

The temporary root, including its dedicated `tmp/` directory, is the only
location the fixture may create, modify, or remove.  The inert bare repository is never a network endpoint.  No fixture
owns or cleans resources under the caller's HOME, current repository, Git refs
or worktrees, Pi sessions, processes, containers, presentation backends, or
installed artifacts.

## Git and process safety

Every Git subprocess receives a copied, deterministic environment from
`sanitize_git_environment()`.  Repository/index/object/config path selectors,
config injection, hooks, pager/editor/diff helpers, transport helpers, and
terminal prompts are removed or neutralized.  Commands use argument arrays and
never `shell=True`.

Process and runtime adapters are in-memory observations with fake identifiers;
they do not start, signal, or kill host processes.  The child failpoint helper
starts a child owned by that helper and only uses its `Popen` handle for timeout
cleanup.  Failpoint selection is passed by constructor/child arguments, never
by an unrestricted installed environment variable.

## Host-state proof

A fixture captures the current repository's filesystem/Git snapshot and the
real HOME snapshot before setup.  `assert_untouched()` compares entries,
permissions, inodes, regular-file hashes, refs/OIDs, linked worktrees, and
status after the test.  Tests must call it before the temporary root is
released.  If a scenario cannot remain inside the temporary root, it is not a
Phase 1 fixture.

The directory is documentation only; it is not a runtime state directory and
must not be populated with generated sessions, databases, logs, or artifacts.
