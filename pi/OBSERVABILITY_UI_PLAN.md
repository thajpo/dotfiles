# Observability UI implementation plan

## Goal

Let the operator verify the task mental model without expanding the entire
conversation with `Ctrl+O`.

The Inspector is observability, not a completion throttle. Spawned agents have
no automatic elapsed-time, assistant-turn, provider-token, or tool-call limit;
contracts, authority boundaries, acceptance evidence, and explicit user stop
remain the anti-slop controls.

The first UI must make these facts easy to inspect:

- the active task packet;
- the exact brief/instruction sent to each child agent;
- the context sources included in that brief;
- each child’s status, current tool, progress, and result;
- parent/child messages, failures, and attention signals.

Show explicit instructions and messages, not provider-private hidden
chain-of-thought. Keep the view read-only in the first slice.

## UX contract

Add a configurable `Ctrl+I`-style inspector command/key. Opening it shows a
compact overlay; `Ctrl+O` remains unchanged.

Tabs:

- **Task** — mode, learning level, goal/current slice, constraints, decisions,
  open decisions, acceptance, and packet generation.
- **Fleet** — every child in a compact list: agent, model, status, elapsed time,
  current tool/path, step, and attention/error state.
- **Messages** — selected child’s parent assignment, progress messages, result,
  failure, and steering/intercom messages.

Interaction:

- `j/k` or `Tab`/`Shift-Tab`: select the next/previous child;
- number keys: jump to a visible child;
- `a`: interleaved all-agent message stream;
- `v`: full read-only selected-session transcript;
- `Esc`: close the inspector.

The fleet list always shows all agents; only the detailed message pane is
single-selection. A small statusline summary may show packet state and active
agent count without replacing the inspector.

## Implementation slices

### 1. Define a small observation model

Create a host-side, read-only observation state with bounded records:

- `task.packet.changed/cleared`;
- `agent.started/updated/completed/failed/paused`;
- `agent.instruction`;
- `agent.message`;
- `tool.started/ended/error`;
- `attention` and `compaction` markers.

Use stable IDs, parent/child IDs, timestamps, model identity, status, and
bounded text previews. Do not put this state in the project repository.

### 2. Reuse existing evidence first

Use the current substrate before adding new instrumentation:

- workflow-state session entries for the active task packet;
- pi-subagents `status.json`, `events.jsonl`, child session files, results, and
  existing fleet state for child status;
- existing subagent result/intercom events for messages;
- context-audit manifests for context source summaries when enabled.

Add capture only where an exact child brief or live message is not already
available. Do not infer a mental model from status text if the actual brief can
be recorded at spawn time.

### 3. Build the read-only inspector

Add a dedicated observability extension/component that:

- keeps a bounded in-memory projection for fast rendering;
- refreshes from persisted artifacts/events after resume;
- exposes the Task/Fleet/Messages tabs;
- supports selection and keyboard navigation;
- renders compact previews and explicit “open transcript” actions;
- remains safe when an artifact is missing, malformed, stale, or still being
  written.

The host TUI owns this view; it should not require exposing session files to the
Docker task container.

### 4. Add activation and statusline integration

- Register the extension through the existing dotfiles install path.
- Add a configurable inspector shortcut and `/observe` fallback command.
- Add only a compact optional statusline segment, such as
  `packet BUILD · agents 1/2 running`.
- Preserve `Ctrl+O` behavior and normal model/tool semantics when the inspector
  is closed or disabled.

### 5. Validate with deterministic fixtures

Add tests for:

- packet replace/clear and resume reconstruction;
- one child, parallel children, nested children, completion, failure, pause,
  and attention;
- exact instruction capture and parent/child ordering;
- malformed or truncated event/session files;
- selection cycling and all-agent interleaving;
- no secret/raw system-prompt leakage in compact mode;
- no project-file or task-container writes from the inspector.

Use a fake provider/subagent fixture for event ordering. Add a manual acceptance
scenario with two real child agents to verify the operator can identify:

1. what each child was instructed to do;
2. what context/boundaries it received;
3. what it is doing now;
4. what it reported back;
5. whether the parent synthesized the result correctly.

## Rollout order

1. Task packet + fleet list.
2. Selected-agent instruction/result view.
3. Interleaved messages and transcript drill-down.
4. Tool/retry/compaction timeline.
5. Optional pause/stop/steer controls, kept separate from read-only viewing.

The outside-project-root Pi docs mount is a separate security workstream; do not
couple its activation to this UI.
