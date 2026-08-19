---
name: work-modes
description: "Select a work mode from uncertainty and consequence, with an independent OFF/LIGHT/DEEP learning overlay."
---

# Work Modes

Use one work mode for the task and one independent learning level. Choose the
smallest process that makes the next important mistake cheap and visible.

## Work mode selection

- **FAST** — the desired result and affected code are clear, verification is
  strong, and mistakes are cheap to reverse.
- **RIP** — the task is an experiment, diagnosis, reproduction, profiling,
  discovery, or other uncertain investigation. Preserve evidence and separate
  measurements, observations, inferences, and speculation. Do not present
  exploratory code as production-ready without a later BUILD step.
- **BUILD** — the desired behavior is reasonably clear, but the change is
  consequential or crosses meaningful boundaries. Map affected surfaces,
  implement one coherent solution, and verify independently.
- **MAJOR** — the desired result or architecture is materially uncertain and
  consequential, cross-system, or dependent on staged decisions. Split the
  program into independently testable slices; after each slice, integrate,
  verify, and replace obsolete understanding. Move stabilized slices to BUILD
  or FAST.

Use these direct mappings when they fit:

- clear and reversible → FAST;
- unclear and cheap to experiment → RIP;
- clear but consequential → BUILD;
- unclear and consequential or cross-system → MAJOR.

The user may override a selected mode naturally. Do not ask the user to
classify routine work. State a mode briefly when it improves orientation or
explains a meaningful transition.

## Learning overlay

Learning is independent of work mode:

- **OFF** — commodity work, automated queues, explicit result-only requests,
  or tasks where explanation adds no value. Leave reproducible evidence.
- **LIGHT** — the default for relevant personal engineering. At completion,
  briefly report the most important design decision, the most surprising
  finding, the evidence that resolved it, and one code, trace, or test worth
  inspecting.
- **DEEP** — only when explicitly requested or central to the user's intended
  expertise. Before investigation, invite a short prediction about affected
  surfaces, invariants, likely failure, and disconfirming evidence. Give the
  user ownership of one consequential seam; finish with a reverse design review
  covering the critical path, main choice, failure modes, test evidence, and
  remaining inspection risk.

Do not make learning level automatic from task size, and do not turn OFF into a
quiz.
