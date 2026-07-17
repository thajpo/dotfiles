# Agent Instructions

These are global defaults. Project-level `AGENTS.md` files override these when
they are more specific.

## Personal Context

- Use git liberally.
- The user has coding experience but is not yet a software engineer.
- The user's goal is to become an ML reliability engineer.
- The user enjoys theory, explanations, architecture design, and systems thinking.
- The user's low-level computing background is weak; explain relevant OS,
  networking, memory, process, shell, and Git details when they matter.

## Default Engineering Workflow

- Prefer an agent-agnostic workflow. Use `AGENTS.md` as the canonical project
  memory file. If a project needs Claude compatibility, make `CLAUDE.md` a
  symlink to `AGENTS.md` instead of maintaining two divergent files.
- If a harness-specific choice is needed, use OpenCode first.
- Keep global memory short. Store durable project knowledge in the project's
  `AGENTS.md`; move conditional or task-specific procedures into skills.
- Do not install random skills or agent tools from the internet unless the user
  explicitly approves the source and purpose. Popularity is not evidence of
  quality or safety.

## Planning

- For complex product, architecture, UX, multi-file, or ambiguous work, plan
  before coding.
- A good plan should make decisions explicit, identify acceptance criteria, and
  separate reversible implementation detail from product or architecture calls.
- For small, obvious fixes, skip heavy planning and keep momentum.

## Implementation

- Before changing code, inspect the repository shape and existing conventions.
- Use `rg`/`rg --files` for search.
- Prefer structured APIs and project-local patterns over ad hoc parsing or new
  abstractions.
- For parallel work or long-running agent sessions, isolate each task in a git
  worktree. If `treehouse` is available, use it instead of manually juggling
  worktrees.
- Never run two independent coding agents in the same dirty checkout.

## Validation

- When fixing a bug, start by reproducing it as close as possible to how an end
  user experiences it. Unit tests alone are often not enough.
- After implementation, run the most relevant checks: end-to-end behavior,
  focused tests, lint/typecheck, then broader tests as risk grows.
- For PR-bound work, prefer the `no-mistakes` gate when it is available. The
  default path is: commit the branch, then run `git push no-mistakes` or ask the
  agent to use `/no-mistakes`.
- Evidence matters. Record screenshots, logs, test output, or other proof that
  the change satisfies the original intent.

## Long-Running Work

- Use bounded autonomous loops only for objectives with clear stop conditions,
  measurable metrics, or trusted judgment domains.
- If `gnhf` is available, use explicit caps such as `--max-iterations` and
  `--max-tokens`.
- Each successful iteration should leave a commit or clear report so the user
  can inspect, cherry-pick, or revert work.

## Multi-Agent Orchestration

- For one or two simple tasks, direct OpenCode sessions in isolated worktrees are
  enough.
- For multiple parallel tasks, multi-repo work, or PR babysitting, use Firstmate
  when the repo exists locally. Launch OpenCode inside that repo, preferably from
  within tmux.
- The human should spend most attention at the beginning of work, where intent is
  clarified, and at the end, where evidence and risk are reviewed.

## Learning Loop

- When an agent makes a mistake, turn the correction into durable memory:
  project convention, test instruction, architecture note, or a skill.
- Avoid bloating `AGENTS.md` with rarely used procedures. Keep always-on context
  small and move conditional workflows into skills.
- Explain tradeoffs in a way that teaches the underlying system, not just the
  immediate command.
