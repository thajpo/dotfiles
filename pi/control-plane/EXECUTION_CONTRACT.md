# Pi System Execution Contract

Every process starts from a controller-created run manifest. The manifest
binds project, conversation, working copy, role, build, runtime specification,
owner identity, capability hash, and writer generation. A launcher exports it
as `PI_RUNTIME_MANIFEST` and cannot replace it with a route file.

Secretary, investigator, and reviewer processes run as `host-read-only` and
receive scoped controller tools. Personal, workstream, and integration agents
run in a container with one writable assigned working tree, no writable
project Git common directory, no host credentials, no Docker socket, no SSH
agent, and no external shell network by default.

Docker image reference, image configuration ID, optional registry digest,
platform, and runtime-spec hash are independent values. The tag is inspected
immediately before creation and the container is independently attested.

The host coordinator owns commit creation, immutable submitted refs, branch
updates, integration setup, and target updates. A worker may edit its assigned
files and request a checkpoint or submission, but cannot move a source or
target ref directly.

Unknown process, container, mount, owner, inode, or lock state fails closed.
Restart creates a new run for the same durable conversation and working copy.
