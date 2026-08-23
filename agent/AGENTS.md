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
