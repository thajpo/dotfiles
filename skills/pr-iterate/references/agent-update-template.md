# Agent Update Template

Preferred usage:
- Keep one primary rolling `Agent Update` comment on the PR and update it each push.
- Use additional comments only when needed to answer specific reviewer threads/questions.

```md
## Agent Update
- Status: in_progress | blocked | ready_for_review
- Round: <n>
- Feedback ingestion:
  - PR comments: <count>
  - Mentions/direct requests: <count>
  - Review summaries: <count>
  - Inline comments: <count>
  - Review threads: <count>
  - Unresolved threads: <count>
- Spec context:
  - Spec id/title: <id or title>
  - Intended behavior change: <short summary>
  - Scope addressed in this push: <short summary>
- Implementation context:
  - What changed:
    - <3-6 concise bullets>
  - Why this approach: <short rationale>
  - Files touched: <short list or "no new files">
- Validation:
  - Commands run: <short list>
  - Tests: <pass/fail + counts>
  - Lint: <pass/fail>
  - Typecheck: <pass/fail/not run>
- Failures (if any):
  - <failing test id>: <one-line reason>
- Remaining open items:
- Risks/assumptions:

<!-- Rules: no raw command output, no pasted stack traces, no full logs. -->
```
