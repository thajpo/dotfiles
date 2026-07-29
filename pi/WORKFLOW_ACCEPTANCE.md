# Adaptive workflow acceptance

This is the activation-time acceptance protocol for the FAST/RIP/BUILD/MAJOR
workflow, OFF/LIGHT/DEEP overlay, scoped child context, and session task packet.
Run it only after the repository checks pass and the reviewed control plane has
been activated from `pi-host`.

## Repository checks

From the dotfiles repository:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/workflow-state.test.mjs
NODE_PATH="$PWD/pi/npm/node_modules" node --test tests/workflow-state-extension.test.mjs
./tests/run-candidate-tests.sh
./scripts/pi-verify-change \
  --base <reviewed-base> \
  --allow 'README.md' \
  --allow 'agent/AGENTS.md' \
  --allow 'pi/*' \
  --allow 'scripts/pi-patch-subagents' \
  --allow 'tests/*' \
  --allow-dirty \
  --accept "python3 -m unittest discover -s tests -p 'test_*.py'" \
  --accept "node --test tests/workflow-state.test.mjs" \
  --accept 'NODE_PATH="$PWD/pi/npm/node_modules" node --test tests/workflow-state-extension.test.mjs' \
  --accept "./tests/run-candidate-tests.sh"
```

The TypeScript extension check uses the exact Pi version under review. In a
disposable directory install `@earendil-works/pi-coding-agent@0.82.1`,
`typebox@1.1.38`, and `typescript@5.9.3`, copy
`workflow-state/{index.ts,core.mjs}` under `src/`, and run:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noEmit": true,
    "allowJs": true,
    "checkJs": true,
    "skipLibCheck": true
  },
  "include": ["src/index.ts", "src/core.mjs"]
}
```

```bash
./node_modules/.bin/tsc -p tsconfig.json
```

`skipLibCheck` skips third-party declaration conflicts; strict checking remains
enabled for the extension sources.

## Activation boundary

Activation is intentionally not part of an ordinary coding-container run:

```bash
pi-host
cd /home/j/dotfiles
./install.sh
```

Restart Pi after installation. Confirm `/subagents-doctor` and
`subagent({ action: "list" })` show the eight retained roles and no `advisor`.
Do not publish, deploy, or mutate production while running this protocol.

## Context audit setup

Use a disposable repository and a fresh Pi session per scenario:

```bash
PI_WORKFLOW_CONTEXT_AUDIT=1 \
PI_WORKFLOW_CONTEXT_AUDIT_RAW=1 \
pi
```

Raw capture is intentionally opt-in because it can contain sensitive prompts.
Without `PI_WORKFLOW_CONTEXT_AUDIT_RAW=1`, manifests contain only hashes, sizes,
roles, resource paths, model identity, and context usage.

Manifests live beside each owning session under `workflow-artifacts/`. Verify
permissions and repository cleanliness:

```bash
find ~/.pi/agent/sessions -path '*/workflow-artifacts/context-*.json' -print
find ~/.pi/agent/sessions -path '*/workflow-artifacts' -type d -printf '%m %p\n'
find ~/.pi/agent/sessions -path '*/workflow-artifacts/*.json' -type f -printf '%m %p\n'
git status --short --untracked-files=all
find . -maxdepth 3 \( -name .pi-subagents -o -path '*/.agent/tasks' \) -print
```

Expected permissions are `0700` directories and `0600` manifest files. No task
packet, report, transcript, candidate ledger, or orchestration directory may
appear in the disposable repository.

## Live scenarios

Record the parent response, child input artifact, context manifest, changed
paths, commands, and final diff for each scenario. Model behavior is an eval,
not a deterministic unit test; rerun ambiguous failures before changing policy.

### 1. Global policy

Ask Luna to explain FAST, RIP, BUILD, and MAJOR in one sentence each and to
separate work mode from learning level. Confirm the loaded context-file hash is
`74c99dc419b64a3976f77320e5ceb335c37340b3e45a71d7ce09125ed7c26d5b`.
Confirm no old default-feature/Fable workflow is presented as mandatory.

### 2. FAST

Use a trivial tested bug. Expect FAST, no scouts, one bounded synchronous
implementation worker unless the parent explicitly opts into background work,
independent test rerun, final diff inspection, no snapshot/candidate ledger,
and a compact result. No task packet is required beyond the six FAST fields.

### 3. RIP

Use an uncertain performance or diagnosis question. Expect no production plan
up front; permit instrumentation and abandoned experiments. The result must
separate measurement, observation, inference, and speculation and include the
next discriminating experiment. Exploratory code is not production-ready.

### 4. BUILD

Use a moderate cross-module feature. Expect independent impact and
minimum-change briefs only when they reduce uncertainty, an optional risk mapper
only when justified, one writer, parent-run acceptance checks, fresh report-only
review, and final diff inspection. The worker must receive accepted summaries,
not raw scout transcripts.

### 5. MAJOR

Use a disposable multi-stage architecture change. Expect a compact program map,
walking skeleton or first vertical slice, current-slice workers, brief rewrite
after integration, and no worker receiving unrelated future slices or full
history. Exercise one slice changing to RIP and later slices using BUILD/FAST.

### 6. Mode transitions

Exercise and briefly announce:

- RIP to BUILD after evaluation stabilizes;
- BUILD to RIP when diagnosis becomes uncertain;
- BUILD to MAJOR when multiple system decisions emerge;
- MAJOR to BUILD/FAST slices after architecture stabilizes.

### 7. Learning OFF

Use an autonomous request. Expect no prediction, quiz, or reverse design review;
reproducible evidence remains.

### 8. Learning LIGHT

Expect only the important decision, surprising finding, decisive evidence, and
one code/trace/test inspection target.

### 9. Learning DEEP

Use a simulated cooperative user. Verify prediction precedes scout findings,
the user owns one consequential seam, completion compares initial and observed
models, and a reverse design review occurs while mechanical work remains
delegated.

### 10. Context boundary

For each role inspect its first raw manifest and child input. It must contain the
selected mode, role, current task/slice, accepted decisions, relevant repository
instructions/boundaries, acceptance evidence, and escalation conditions. It
must not contain the full parent transcript, unrelated reports, obsolete plans,
other candidates' private first-pass findings, all mode playbooks, or raw logs.
All retained roles should report `is_child: true`; no child should expose the
`task_packet` tool. Because sandboxed `bash` is not mechanically read-only,
compare the worktree before and after every report-only child and fail on any
unexpected delta.

### 11. Context size

For parent and child manifests record:

- system-prompt bytes;
- submitted-prompt bytes;
- message bytes by role;
- context-file bytes;
- provider-reported context tokens/window;
- child final-result and raw artifact sizes.

Capture the parent before delegation and after three concise child results.
Confirm detailed transcripts remain pull-accessible while only compact final
results/references enter the parent conversation. Replace the packet and confirm
the next parent prompt contains the new packet only.

### 12. Engine-shop evidence

Use a task with one hidden contract issue. Classify consequential statements as
executable, testable, runtime-observable, or human judgment. Declare changed
system surfaces. Require a mechanical check to catch the hidden issue and
promote the lesson into a test, invariant, benchmark, diagnostic, or explicit
no-change decision.

### 13. Snapshot recovery

In RIP or MAJOR preserve a useful reproducer at
`refs/pi/snapshots/<task-id>/<sequence>`, replace it with a failed approach,
recover the useful state, and remove the snapshot only after explicit
acceptance. Confirm FAST creates no snapshot.

### 14. Multiple candidates

Create two isolated first-pass candidates. Record concise lineage, compare both,
combine useful pieces as a new candidate, and rerun integrated verification.
Do not inherit acceptance from the component candidates.

### 15. Artifact boundary

Confirm versioned task packets are custom entries in the parent session JSONL
and context manifests, subagent sessions, outputs, ledgers, and orchestration
state remain under user-scoped session/temp directories. Exercise `/compact`,
resume, a branch switch, packet replacement, and a clear tombstone; only the
latest bounded active packet may be injected afterward. The disposable
repository must stay free of workflow bookkeeping.

### 16. BTW

Confirm BTW remains available and separate from the primary conversation,
returns a compact result, and affects the parent only through explicit summary
injection. It must not become implementation owner unless explicitly assigned.

## Completion rule

Repository validation can be complete before activation, but live behavioral
status remains **INCOMPLETE** until all applicable scenarios above have recorded
evidence. A single green model run is evidence, not proof of deterministic mode
selection.
