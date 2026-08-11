# Harness feedback

Status: source feature retained as a separate user-scoped improvement feed;
its own greenfield installed-process delivery remains to be accepted.

Harness feedback is an encouraged self-improvement channel for every Pi
subagent: workers, reviewers, scouts, researchers, planners, delegates,
secretary investigators, and headful workstream workers.

## When to send it

Send one bounded feedback item when the work reveals a useful harness
observation, such as:

- a missing capability, tool, context item, or approval route;
- an unsafe, surprising, or ambiguous default;
- repeated ceremony, friction, or wasted effort;
- an observability, recovery, testing, or documentation gap;
- a concrete improvement that would make the next incorrect implementation
  fail sooner or make a correct implementation easier.

Do not send routine completion status, generic praise, speculative complaints,
or secrets. No useful observation is also a valid outcome; feedback is
encouraged, not a quota.

## Submission protocol

Every subagent has the direct `harness_feedback` tool. For a non-blocking
observation, use it once with a bounded form:

```text
harness_feedback({
  kind: "harness-improvement",
  title: "Short concrete title",
  evidence: ["Observed ..."],
  recommendation: "Change ...",
  decision_needed: false
})
```

The tool writes directly to the central Pi feed, including project and
workstream provenance when the launcher supplies it. Normal child agents may
also use the parent-scoped native channel:

```text
contact_supervisor({
  reason: "progress_update",
  message: 'AGENT_FEEDBACK {"schema":"agent-feedback.v1","kind":"harness-improvement","title":"Short concrete title","evidence":["Observed ..."],"recommendation":"Change ...","decision_needed":false}'
})
```

The update is non-blocking. Do not wait for a reply. If the observation blocks
the task, use the structured `interview_request` form instead. Feedback is a
proposal and never grants authority or silently changes memory, product scope,
or repository policy. The direct tool and native channel use the same central
record format; use one path per observation rather than duplicating it.

In the greenfield product, headful workers send the same bounded observation
through the controller-bound project-message path. This is feedback delivery,
not lifecycle authority or permission to change project state.

## Logging and review

Every project feeds one central Pi-owned feedback store. Pi automatically
stores each bounded normalized record in:

```text
~/.pi/agent/feedback/records/
```

The store is intentionally outside project worktrees: raw prompts can contain
sensitive material, and subagents must not mutate a project repository merely
to report an observation. Raw prompt/interview content is omitted unless
`PI_AGENT_FEEDBACK_RAW=1` is explicitly enabled.

Review all projects, or narrow the central feed to this repository:

```bash
pi-harness-feedback
pi-harness-feedback --repository ~/dotfiles
pi-harness-feedback --repository ~/dotfiles --format markdown --include-reviewed
```

The command emits normalized records only. To create an explicit sanitized
review snapshot inside the repository, use an output path deliberately:

```bash
pi-harness-feedback --repository ~/dotfiles --format markdown \
  --output pi/HARNESS_FEEDBACK_LOG.md
```

The snapshot is a review artifact, not an automatic commit. The parent or
secretary decides whether feedback is accepted, rejected, deferred, or worth
turning into a test, invariant, diagnostic, instruction, or code change. The
central feed remains the source of truth across all projects; repository
filtering is only a review view.

For native child feedback, the parent may record a disposition with
`subagent_supervisor({ action: "review", feedbackId, outcome: "accepted|rejected|deferred" })`.
