---
name: repo-init
description: "Bootstrap a repo with lean planning, issue/PR workflow contracts, worktree policy, and baseline GitHub PR checks."
---

# Repo Init

## Goal
Create a zero-setup baseline so each repo is immediately compatible with lean spec -> issue -> PR workflows.

## Ownership Boundary
- This skill scaffolds and aligns repository files.
- Canonical markdown policy is owned by `$lean-flow`.
- Runtime markdown policy enforcement is handled by `$pr-iterate`.

## Outputs
- planning scaffold (`current.md` sections)
- workflow policy scaffold (`AGENTS.md` alignment)
- README alignment note (`README.md` kept as product-facing usage doc, linked to workflow docs)
- issue + PR templates
- baseline GitHub Actions for `lint` and `test`
- worktree policy notes

## Procedure
1. Verify repo root and existing docs (`README.md`, `AGENTS.md`, planning markdown).
2. Ask user to confirm repo type (ML or non-ML) when initializing if unclear.
3. Align repo docs to markdown policy defined in `$lean-flow`.
4. Add missing planning sections:
- `Institutional Knowledge`
- `Beliefs`
- `Brainstormed`
- `Specd`
5. Ensure policy enforces:
- `Specd` requires explicit `user intent`.
- ready item must be converted to one GitHub issue before implementation.
- issue defines file-touch scope.
- one issue -> one branch -> one worktree -> one PR.
6. Add/update:
- `AGENTS.md` (workflow contract source)
- `README.md` (quickstart remains accurate and points to workflow docs)
- `.github/ISSUE_TEMPLATE/spec-contract.md`
- `.github/pull_request_template.md`
- `.github/workflows/pr-checks.yml`
7. Return setup report with created/updated files and remaining manual steps.

## Guardrails
- Do not overwrite existing custom templates blindly; merge intent-preserving edits.
- Do not delete or replace `README.md`; preserve repo-specific usage/context.
- Treat `AGENTS.md` as a protected contract doc: merge updates, keep stricter existing rules.
- Do not remove extra markdown files without explicit user instruction when content ownership is unclear.
- If required tools are missing (`gh`, language toolchain), report blockers clearly.
- Keep contracts strict and fail-fast.

## References
- `references/issue-template.md`
- `references/pr-template.md`
- `references/pr-checks-workflow.yml`
