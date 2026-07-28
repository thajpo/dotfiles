---
description: Read-only repository scouting and model-route recommendation
argument-hint: "<task>"
---
Scout this task without changing files: $@

Use only read-only inspection. Map the smallest relevant surface, symbols, call paths, interfaces, data flow, tests, ownership boundaries, and verification commands. Identify contradictory behavior, undocumented invariants, and consequential decisions that remain unresolved. Do not write an implementation essay.

Return compact YAML:
```yaml
surfaces:
  files: []
  symbols: []
  interfaces: []
  tests: []
uncertainty:
  unresolved_decisions: []
  undocumented_invariants: []
risk:
  public_api: false
  schema: false
  concurrency: false
  numerical: false
  weak_tests: false
verification:
  commands: []
recommended_route:
  model: flash|luna|sol
  reason: ""
```
