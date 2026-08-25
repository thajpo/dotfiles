---
name: pr-scope-guard
description: "Enforce issue-approved file touch scope in PRs and block out-of-scope changes unless the user explicitly approves scope expansion."
---

# PR Scope Guard

> Pisec routing: this skill is for generic GitHub workflows only; do not use it for a Pisec-managed workstream. Use the Pisec Secretary and brokered acceptance path instead.

## Usage
- Internal helper skill.
- Normally invoked by `$pr-iterate`, not directly by user.

## Goal
Prevent silent scope creep by enforcing file-touch boundaries from the approved issue/spec.

## Inputs
- issue/spec `file touch scope`
- PR diff changed files
- base branch

## Procedure
1. Extract allowed scope globs from issue/spec.
2. Compute changed files in PR.
3. Compare changed files against allowed scope.
4. If violations exist, stop and escalate one explicit approval question.
5. Continue only after explicit user approval evidence is recorded.

## Approval Rule
Out-of-scope files are a workflow failure until user explicitly approves expansion.

## Output
- allowed files touched
- out-of-scope files (if any)
- escalation question or approval evidence link

## References
- `references/scope-check-commands.md`
- `references/scope-approval-template.md`
