# Pi Greenfield Execution Contract

Owns: process topology, tool execution, run identity, Docker lifecycle,
dependency execution, and sensitive approvals.

Release 1 supports Linux only. Every conversational Pi, model, session, and
TUI process runs on the host. Secretary, investigator, and reviewer calls use
controller-scoped host adapters. Personal, workstream, and integration writer
read/write/edit/shell calls execute only inside one controller-created and
controller-owned container assigned to that run. Pi itself is never in that
container.

The host Pi process inherits one authenticated controller channel. It cannot
replace the channel or choose its role. The controller derives project, role,
authority, working copy, build, and writer generation, then creates one run
identity exported as `PI_RUNTIME_MANIFEST`. No `PI_TASK_*` greenfield
compatibility behavior exists.

`scripts/pi_control/docker_runtime.py` is the intended sole Docker lifecycle
owner. Before create it inspects an explicit digest-pinned local image and binds
the image reference, configuration ID, registry digest, platform, idle command,
UID/GID, complete mounts, regular-file `.git` mask, read-only root, tmpfs,
network-none, dropped capabilities, no-new-privileges, environment allowlist,
labels, and resource limits into `toolRuntime.specHash`. Mutable tags are not a
production image selection. `pi-sandbox-control` is an inert manifest/channel
broker client and cannot access the Docker socket, host tools, or lifecycle
state.

The controller records durable container name and create intent before create,
the exact container ID immediately after create, and exact inspection after
start. Create, start, exec, stop, and remove calls occur outside SQLite write
transactions. Tool service and run `running` begin only after both container
and host Pi handshakes pass. On host Pi exit the controller disables requests,
stops and removes the exact container, proves it absent, then terminalizes the
run and compare-and-swap clears the writer claim. Unknown cleanup becomes
`needs_attention` and retains the database writer claim.

The writer container receives one assigned working tree at `/workspace`. A
file-form root `.git` marker is hidden by a controller-owned empty regular
read-only file; primary checkouts with directory-form metadata, nested `.git`,
submodules, unsafe parents, changed source inodes, and unexpected mounts fail
closed. It receives no host credentials, channel authority, SSH agent, browser
state, Docker socket, controller socket/database/CLI, unrelated path, writable
Git metadata, push transport, published port, or external shell network.

Every read, write, edit, or shell call re-reads run, conversation, project,
working copy, manifest, current writer epoch, exact container, mounts, and
source inodes. File calls use canonical relative non-symlink paths and a fixed
container helper with structured standard input. Shell calls accept bounded
text or exact argv and always use `docker exec` at `/workspace` with a timeout
and output limit. There is no host fallback; cancellation attempts to kill the
exact observed exec.

Dependency execution is release-scoped to:

- npm projects with a committed `package-lock.json`.
- Python projects with a committed `uv.lock` and hash-pinned requirements for
  every resolved artifact used by the release runner.

Lock bytes, platform, runtime image, and working-copy identity bind the
environment. Unsupported managers, absent locks, unresolved hashes, platform
mismatch, or mutable dependency inputs fail closed.

The npm adapter requires `package-lock.json` version 3 and an exact
`packageManager: npm@X.Y.Z` pin. The Python adapter accepts `uv.lock` only when
every non-local artifact has a SHA-256 hash, or exact `name==version`
requirements with SHA-256 hashes. Inventory compares the immutable base and
candidate Git trees selected by the change revision; callers cannot supply a
filesystem path. Package actions are only `add`, `remove`, or `sync`, must
match that lock delta, and default to lifecycle/build scripts disabled. No
release-1 model tool accepts package-manager argv.

Package materialization is a separately approved one-shot network-container
operation. The controller alone selects a mode-checked read-only artifact cache
root and binds its canonical inventory digest; model requests cannot supply a
host path. The container receives immutable package inputs reconstructed from
the exact candidate Git tree, the cache read-only, and one controller-derived
private environment output read-write. It receives no working Git metadata,
credentials, controller state/socket, or Docker socket. npm runs offline with
scripts ignored; pip runs no-index with required hashes and binary-only inputs.
The receipt records exact image/config/platform identity, cache inventory,
lock input and delta, scripts policy, installed versions, environment path and
tree digest, and container cleanup. If an exact cache is absent, the operation
instead consumes its one-use receipt and records a deterministic refusal
without contacting a registry or claiming an environment was created.

Production images still require registry-digest identity. A local image config
ID without a registry digest is accepted only when a canonical controller
policy is marked test-only, the state root is disposable under `/tmp`, and a
mode-0600 test marker is present. That exception is recorded in evidence and
is never a production default or model-selected input.

Model-visible tools may create an exact sensitive host/network request but may
not approve it. Approval and rejection occur only through a separate
TTY-bound host CLI that is absent from the model tool registry and inherited
controller channel. Approval binds one request digest, execution place,
working directory, effect scope, expiry, and one use. A changed or replayed
request fails.

Release-1 command requests use a fixed operation grammar, not shell text or
caller argv. The controller derives the exact argv, place, assigned working
copy, effect scope, timeout, and (for network operations) local image reference,
configuration ID, and platform. A network operation runs in a separate
controller-owned one-shot bridge container with a read-only root, private
tmpfs, dropped capabilities, no-new-privileges, bounded resources/output/time,
a read-only assigned-working-copy mount, masked Git metadata, no inherited
host environment or credentials, and proved removal. Writer tool containers
remain network-none.

`bin/pi-authorize` is the only command/package approval entrypoint. It opens
`/dev/tty`, verifies `isatty`, displays the exact project, conversation, run,
operation/argv, working directory, effect scope, place, expiry, and digest,
and requires an explicit `APPROVE` or `REJECT`. Its one-use receipt also binds
the controller build and restart epoch. It is absent from role resources and
channel operations. A noninteractive mode exists only behind an explicit
mode-0600 disposable `/tmp` test marker and test environment guard.
