# Greenfield Workflow Acceptance

This document covers the agent-engineering behavior layered on top of the fresh
Pi runtime. System release acceptance remains defined by
[`control-plane/ACCEPTANCE_PLAN.md`](control-plane/ACCEPTANCE_PLAN.md).

## Preconditions

Run these scenarios only through an installed greenfield conversation with an
exact controller project, conversation, working copy, run, and session file.
Repository-source imports, old Pi launchers, help output, and direct database
seeding are not live workflow evidence.

## Work Modes

- **FAST:** clear, reversible work; one implementation path, independent
  verification, and final diff inspection.
- **RIP:** uncertain diagnosis or experiment; distinguish measurements,
  observations, inferences, and speculation.
- **BUILD:** consequential but reasonably clear work; map impact, implement one
  production path, and review the surviving diff.
- **MAJOR:** uncertain, cross-system work; maintain a current program map and
  deliver independently accepted slices.

Exercise transitions between these modes when evidence changes the uncertainty
or consequence classification.

## Learning Levels

- **OFF:** result and reproducible evidence only.
- **LIGHT:** important decision, surprising finding, decisive evidence, and one
  inspection target.
- **DEEP:** prediction, user-owned design seam, mental-model comparison, and
  reverse design review.

Learning level is independent of work mode.

## Context Boundary

Children start with fresh context unless a fork is explicitly required. Their
brief contains the current goal or slice, accepted decisions, relevant
instructions and boundaries, acceptance evidence, and stop conditions. It does
not contain the complete parent transcript, unrelated reports, obsolete plans,
or raw logs.

Detailed child sessions and artifacts stay in user-scoped controller/session
state rather than project repositories. The parent receives compact results and
pulls detailed evidence only when needed.

## Required Scenarios

1. FAST bug fix with focused tests and no planning ceremony.
2. RIP diagnosis with instrumentation and a discriminating next experiment.
3. BUILD feature with impact mapping, one writer, fresh review, and integration
   checks.
4. MAJOR program with one slice changing mode as evidence evolves.
5. OFF, LIGHT, and DEEP learning behavior.
6. Fresh-context child brief and prohibited-context inspection.
7. Read-only investigator access that cannot mutate the assigned project.
8. One-writer coding run with independent final-diff verification.
9. Multiple independent candidates followed by a newly tested integration.
10. Task packet replacement, compaction, stop, and exact-session resume.
11. BTW remaining separate until an explicit result is injected.
12. Harness feedback recorded without changing project state or authority.

## Evidence

Record the controller IDs, installed build, model, tool schema, explicit child
brief, actual changed paths, commands and exits, session continuity, and final
diff. Model behavior is evaluation evidence; rerun ambiguous outcomes before
changing policy.

Live workflow status remains **INCOMPLETE** until all applicable scenarios have
evidence from the accepted installed system.
