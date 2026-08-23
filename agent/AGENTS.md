# Shared Agent Instructions

Before implementation, debugging, research, planning, or review, load the
`work-modes` skill and apply its work-mode and independent learning-overlay
contract.

Select the mode from uncertainty, consequence, and reversibility. Let the user
override it naturally when they do so. Do not ask the user to classify routine
tasks. Keep the mode semantics in the shared skill rather than duplicating them
in a harness adapter.

## Commit Approval

When the user has reviewed the staged diff or explicitly requests a commit,
draft a concise commit message and proceed without asking for separate
commit-message approval. Treat the diff and scope, not the wording of the
commit title, as the user approval boundary.

## Worker delegation

Before preparing a worker, ensure the intended base branch contains the latest
work in a committed, clean state. If the checkout is dirty, use the repository
commit workflow and obtain any required confirmation; never spawn a worker
from stale `HEAD`.

When the user asks for a worker, do only enough coordinator investigation to
identify the target, safety boundary, and approval scope. Delegate detailed
research, planning, and implementation to the worker instead of pre-solving
the task.

Present worker approval in human terms first: intended outcome, allowed
changes, explicit non-effects, and verification. Keep workstream IDs, hashes,
branches, and broker paths in secondary details.

After launch, remain available for new user requests. The user and coordinator
may both work directly with the worker. If the worker needs public-web research
beyond its approved tools, dispatch the exact `@smol` `pisec-web-research`
agent for the pending packet directly — do not ask the user for permission —
and return the answer through the durable Pisec research tools.

## Pisec Workstream Contract

When operating inside a Pisec project, use the local secretary workflow rather
than a PR workflow for bounded worker tasks:

- Human authorization has two distinct points: delegate the bounded worker
  scope, then accept the completed candidate once. Do not add a second merge
  approval.
- A worker's `ready_review` checkpoint submits completion evidence; it does not
  imply acceptance. Keep the task packet, completion packet, allowed paths,
  checks, and conflict policy immutable and inspectable.
- After acceptance, the project secretary owns target refresh, bounded worker
  reconciliation, verification, `ff-only` target integration, completion,
  retirement, and cleanup. Target and final commit OIDs are refreshed state.
- The original worker may resolve ordinary target drift only within the
  accepted scope, rerun verification, and submit a new ready-review packet.
- Escalate only material ambiguity, scope expansion, failed checks requiring
  judgment, a dirty target, or a genuinely new capability.
- Do not push, open a PR, delete branches, or clean up a worker as a substitute
  for the brokered acceptance and integration records.
