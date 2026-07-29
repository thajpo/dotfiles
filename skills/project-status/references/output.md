# Synthesis format

Use this order unless the user asks for another format:

## Scope and visibility

State the repository/checkout, time window, explicit target, evidence sources,
and unavailable views. Say when no explicit target was provided.

## What is observed

- **Observed facts:** command-backed state, refs, worktree locations, changed
  paths, and test or document evidence.
- **Explicit statements:** goals or intent quoted/paraphrased from attributed
  commit messages, issues, plans, or notes.

Keep these categories separate. A clean tree, old tip, merged ref, or missing
worktree is not a completion or abandonment decision.

## Direction and target fit

Explain **inference:** what recent work appears to be moving toward and whether
there is evidence of inclusion in the explicit target. If no target or
comparison evidence exists, say that target fit is unknown.

## Attempts and next actions

For each relevant branch, worktree, or session, describe it as an attempt or
location, then provide:

- evidence worth inspecting or resuming;
- observed blockers and missing visibility;
- one bounded next action, without performing it.

## Gaps and human questions

List evidence-backed gaps and tentative directions. Ask only questions whose
answers would change prioritization, target interpretation, ownership, or the
next action. Avoid health scores, completion percentages, and unsupported
rankings.
