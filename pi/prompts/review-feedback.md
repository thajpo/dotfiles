---
description: Turn exported human review comments into verified repairs
argument-hint: "<export-or-path>"
---
Process this exported human review: $@

Convert every line/range comment into a checklist. For each item report exactly one state: `ADDRESSED` with location, `DECLINED` with reason, or `ESCALATED` with the decision needed. Make only relevant repairs, rerun focused verification, and provide a fresh diff summary. Do not treat praise as a change request or make unrelated cleanup.
