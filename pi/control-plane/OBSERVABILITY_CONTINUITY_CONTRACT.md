# Pi System Observability and Continuity Contract

Each real scenario records source commit/tree, installed build, package and
extension hashes, registered tools, authority, run manifest, process and
container identity, mounts, working-copy/Git state, writer generation,
controller versions, session identity, project messages, user-visible result,
host mutations, remote mutations, and rollback result.

The project work index is derived from controller state and durable messages.
It reports working now, investigations, changes ready for review, changes
ready to merge, needs attention, recently integrated work, and unmanaged Git
work. Pane text is never lifecycle truth.

Durable secretary, personal, workstream, and integration conversations resume
after restart. Temporary investigations become interrupted and are not
resumed. Pending user decisions and unacknowledged messages survive.

Evidence is written outside the project repository. Static help, syntax, and
import checks are narrow checks only; installed-process evidence must invoke a
real tool through the real Pi process.
