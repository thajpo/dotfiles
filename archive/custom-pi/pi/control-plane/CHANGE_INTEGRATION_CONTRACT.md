# Pi Harness Change And Integration Contract

Owns: Git mutation authority, submission, review, and local integration.

The host controller alone creates commits, immutable submitted refs, review
views, integration setup, assigned-branch updates, rollback refs, and final
local target-ref updates. A writer may edit assigned working-tree files and
request an operation. Its container has no writable Git metadata, credential,
push transport, or direct ref-update path.

Submission captures one observed working-tree state, changed paths, tests,
dependency identity, provenance, and expected current branch object. The
controller creates exactly one commit, advances only the assigned branch with
compare-and-swap, and records an immutable submitted revision.

Review binds one immutable revision, dependency-review state, reviewer
identity, and verdict. A new revision or dependency identity makes the receipt
stale. The required reviewer is independent from the submitting writer, and a
review verdict grants no integration authority.

A simple local target update requires one current accepted review, current
tests and dependency evidence, direct ancestry, unchanged expected target, and
one exact user authorization. Otherwise the controller creates an integration
assignment from exact immutable input revisions. Its writer submits a new
revision that receives independent review and a separate exact target-update
authorization.

Every ref mutation names expected old and new object IDs and is idempotently
reconciled. No operation pushes, publishes, deploys, force-resets unrelated
work, or integrates a newer object than the authorized object.
