# Pi Harness Observability Contract

Status: the base greenfield installed-process driver is accepted; Inspector
loading and the metrics and trace planes remain incomplete.

## Implemented Inspector Contract

The read-only Inspector exposes Task, Fleet, and Messages views through
`/observe` and `Ctrl+I`. It shows the active task packet, explicit child briefs,
bounded context-source summaries, child status/current tool/progress, results,
failures, and parent-child messages. It does not expose hidden reasoning.

The view supports bounded selection and transcript drill-down, tolerates
missing or malformed artifacts, and does not write project files. Source-level
tests cover projection and rendering, but greenfield acceptance still requires
loading the Inspector in a real installed controller-bound Pi process.

This document defines the observability boundary for a custom Pi build. The
purpose is to make the harness itself understandable and measurable before
optimizing prompts, models, tools, or workflows. Later evaluation datasets may
consume these records, but model training is not the design center.

## 1. Goal

The harness should answer, for every meaningful run:

- What did the user ask, and what task/run boundary did we assign?
- Which model/provider/transport actually ran?
- How much time was spent queued, waiting, in the provider, in tools, in the
  UI, in serialization, and in shutdown/drain?
- What tokens, cache reads/writes, reasoning tokens, and cost did the provider
  report?
- Which tools ran, with which arguments, results, errors, durations, and
  truncation behavior?
- Which files did the model request to read or modify, and what bytes/lines
  were actually handled?
- What context was sent, what caused its growth, and when did compaction occur?
- Which retries, interruptions, watchdogs, or subagent boundaries occurred?
- Did the run complete, fail, timeout, abort, wedge, or terminate without a
  durable terminal record?

A metric without provenance is not sufficient. Every derived number must say
whether it is provider-reported, locally measured, or estimated.

## 2. Model freedom and anti-slop

Observability must not become a hidden completion throttle. Spawned agents have
no automatic elapsed-time, assistant-turn, provider-token, or tool-call limit.
The harness records observed duration, turns, tokens, and tool calls but does not
use those measurements as arbitrary stop conditions. Explicit user interrupt or
stop remains available.

The constraint is semantic: task packets, contracts, authority/tool boundaries,
acceptance criteria, and durable evidence should make slop visible and rejectable
without cutting off a model that is still making useful progress. The Inspector
shows those explicit instructions and outcomes; it never exposes hidden
chain-of-thought.

## 3. Non-goals

This phase does not:

- optimize a model or prompt automatically;
- create an RL pipeline;
- export prompts, files, or provider payloads remotely by default;
- change Pi task semantics when diagnostics are disabled;
- treat token count as a quality metric by itself;
- claim that model-reported cost is an invoice or billing authority.

## 4. Three data planes

Keep these separate so raw evidence can be retained or discarded without
changing metric definitions.

### 3.1 Raw evidence

Append-only local records of lifecycle observations. Examples:

- run/turn/attempt start and end;
- provider request/response metadata;
- tool start/update/end;
- filesystem operation start/end;
- queue, retry, compaction, abort, and shutdown events;
- terminal outcome and uncaught-process marker.

Raw evidence should be bounded per record and written crash-safely. Content is
redacted or omitted unless an explicit debug/trace mode enables it.

### 3.2 Normalized events

Versioned records with stable names and fields, independent of Pi's internal
event spelling. These are the compatibility layer for reports and exporters.

Minimum envelope:

```json
{
  "schema_version": 1,
  "event_id": "...",
  "event_kind": "tool.end",
  "wall_time": "2026-08-03T00:00:00.000Z",
  "monotonic_ms": 12345.67,
  "trace_id": "...",
  "session_id": "...",
  "task_id": "...",
  "run_id": "...",
  "turn_id": "...",
  "attempt_id": "...",
  "parent_id": "...",
  "status": "ok"
}
```

### 3.3 Derived metrics

Reports and rollups are computed from normalized events, not emitted as the
only source of truth. Store the query/report version used to derive them.

Examples:

- `task.wall_ms`
- `task.active_ms`
- `task.provider_ms`
- `task.tool_ms`
- `task.queue_ms`
- `task.ui_drain_ms`
- `task.input_tokens`
- `task.output_tokens`
- `task.cache_read_tokens`
- `task.cache_write_tokens`
- `task.reasoning_tokens`
- `task.cost_reported`
- `task.tool_calls`
- `task.file_reads_requested`
- `task.file_bytes_returned`
- `task.compactions`
- `task.retries`
- `task.terminal_status`

## 5. Correlation model

Pi's session ID is not enough for custom diagnostics. Use explicit IDs:

- `trace_id`: one end-to-end user-visible operation, including retries and
  subagents;
- `session_id`: persisted Pi conversation;
- `task_id`: the user/workflow-defined unit being evaluated;
- `run_id`: one agent run under a task;
- `turn_id`: one model response plus its tool batch;
- `attempt_id`: one provider request attempt, including retries;
- `tool_call_id`: the model's tool call identity;
- `subagent_run_id`: child run identity, linked through `parent_id`;
- `event_id`: unique event identity for deduplication.

Task boundaries should be explicit where possible. Prompt inference can be a
fallback, but inferred task boundaries must be marked as inferred rather than
presented as fact.

## 6. Event taxonomy

### Run and task

```text
run.start
run.accepted
run.settled
run.end
run.failure
run.abort
run.timeout
run.incomplete

task.start
task.end
task.label
subagent.start
subagent.end
```

### Model/provider

```text
provider.request.start
provider.request.headers
provider.request.payload_hash
provider.response.headers
provider.stream.first_token
provider.stream.update
provider.request.end
provider.attempt.failure
provider.retry.scheduled
provider.retry.started
provider.retry.exhausted
```

Payloads and headers are metadata-only by default. API keys, authorization
headers, prompts, tool definitions, and raw completions are sensitive.

### Turns and tools

```text
turn.start
turn.context_built
turn.end
message.persisted

tool.start
tool.update
tool.end
tool.error
tool.blocked
tool.cancelled
```

Each tool event should carry tool name, call ID, start/end time, status,
argument/result byte counts, truncation flags, and an optional redacted preview
or external local-content reference.

### Filesystem

```text
file.read.request
file.read.start
file.read.end
file.write.start
file.write.end
file.edit.start
file.edit.end
```

A `file.read` event must distinguish:

1. **requested read**: the model asked Pi's `read`/`grep`/`find`/`ls` tool to
   inspect a path;
2. **built-in operation**: Pi's filesystem operation actually opened/read the
   path;
3. **arbitrary process access**: a shell/custom tool may have read files that
   Pi cannot semantically observe without OS-level tracing.

Do not claim complete filesystem coverage from model tool calls alone.

Recommended file fields:

```text
path_display
path_hash
path_scope (workspace / outside / unknown)
line_start / line_limit
bytes_read / bytes_returned
result_lines
truncated
mime_or_kind
content_hash (optional, local-only)
duration_ms
error
```

### Context and UX

```text
context.build.start
context.build.end
context.usage.sample
compaction.start
compaction.end
queue.enter
queue.leave
serialization.start
serialization.end
stdout.write.start
stdout.write.end
ui.render.start
ui.render.end
```

User-visible latency must not be conflated with provider latency. At minimum,
report:

```text
submit_to_first_visible_output
submit_to_first_model_byte
submit_to_first_tool_start
submit_to_final_model_output
submit_to_settled
provider_time
tool_time
queue_time
compaction_time
serialization_time
stdout_drain_time
```

## 7. Usage and cache semantics

Each usage field must carry provenance:

```json
{
  "value": 123,
  "source": "provider | local_estimate | unavailable",
  "field_present": true
}
```

Track separately:

- input tokens;
- output tokens;
- cache-read tokens;
- cache-write tokens;
- reasoning/thinking tokens when reported;
- total tokens;
- reported cost by category;
- provider/model/response model;
- prompt-cache hit/miss evidence;
- context-token estimate before the request.

Do not silently turn missing provider usage into zero. `0` and `unavailable`
are different observations.

For retries, preserve both:

- one logical generation record for the task/turn;
- one physical attempt record per provider request.

Rollups must avoid double-counting unless the report explicitly asks for
physical spend.

## 8. Failure and incompleteness

A run is not successful merely because the process exited zero. Terminal status
must distinguish:

```text
completed
provider_error
tool_error
acceptance_rejected
cancelled
interrupted
timeout
watchdog_timeout
compaction_failure
transport_failure
process_crash
serialization_failure
incomplete_no_terminal_event
```

Flush a terminal diagnostic record on:

- normal settlement;
- explicit abort;
- provider error;
- tool error;
- retry exhaustion;
- compaction failure;
- process shutdown;
- uncaught exception;
- watchdog termination.

Use a small launch-time or parent-side journal so a process that dies before
Pi persists its final session message still leaves an incomplete-run marker.

## 9. Storage and privacy modes

Recommended modes:

### Off

No diagnostic persistence beyond normal Pi session behavior.

### Metrics

Local append-only JSONL or SQLite containing IDs, timestamps, counts, sizes,
statuses, hashes, and usage. No prompts, file contents, or raw tool output.

### Debug

Metrics plus bounded, redacted previews of tool arguments/results, provider
errors, context summaries, and file metadata.

### Trace

Explicit local-only full evidence capture, with size limits, redaction, and
retention controls. Remote exporters are separate opt-in adapters.

Retention should support:

- per-run maximum bytes;
- per-session maximum bytes;
- rolling files or partitions;
- flush-on-failure and flush-on-exit;
- atomic append/rename behavior;
- permissions restricted to the current user;
- a report showing what was dropped or redacted.

Never capture raw authorization headers or API keys. Treat prompts, system
prompts, tool schemas, arguments, results, file paths, stderr, images, and
provider payloads as sensitive unless the user explicitly chooses trace mode.

## 10. Current Pi substrate and gaps

Upstream Pi already supplies the hooks and session data needed for this design:

- session JSONL with messages, tool calls/results, bash records, and usage;
- `--mode json` lifecycle events;
- SDK `session.subscribe()`;
- extension hooks for turns, tools, providers, retries, and compaction;
- compaction file-operation summaries;
- context-usage access;
- provider response metadata hooks.

The greenfield target additionally has:

- controller project, conversation, working-copy, and run identities;
- controller-selected Pi session JSONL;
- workflow task packets and optional context-audit manifests;
- subagent status, events, transcripts, results, and artifacts;
- worker/subagent parent-child relationships.

Important gaps remain:

- no normalized parent-run trace envelope;
- no durable terminal marker for every normal-process failure/crash;
- no standard per-tool duration/byte/file event;
- arbitrary `bash` filesystem access is not semantically observable;
- cache/usage provenance is inconsistent across some subagent paths;
- no task-level rollup joining parent, child, provider, tool, and UX time;
- no query/report layer for comparing task attempts.

## 11. Minimum vertical slice

Implement one diagnostic slice before broad instrumentation:

1. Assign `trace_id`, `task_id`, `run_id`, and `turn_id` at prompt acceptance.
2. Record run start and an immediate launch marker.
3. Subscribe to provider/model, turn, tool, retry, compaction, and terminal
   events.
4. Record provider-reported usage and local monotonic timestamps.
5. Record tool name, call ID, status, duration, and redacted argument hash.
6. Write a terminal event on success, error, abort, timeout, or shutdown.
7. Provide a CLI report for one task showing latency decomposition, token/cache
   totals, tool counts, failures, and evidence provenance.
8. Validate with a deterministic fake provider and one deliberately failing
   tool.

Only after this slice is reliable should file byte accounting, subagents,
provider payload metadata, and exporters be added.

## 12. Validation matrix

The disposable matrix should cover:

- text-only success;
- one tool success;
- parallel tools;
- tool error;
- provider stream error;
- provider timeout;
- retry then success;
- retry exhaustion;
- compaction then retry;
- user abort during provider stream;
- user abort during tool execution;
- process kill before terminal event;
- subagent success;
- subagent child failure;
- async runner startup failure;
- slow stdout/RPC consumer;
- large streamed tool-call payload.

For each case, compare the session JSONL, JSON events, diagnostic journal,
subagent artifacts, exit status, and final report. The contract is not met if a
known failure can only be inferred from silence.

## 13. Open decisions

Before implementation, choose:

- whether `metrics` is enabled for every custom-build run or only by flag;
- the task-boundary API (`/task`, extension, workflow packet, or explicit CLI
  run ID);
- JSONL, SQLite, or both as the local store;
- whether paths are stored plainly, hashed, or both in local-only mode;
- whether arbitrary bash file access is out of scope or requires OS tracing;
- the maximum retention and content-preview budgets;
- whether provider payload metadata is stored locally;
- which TUI surfaces are required in the first vertical slice.

## 14. Red-team decision

Fresh independent reviews converged on a conditional **yes**:

- This is worthwhile because the owner needs causal control over a custom
  harness, not generic analytics. A correlated trace can distinguish slow model
  work from queueing, tools, compaction, serialization, and subagent behavior.
- It is not worthwhile as an unconstrained fork, telemetry platform, dashboard,
  or RL project. Existing Pi sessions and events should remain the substrate;
  the first layer should be an adapter/plugin over stable public boundaries.
- Fork or patch Pi core only when a concrete unanswered diagnostic question
  cannot be answered through the public SDK/extension surface.

The first experiment should answer only these questions:

1. Why was this turn slow?
2. What did context growth, cache behavior, or compaction contribute?
3. Which tool, retry, or subagent caused a failure or delay?

Before broadening the schema, pre-register one intervention hypothesis, for
example: reducing an identified unexplained wait lowers abandonment or retry
rate. Compare baseline and intervention runs on comparable task classes and
models. Record task outcomes such as success, rework, abandonment, retries,
and operator diagnosis time; raw token or latency totals are not quality by
themselves.

The diagnostic writer is itself part of the system under test. Metrics mode
must record its own queue depth, dropped-event count, flush status, write errors,
record size, and overhead. It must be asynchronous/bounded and must never block
or crash the harness. A dropped or unavailable diagnostic record is evidence,
not silent success.

Initial guardrails:

- target less than 2% measured p95 overhead in metrics mode and no unexplained
  first-visible-output regression;
- no unredacted secret or raw content in metrics mode;
- bounded retention and explicit access/permission checks;
- deterministic crash/recovery tests must preserve a trustworthy incomplete-run
  classification;
- no database, dashboard, remote exporter, OS tracing, raw provider payloads,
  or RL consumer in the first experiment.

Stop or narrow the effort after two unsuccessful actionable experiments, after
20 representative tasks produce no decision-changing evidence, when privacy or
recovery guarantees cannot be enforced, or when routine Pi upgrades require
repeated fork-specific repair. Expand only when a trace changes a concrete
engineering decision or materially reduces diagnosis time.

## 15. Outcome and experiment contract

Mechanism metrics are not outcomes. A task that uses more tokens, tools, or
files may simply be harder. Any claim that an instrumentation-guided change
improved Pi must attach an independent outcome record:

```json
{
  "experiment_id": "...",
  "condition": "baseline | intervention",
  "task_id": "...",
  "task_attempt_id": "...",
  "outcome": "accepted | rejected | rework | abandoned | failed | unknown",
  "quality_evidence": {
    "tests_passed": null,
    "review_verdict": null,
    "user_acceptance": null
  },
  "rework_count": null,
  "user_interrupts": 0,
  "user_steers": 0,
  "time_to_accepted_ms": null
}
```

Outcome labels must identify their authority. An acceptance-policy verdict is
not automatically a quality label. Prefer deterministic tests, blind review,
user acceptance, rework, abandonment, or time-to-accepted where available.
Unknown and missing outcomes must remain explicit.

For paired comparisons, also record:

- harness/code/config hashes;
- requested, resolved, and response model;
- provider and transport;
- task class and corpus version;
- intervention assignment;
- parent/child trace links.

Do not pool heterogeneous providers, models, or task classes without
stratification. A first value experiment should compare the same task and
provider/model under baseline and one intervention, with a predeclared primary
outcome and overhead/privacy budget.

## 16. Clock and completeness semantics

Every process that emits timing data must include a clock-domain descriptor:

```json
{
  "clock": {
    "kind": "monotonic",
    "origin_id": "process-uuid",
    "origin_wall_time": "..."
  }
}
```

Never subtract monotonic timestamps from different processes. Cross-process
reports should use local durations, wall-clock correlation with uncertainty, or
an explicit synchronization event. User-visible output also needs an explicit
observation boundary: stdout write completion is not proof that a terminal
rendered or a human saw the output.

The first validation slice must therefore test not only event fields but also
completeness and missingness:

- every injected failure has a terminal classification or an explicit
  `incomplete_no_terminal_event` record;
- missing provider usage remains `unavailable`, not zero;
- logical generations and physical retries are separately reconstructable;
- dropped events, writer failures, and retention truncation are themselves
  recorded;
- parent and child records join through explicit task-attempt IDs.

## 17. User interaction model

The event ledger is not the primary user interface. Users should interact with
three layers:

### Observe a normal run

Every diagnostic-enabled prompt receives a trace automatically. The user can
ask for a compact report rather than inspect raw JSONL:

```text
/diag current
/diag failures
/diag context
/diag export <trace-id>
```

The current-run view should show:

```text
status: running | completed | failed | incomplete
elapsed: 42.1s
first visible output: 0.8s
provider: 31.4s
tools: 8 calls / 7.9s
queue + compaction: 1.2s
input/output/cache tokens: ...
```

### Deliberately run a task

Benchmarkable work needs an explicit task boundary. A task can be started from
the TUI, CLI, or workflow layer:

```text
/task start fix-login-tests
```

The task ID must be carried into every prompt, retry, child, and outcome record.
If no explicit task is active, Pi may create an inferred prompt-run ID, but
reports must label it as inferred and it must not be used for strong benchmark
claims.

### Compare conditions

A benchmark command should run a declared suite and produce a report, not make
the user read logs:

```bash
pi bench run suites/login.json --condition baseline --repeats 3
pi bench run suites/login.json --condition intervention --repeats 3
pi bench compare runs/baseline runs/intervention
```

The raw event ledger remains available for diagnosis; the report is the normal
interaction surface.

## 18. Benchmark task model

A benchmark task is a reproducible starting state plus an independent evaluator,
not just a prompt:

```json
{
  "task_id": "fix-login-tests",
  "task_class": "bugfix",
  "repository": "./fixtures/login-app",
  "base_revision": "exact-commit-or-snapshot",
  "prompt": "Fix the failing login tests without changing the public API.",
  "setup": ["npm ci", "./scripts/seed-fixture"],
  "evaluator": ["npm test -- --runInBand"],
  "timeout_ms": 600000,
  "allowed_tools": ["read", "grep", "find", "ls", "bash", "edit", "write"],
  "repetitions": 3
}
```

The evaluator should produce the primary outcome. For code tasks this is
usually deterministic tests plus a diff/scope check. Human acceptance,
rework, and abandonment can be added as secondary outcomes. Do not begin with
an LLM judge unless deterministic evaluation is impossible.

Use three different benchmark layers:

1. **Harness fixtures**: deterministic fake-provider tasks that validate event
   ordering, timing, retries, compaction, crash recovery, and missingness. They
   measure instrumentation correctness, not model quality.
2. **Replayable task corpus**: fixed repositories, prompts, setup, evaluators,
   and model/provider conditions. These support before/after comparisons.
3. **Live observational runs**: ordinary work captured for discovery. They are
   useful for finding unknown failure modes but are confounded and should not be
   treated as causal benchmark results.

## 19. What “the model was faster” should mean

Avoid a single speed number. Report a vector conditioned on task class, model,
provider, and outcome:

```text
completion rate
accepted-task rate
p50/p95 time-to-first-visible-output
p50/p95 provider generation time
p50/p95 time-to-first-tool
p50/p95 time-to-accepted
input/output/cache/reasoning tokens per accepted task
reported cost per accepted task
rework and user-intervention rate
failure and retry rate
```

A model that answers faster but requires more rework is not necessarily faster
at the task. The primary comparison should usually be **time-to-accepted** or
**accepted work per unit cost**, with provider/tool/token metrics explaining
why the result changed.

## 20. Boundary: Pi runtime versus benchmark harness

Most benchmark functionality should not live inside Pi. Pi should expose a
small, stable measurement surface; the benchmark harness should own experiment
semantics.

### Belongs in the custom Pi build

- lifecycle and provider/tool/session events;
- local monotonic timing and clock-domain metadata;
- usage/cache/cost observations with provenance;
- redaction and bounded diagnostic persistence;
- terminal, crash, abort, retry, and incomplete-run markers;
- correlation IDs propagated through Pi and subagents;
- a small live diagnostic view for the current run;
- stable schemas and versioned adapters around upstream Pi changes.

Pi should not decide whether a task is good, whether a condition won, or how a
benchmark is statistically analyzed.

### Belongs in the external harness benchmark

- task manifests and repository fixtures;
- setup/reset/isolation of worktrees or containers;
- baseline/intervention assignment and randomization;
- repetitions and task-class stratification;
- deterministic evaluators and test commands;
- human acceptance and rework labels;
- report generation, comparisons, confidence intervals, and retention of
  benchmark results;
- optional export to later evaluation or training systems.

This separation lets ordinary Pi sessions remain useful without requiring
benchmark metadata, while benchmark runs can inject explicit `experiment_id`,
`condition`, `task_id`, and evaluator information into the Pi trace.
