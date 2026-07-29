---
name: project-status
description: "Read-only repository status and direction synthesis from Git history, branches, worktrees, and visible project evidence. Use for requests such as what the project or repository state is, what worktrees or branches can be resumed, what recent work has been moving toward, suggestions based on recent work, or using Git history to look for gaps."
---

# Project Status

Use this skill as a read-only project lens. Describe the work in terms of **intent,
attempts, evidence, explicit target, next action, unknowns, and human questions**;
branches, sessions, and worktrees are attempts or locations, not the work itself.

## Choose the investigation

- For a **broad state request**, gather a bounded fresh-context fanout when Pi
  agent fanout is available. Use at most three read-only investigators with
  distinct angles: recent history and intent; attempts and worktrees; gaps and
  directions. The parent synthesizes their evidence; do not ask children to
  edit, clean up, or decide completion.
- For a **targeted request**, investigate only the named branch, worktree,
  interval, question, or evidence source. Do not run the broad fanout by
  default.
- Read [references/investigation.md](references/investigation.md) for bounded
  commands and investigator prompts. Read
  [references/output.md](references/output.md) when producing the final
  synthesis.

## Read-only workflow

1. Restate the question and the time or comparison window. Identify the
   explicit target, if one exists (issue, plan, milestone, branch, or user
   goal). If no target is explicit, say so.
2. Inspect only visible, relevant evidence: current checkout status and
   identity, recent commits and changed paths, refs and upstreams, worktree
   listings, and explicitly named project documents. Treat session transcripts
   as out of scope unless the user names an accessible source.
3. Label every conclusion as one of:
   - **Observed fact** — directly shown by a command or file.
   - **Explicit statement** — written by a person in a commit, issue, plan, or
     project document.
   - **Inference** — a tentative interpretation supported by cited evidence.
4. Synthesize what appears to be moving toward the target, what attempts have
   evidence worth resuming, and the smallest evidence-backed next actions.
   Present gaps and directions as hypotheses, not commitments.
5. State visibility limits, including inaccessible sibling worktrees, sessions,
   private remotes, shallow history, missing documents, and unavailable agents.
   A missing view is unknown—not evidence that no work exists.

## Guardrails

Never mutate Git or runtime state. Do not checkout, switch, reset, rebase,
merge, commit, fetch, push, stash, clean, create/delete branches, or
create/remove worktrees. Do not install or activate anything. Do not infer
completion or abandonment from cleanliness, inactivity, age, or merge status;
those are observations only. Never invent percentages. Do not crawl unrelated
session transcripts by default. Do not call an attempt “the work” or treat a location as
proof of intent.

A branch or worktree may be described as a **candidate to inspect or resume**
only with its observable evidence and blockers. Do not promise that it is
resumable. Compare work to an explicit target when one is available; otherwise
report that target inclusion cannot be established. Ask focused human
questions when intent, ownership, target, or authority is unresolved.

Return a concise synthesis with cited commands or paths, separating facts,
statements, and inferences. Do not hide uncertainty behind a single health
label.
