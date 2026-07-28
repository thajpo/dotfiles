---
description: Debug by evidence before changing code
argument-hint: "<failure>"
---
Debug this failure using an evidence-first sequence: $@

1. Reproduce reliably.
2. Capture bounded logs, stack traces, environment facts, and a minimal failing case.
3. Classify the failure.
4. Identify the likely violated invariant.
5. Run the smallest discriminating diagnostic experiment.
6. Fix only after evidence selects a cause.
7. Add focused regression coverage and rerun relevant checks.

Do not shotgun-edit, suppress errors, or broaden scope without escalation.
