# Pisec Model Contract and Runtime Recovery Plan

Status: planning and handoff contract; not implementation authorization

Prepared: 2026-08-27

Repository: /home/j/dotfiles

## 1. Purpose

This plan repairs the Pisec failures exposed while opening the VLA Lens issue
#38 and #39 workers. The failures are related, but they do not have one cause.
They cross the model prompts, model-facing tool schemas, broker contracts,
worker routing, completion protocol, issue lifecycle, runtime refresh, and
Herdr presentation.

The desired result is:

1. Every newly spawned worker starts useful work immediately.
2. The worker receives the actual engineering goal. It never receives
   "create the worker" as its own assignment.
3. The configured default worker is Codex with GPT-5.6 Luna at high reasoning.
4. A malformed model selection cannot silently produce a Pi/OMP worker.
5. OMP and Codex use one completion protocol.
6. Pisec-generated events never appear to be messages typed by the human.
7. A worker tooling report has a visible owner, remediation path, and final
   verification.
8. Pisec update cannot interrupt worker provisioning and leave an empty pane.
9. Human-facing status explains the implementation in normal engineering
   language. It does not lead with IDs, hashes, or short status-card prose.

This is a MAJOR change. Implement it in independently testable slices. Do not
combine all changes into one deployment.

## 2. Corrections to the earlier draft

### 2.1 Workers always start

There is no await-user launch mode in this plan.

After authorization and successful provisioning, every worker starts its
approved task. The problem in the incident was not that the worker started.
The problem was that the Secretary gave it a circular infrastructure task:
create or prove the existence of the worker that Pisec had already created.

The repaired Secretary must give the worker the engineering assignment that
motivated the spawn.

Examples:

- "Open issue #38" means: inspect the existing issue #38 implementation,
  reconstruct the original issue contract, run the relevant checks, identify
  completed and missing work, continue fixing approved gaps, and report the
  implementation status.
- "Review issue #38" means: compare the implementation with the issue
  requirements, run verification, identify risks and gaps, and make approved
  corrections.
- "Implement issue #38" means: implement the issue contract, verify it, and
  submit the candidate for review.

Worker provisioning is a Secretary and broker responsibility. It must not
appear in the worker outcome, acceptance criteria, or completion evidence.

### 2.2 Human reports explain engineering work

Secretary and First Mate replies must not default to a small status card with
raw metadata. The human needs to understand:

- the original change request;
- the approach used by the implementation;
- which important parts are complete;
- how the changed parts work together;
- what verification ran and what it proves;
- what remains incomplete or risky;
- the next engineering action.

IDs, commit hashes, packet hashes, workstream IDs, timestamps, and internal
state names are diagnostic details. Include them only when the human must use
one to approve, inspect, disambiguate, or debug something.

### 2.3 Internal safety records stay internal

The cross-process lock and provisioning journal described later are internal
control-plane mechanisms. They must not become routine human-facing metadata.
The user-facing explanation translates them into normal language, such as:

> Worker creation stopped after the Herdr tab was created because the Pisec
> runtime changed. Pisec removed the partial tab and retried creation against
> the new runtime.

## 3. Incident facts that the implementation must preserve

The implementation agent must treat these as measured facts:

1. The Secretary supplied "worker-default" as implementationModel.
2. "worker-default" is an execution profile, not a model route.
3. The broker did not find that route and silently selected fallbackHarness,
   which was OMP.
4. Omitting implementationModel would have selected the configured Codex /
   GPT-5.6 Luna / high route.
5. The OMP worker checkpoint tool advertised ready_review with an optional
   completion object.
6. The broker rejected the generated completionPacket field before workflow
   validation.
7. The backend completion operation accepted materially equivalent evidence
   and synthesized the ready_review checkpoint.
8. The Codex MCP surface does not currently expose pisec_submit_completion.
9. The worker creation brief and attention wake are delivered with Herdr's
   ordinary agent prompt API and are persisted as human-authored messages.
10. runtime.turn.prepare uses the full OMP session pathname as sessionKey.
11. The observed worker pathname exceeded the 256-character broker limit.
12. OMP displayed the extension exception but continued the model turn, so the
    claimed fail-closed behavior did not fail closed.
13. The reported tooling issue exists only in the local Pisec database. It is
    acknowledged, unresolved, and has no remediation link.
14. Pisec update replaced the runtime while workstream creation had already
    created a Herdr tab. Restart recovery could not validate the saved runtime
    snapshot and left a partial empty pane.

## 4. Product contract

### 4.1 First Mate

The First Mate is the fleet-level coordinator.

It must:

- explain cross-project activity and platform problems;
- route work through the correct project Secretary;
- triage fleet-visible issues;
- identify ownership, consequences, and required decisions;
- avoid claiming it can create or link project workers when its tools do not
  provide that authority.

It must not receive worker-creation guidance that names tools it does not have.

### 4.2 Secretary

The Secretary is the project-level coordinator.

It must:

- understand the user's engineering request;
- inspect the original issue, branch, or approved source when needed;
- construct a worker task about the engineering outcome;
- ensure the source is clean and committed;
- prepare the exact Pisec workstream proposal;
- show the resolved harness and model in the approval;
- create the worker after approval;
- let the worker start immediately;
- monitor implementation progress, evidence, blockers, and completion;
- explain implementation status in medium detail;
- own acceptance, integration, retirement, and cleanup;
- triage worker issues instead of stopping after acknowledgement.

The Secretary must never use these as worker goals:

- create a worker;
- prove that a tab exists;
- report workspace IDs;
- verify that Pisec bound the worker;
- complete the worker provisioning operation.

Those are broker postconditions.

### 4.3 Worker

The worker is the bounded engineering executor.

It must:

- start the task immediately after launch;
- use the immutable task packet as its authority;
- inspect existing implementation before claiming what remains;
- relate its work to the original change request;
- continue implementation or correction within the approved boundaries;
- run proportionate verification;
- report blockers through typed Pisec tools;
- record progress checkpoints;
- submit one completion packet when the engineering task is complete;
- distinguish candidate completion from human acceptance and integration.

The worker must not:

- create itself;
- treat successful provisioning as task completion;
- claim acceptance or integration;
- push or merge;
- expand scope because of an ordinary chat message.

## 5. Worker task construction

### 5.1 Required task content

Every worker proposal must contain:

1. Original goal
   - What user or issue outcome is required.
2. Starting state
   - New implementation, existing branch, imported snapshot, or partial work.
3. Required first action
   - What the worker should inspect or execute immediately.
4. Boundaries
   - Allowed paths, systems, and non-effects.
5. Acceptance criteria
   - Observable engineering results, not infrastructure identities.
6. Verification
   - Commands, tests, or inspections that demonstrate correctness.
7. Reporting expectation
   - Medium-detail explanation tied to the original request.

### 5.2 Default assignment for an existing implementation

When the user asks to open or spawn a worker around existing committed work and
does not provide a more specific task, use this semantic default:

> Reconstruct the original change request and acceptance criteria. Inspect the
> existing implementation against that contract. Run the relevant targeted
> checks. Identify which requirements are complete, partial, or missing.
> Continue correcting gaps that are within the approved paths. Report the
> implementation at a medium level of detail, including how the main code
> surfaces work together, verification results, remaining risks, and the next
> engineering action.

This default starts useful work. It does not tell the worker to wait, and it
does not assign worker provisioning as the task.

### 5.3 Example corrected issue #38 assignment

The issue #38 worker should have received an assignment equivalent to:

> Evaluate the imported issue #38 FOUNDATION execution-driver implementation
> against the original issue contract. Explain the execution flow and the
> responsibilities of the foundation driver and batch-capture code. Run the
> relevant project checks. Identify missing behavior, integration gaps, and
> residual risks. Correct approved gaps within the issue #38 files. When the
> implementation satisfies the contract, submit completion evidence that maps
> each acceptance criterion to code and verification.

It should not have received:

> Create a Pisec worker tab by importing the branch.

## 6. Human-facing reporting contract

### 6.1 Default level of detail

Use a medium-detail senior-engineering briefing. Use normal English and
STE-100-style discipline:

- use short, direct sentences;
- use active voice;
- put one main idea in each sentence;
- define uncommon Pisec terms when they matter;
- prefer concrete behavior over abstract status words;
- explain why a fact matters;
- avoid vague phrases such as "substantial work" without describing the work.

A normal worker status should usually contain five sections in this order:

1. Goal and current position
2. Implementation completed
3. How the important parts work
4. Verification and confidence
5. Remaining work, risks, and next action

The format may use paragraphs or bullets. Do not force the labels "Status",
"Needs attention", and "Next action".

### 6.2 Required evidence before summarizing a worker

Before the Secretary explains a worker, it must inspect:

- the original task packet or issue contract;
- the worker's latest semantic checkpoint;
- the completion packet, if one exists;
- the worker Git changes or committed candidate;
- the important changed code surfaces;
- verification results;
- open blockers and attention.

Attention is supporting context. It must not replace the implementation
summary unless the attention item prevents the worker from doing any
engineering work.

### 6.3 Completed-worker report

When a worker reports completion, the Secretary must explain:

- what the original request required;
- what the candidate changed;
- how each major change satisfies the request;
- the important design or execution flow;
- tests or checks that passed;
- any meaningful untested condition or residual risk;
- whether the candidate is ready for human acceptance.

It must not merely say that the worker is ready_review or list a completion
packet ID.

### 6.4 Tooling issue report

When a tooling issue exists alongside valid implementation work, report both:

1. The implementation status and substance.
2. The Pisec lifecycle/tooling problem and its effect on review or integration.

For the issue #38 incident, the Secretary should have said, in substance:

> The issue #38 branch already contains the foundation execution driver and
> batch-capture implementation. The worker inspected the imported snapshot and
> confirmed that the candidate consists of two main code surfaces. It then ran
> the Git-level identity checks and submitted completion evidence. The code
> candidate was not lost.
>
> A separate Pisec defect affected the completion handoff. The OMP tool told
> the worker to include completion evidence in a ready-review checkpoint, but
> the broker rejects that field. The worker then used the separate completion
> operation, which succeeded and created the ready-review state. The candidate
> can still be reviewed, but the first completion interface remains broken and
> has not been remediated.
>
> The next engineering step is to inspect the issue #38 implementation against
> the original acceptance criteria and run its project-level tests. Separately,
> Pisec needs one canonical completion contract across OMP, Codex, and the
> broker.

This leads with the work the human cares about. It keeps internal IDs out of
the default explanation.

## 7. Canonical completion contract

### 7.1 Decision

Use pisec_submit_completion as the sole final handoff operation.

Ordinary checkpoints record progress:

- investigating;
- implementing;
- verifying.

Submitting completion performs one atomic backend transaction:

1. Validate the completion packet.
2. Store the immutable completion packet.
3. Create the matching ready_review checkpoint.
4. Wake the Secretary for review.

ready_review is therefore the result of a valid completion submission. It is
not an independently writable progress phase.

### 7.2 Why use a separate completion operation

A completion packet has stricter meaning than a progress checkpoint. It must
include:

- acceptance evidence;
- verification results;
- the exact source commit;
- the task-packet hash;
- changed surfaces;
- residual risk.

Keeping this separate gives the broker one place to enforce those invariants.
It prevents these invalid states:

- ready_review without a completion packet;
- completion packet without ready_review;
- an optional completion body whose required fields depend on a phase string;
- OMP and Codex interpreting the same checkpoint differently.

The current backend already implements this atomic behavior. The failure came
from the OMP extension and prompts continuing to advertise an older combined
checkpoint contract.

An alternative would be to make ready_review checkpoint submission the only
completion API and delete pisec_submit_completion. That can work, but it would
require moving the backend transaction back into checkpoint handling and
making ready_review a discriminated request type with mandatory completion
evidence. There is no product benefit that justifies reversing the working
backend again.

### 7.3 Required implementation changes

OMP:

- remove ready_review from the ordinary checkpoint schema;
- remove the optional completion object from that tool;
- describe the tool as progress-only;
- retain pisec_submit_completion;
- rewrite the worker prompt to use it.

Codex:

- expose pisec_submit_completion through MCP;
- give it the same schema and description as OMP;
- correct the Codex worker prompt;
- correct the Codex checkpoint description.

Broker and protocol:

- keep workstream.completion.submit as the sole completion ingress;
- synthesize exactly one ready_review checkpoint;
- add OMP-to-broker and Codex-to-broker conformance tests;
- generate or validate all model-facing schemas from one checked contract.

### 7.4 Acceptance

Both OMP and Codex workers can:

1. Record each progress phase.
2. Submit completion once.
3. Produce one completion packet and one derived ready_review checkpoint.
4. Retry the same submission safely.
5. Receive a field-specific error for malformed evidence.

## 8. Worker routing

### 8.1 Default route

When the Secretary does not name an explicit model route, the broker resolves
the configured default:

- harness: Codex;
- model: GPT-5.6 Luna;
- reasoning effort: high.

The Secretary should normally omit the model field.

### 8.2 Validation

The broker must reject:

- "worker-default" as a model;
- any explicit model route not present in the configured route table;
- any route whose harness is unavailable;
- any approval whose resolved route changes before creation.

Do not silently use fallbackHarness for an invalid explicit model.

### 8.3 Human approval

Show the resolved worker in normal language:

> Worker: Codex with GPT-5.6 Luna, high reasoning

Do not lead with adapter IDs or routing keys.

### 8.4 Acceptance

- An omitted model creates a Codex/Luna worker.
- "worker-default" fails before any external effect.
- No malformed request creates an OMP worker.
- The approved harness and launched harness are identical.

## 9. Pisec-authored messages and attention

### 9.1 Message provenance

Stop using Herdr agent.prompt for broker-authored control messages.

Use typed extension messages for:

- worker bootstrap;
- coordinator attention;
- runtime-blocked notices.

Each session entry must visibly identify Pisec as the source and store its
source record ID in structured details. The model must inspect the
broker-authenticated source record before acting.

The PISEC: prefix remains a visual fallback. It is not the authority check.

### 9.2 Workers start immediately

After the broker confirms the binding, the extension consumes one durable
bootstrap event and triggers the worker's first turn.

The authoritative system context contains:

- role;
- engineering goal;
- starting state;
- boundaries;
- acceptance criteria;
- verification;
- completion procedure;
- reporting expectation.

The bootstrap event means "start the assigned work now." It does not repeat
the task as a human message.

### 9.3 Attention behavior

Attention should wake the appropriate coordinator when judgment is required.
It must:

- appear as Pisec-authored;
- identify the type of source record;
- be delivered once per actionable revision;
- avoid re-waking merely because the same actor acknowledged it;
- remain visible until the typed source reaches a non-actionable state.

The coordinator's reply must synthesize the underlying engineering situation.
It must not dump the attention record.

## 10. Session and protocol identifiers

### 10.1 Remove sessionKey

Remove the OMP session pathname from runtime.turn.prepare.

Return the immutable task packet on every turn. This is safer after:

- compaction;
- session switching;
- resume;
- runtime refresh;
- extension restart.

The current runtime_sessions table can first become unused. Remove it in a
later schema cleanup after deployed runtimes stop calling the old operation.

### 10.2 Keep actual security controls

Keep:

- runtime capability token;
- runtime generation hash;
- instance identity;
- surface identity;
- workstream binding.

These prevent stale or unrelated runtimes from acting as the worker.

### 10.3 Hide idempotency from models

Keep idempotent broker operations, but generate their identifiers inside the
OMP extension and Codex MCP adapter.

Use:

- approval-scope hash for approved transitions;
- packet hash for immutable completion;
- native tool-call identity or canonical request hash for ordinary retries.

The model should describe the operation. It should not manufacture protocol
bookkeeping strings.

## 11. Real fail-closed behavior

OMP catches extension hook exceptions. Throwing from before_agent_start is
therefore not a security boundary.

The repaired runtime must:

1. Attest the broker, binding, token, surface, and generation before work.
2. Mark the worker blocked if attestation or turn preparation fails.
3. Deny shell, write, Git-mutation, and other project-changing tools while
   blocked.
4. Allow only the diagnostic or recovery tools needed to explain the failure.
5. Resume only after a successful broker handshake or controlled restart.

Acceptance tests must prove that a failed turn-preparation call cannot modify
the repository even if the model continues running.

## 12. Cross-process mutation lock

### 12.1 What the lock protects

Use one host-level cross-process lock for Pisec control-plane mutations that
change runtime generation or external workspace topology:

- Pisec install/update;
- runtime surface refresh;
- workstream provisioning;
- project activation or deactivation;
- runtime-binding replacement;
- workstream retirement and cleanup;
- integration steps that require a stable target and binding.

Normal worker activity does not take this lock.

Workers continue to:

- inspect and edit files;
- run tests;
- commit;
- use Pisec progress tools;
- communicate with the Secretary.

The lock is held only during short control-plane transitions. It does not stay
locked for the lifetime of a worker.

### 12.2 Why it is useful

The incident crossed two processes:

1. The broker was creating a worker and had already created the Herdr tab.
2. The external update process replaced the runtime surface and restarted the
   broker.

The broker's in-memory lock could not stop the updater. The resumed operation
then held a saved generation that no longer existed.

The cross-process lock gives a deterministic ordering:

- If provisioning starts first, it finishes or rolls back before update.
- If update starts first, provisioning waits and then captures the new runtime.

It prevents one operation from observing half of another operation.

### 12.3 User-facing behavior

If an update collides with provisioning, report:

> Pisec update is waiting for one worker creation to finish.

Do not expose lock file paths, process IDs, or generation hashes unless
diagnostics are requested.

### 12.4 Acceptance

- Update cannot replace the runtime during worker creation.
- Worker creation cannot capture a runtime while update is replacing it.
- Active workers keep working during unrelated provisioning.
- A crashed lock owner can be detected and recovered safely.

## 13. Durable external-effect journal

### 13.1 What to record

Persist each confirmed provisioning effect and its exact owned identity:

1. Worker repository created
   - owned path, branch, base commit.
2. Herdr tab created
   - workspace ID and tab/view ID.
3. Runtime surface selected
   - immutable surface generation.
4. Runtime profile staged
   - staging identity.
5. Runtime profile activated
   - active generation and policy hash.
6. Binding committed
   - surface, runtime instance, and token hash.
7. Agent started
   - observed agent identity on the bound surface.
8. Bootstrap delivered
   - durable bootstrap event revision.
9. Final identities observed
   - repository, tab, surface, and runtime all agree.

The journal stores internal recovery data. The Secretary summarizes it in
human language.

### 13.2 How each effect is performed

For each external step:

1. Store the intended deterministic identity.
2. Perform the external effect.
3. Observe the effect through the owning adapter.
4. Verify that the observed identity matches the intention.
5. Mark the step confirmed.

Do not mark a step complete merely because a command returned success.

### 13.3 How restart recovery uses it

After broker restart:

1. Read the last confirmed step.
2. Re-observe the recorded resource by its stored identity.
3. If it exists and matches, continue with the next step.
4. If it is absent and recreation is safe, recreate the same deterministic
   resource.
5. If it exists but does not match, stop and compensate or request attention.
6. Never create a second tab, worktree, or agent merely because the process
   forgot the first one.

Example:

- Journal says the Herdr tab was confirmed.
- Broker restarts before profile activation.
- Recovery observes the exact tab and current runtime generation.
- If both still match, it resumes at profile staging.
- If the runtime changed, it closes the Pisec-owned partial tab and restarts
  provisioning against the new generation.

### 13.4 What this gets us

The journal provides:

- restart-safe resume;
- precise cleanup of Pisec-owned partial resources;
- no duplicate worktrees, tabs, or agents;
- a clear boundary between retry and rollback;
- evidence for why provisioning failed;
- deterministic tests at every interruption point.

It is the mechanism that turns "we restarted somewhere during creation" into
"we know exactly which effects exist and what the next safe action is."

### 13.5 Compensation

Before a durable live binding exists, a failed operation may remove only the
resources it recorded as Pisec-owned:

- stop the partial runtime;
- close the created pane or tab;
- discard the staged profile;
- remove the Pisec-created worker repository.

Never modify or remove the external imported source checkout.

After a valid binding exists, prefer reconciliation and resume. Do not delete a
working worker because a later presentation step failed.

### 13.6 Acceptance

Inject restart or failure after every journal step. Each test must end with
exactly one of:

- one fully bound, running worker; or
- no worker and no Pisec-owned orphan.

## 14. Issue ownership and remediation

### 14.1 Current failure

The issue #38 worker correctly recorded a tooling issue. The Secretary
acknowledged it. No remediation worker, owner, platform escalation, or
resolution was created.

The current model-facing tool surface also omits the documented Secretary
escalation tool.

### 14.2 Required ownership

Every issue must state:

- reporting project and worker;
- project issue or Pisec platform issue;
- current owner;
- remediation project;
- required next action;
- verification actor;
- blocking or degraded impact.

### 14.3 Platform issues

A Pisec defect reported from VLA Lens remains linked to the VLA worker, but its
remediation belongs in the Pisec/dotfiles project.

The broker must create or expose a platform escalation that the First Mate can
see even when the reporting project uses project coordination mode.

The human still authorizes the remediation worker. Reporting an issue does not
grant cross-project write access and does not automatically spawn a worker.

### 14.4 State machine

Use states with concrete meaning:

1. open
2. triaged
3. remediation_planned
4. remediating
5. candidate_ready
6. integrated
7. verifying
8. resolved

Acknowledged is delivery state, not remediation progress.

Completion submission creates candidate_ready. It does not mean the
remediation is integrated. Verification begins after integration.

### 14.5 Reporter lifetime

Do not retire the only authorized verifier for an unresolved issue.

Before reporter retirement:

- resolve the issue; or
- assign an authorized successor verifier; or
- retain the minimal verification binding required to answer.

### 14.6 Acceptance

Test:

- same-project code remediation;
- cross-project Pisec remediation;
- missing escalation tool regression;
- candidate acceptance and integration;
- reporter verification;
- still-blocked reopen;
- no repeated wake loop after acknowledgement;
- no unresolved issue lost during reporter retirement.

## 15. Prompt and tool implementation surfaces

The implementation agent must inspect and align at least:

- omp/extensions/pisec.ts
- scripts/pisec/broker.py
- scripts/pisec/runtime.py
- scripts/pisec/workflow.py
- scripts/pisec/workstreams.py
- scripts/pisec/attention.py
- scripts/pisec/codex_mcp.py
- scripts/pisec/harnesses/codex.py
- pisec/operation-catalogue.json
- generated operation catalogues
- README.md
- OMP extension tests
- Codex MCP tests
- broker protocol tests
- provisioning failpoint tests
- issue lifecycle tests

Prefer one checked protocol contract that generates or validates the broker,
OMP, and Codex projections. Do not repair the same schema independently in
three places without a conformance test.

## 16. Implementation phases and commit boundaries

### Phase A: Prompt and reporting contract

- Extract role prompts into reviewable sources or generated prompt fragments.
- Remove the First Mate tool mismatch.
- Rewrite the Secretary worker-task rules.
- Add the immediate-start worker rule.
- Add the medium-detail reporting contract.
- Add prompt snapshots and role/tool parity tests.

Do not change runtime behavior in this phase.

### Phase B: Routing and completion

- Validate configured model routes.
- Make Codex/Luna the resolved default.
- Reject "worker-default" as a model.
- Remove silent fallback for malformed explicit models.
- Make submit_completion canonical in OMP and Codex.
- Add full protocol conformance tests.

Do not deploy routing until Codex completion passes end to end.

### Phase C: Provenance, session preparation, and fail-closed enforcement

- Replace generated human prompts with typed Pisec events.
- Bootstrap every worker exactly once and start it immediately.
- Remove sessionKey.
- Present the full task packet each turn.
- Generate idempotency inside adapters.
- Add the runtime mutation gate for broker failures.

### Phase D: Issue lifecycle

- Expose Secretary escalation.
- Add explicit ownership and next action.
- Support cross-project platform remediation.
- Tie remediation completion to integration.
- Preserve verification authority across retirement.

### Phase E: Provisioning atomicity

- Add the cross-process mutation lock.
- Complete the durable external-effect journal.
- Add observation and compensation at every step.
- Add update-versus-create concurrency tests.

### Phase F: Deployment and live validation

- Deploy only from a clean committed checkout.
- Do not deploy while creation, integration, or cleanup is active.
- Validate a disposable project first.
- Create a default Codex/Luna worker.
- Confirm that it starts useful engineering work immediately.
- Confirm medium-detail Secretary reporting.
- Confirm completion, issue reporting, restart recovery, and cleanup.
- Reclassify the existing issue #38 tooling report as a platform issue.

Each phase ends with its focused tests, the full Pisec suite, and a coherent
commit. Do not push without explicit user instruction.

## 17. End-to-end acceptance scenarios

### Scenario 1: Existing implementation

Human asks to open issue #38.

Expected:

- Secretary constructs an engineering review-and-continue task.
- Approval says Codex with GPT-5.6 Luna.
- Pisec imports the committed source without modifying it.
- One worker tab appears.
- Worker immediately inspects the issue contract and implementation.
- Worker runs targeted checks and continues approved gap work.
- Secretary later explains the implementation, verification, risks, and next
  action without leading with metadata.

### Scenario 2: New implementation

Human asks to implement a ready issue.

Expected:

- Worker starts implementation immediately.
- Progress checkpoints record engineering phases.
- Worker submits one completion packet.
- Secretary explains how the candidate satisfies the original request.
- Human accepts once.
- Secretary integrates and cleans up.

### Scenario 3: Invalid model input

Secretary or test sends "worker-default" as a model.

Expected:

- Preparation fails with a field-specific correction.
- No repository, tab, profile, or runtime is created.
- No OMP fallback occurs.

### Scenario 4: Runtime failure

Broker becomes unavailable before a worker turn.

Expected:

- Worker is visibly blocked.
- Project-changing tools are unavailable.
- No unscoped work occurs.
- Recovery resumes after attestation.

### Scenario 5: Update during creation

Pisec update starts while a worker is being provisioned.

Expected:

- One operation waits for the other.
- No mixed runtime generation exists.
- Final state is one running worker or no worker and no orphan.

### Scenario 6: Tooling issue

Worker reports a Pisec completion-contract defect.

Expected:

- Secretary explains implementation status first.
- Platform issue receives a visible owner and next action.
- User approves remediation in the Pisec project.
- Candidate is integrated.
- Reporter or successor verifies the fixed behavior.
- Issue resolves without repeated generic attention prompts.

## 18. Non-effects

This plan does not authorize:

- changing or integrating the VLA Lens issue #38 implementation;
- pushing or merging branches;
- deleting the existing workers;
- modifying the imported source checkout;
- auto-creating remediation workers without user approval;
- exposing more internal metadata to the human;
- making workers idle after spawn;
- treating acknowledgement as resolution;
- including the Reviewr branch-scope UX improvement in the critical repair.

The existing uncommitted Neovim and Pisec-label changes must be reviewed and
handled separately. Preserve unrelated dirty files.

## 19. Handoff rule

An implementation agent must begin by reading this entire plan and inspecting
the cited incident evidence. It must not restart from the older assumption
that workers sometimes wait for a human after spawn.

The first implementation output must be:

1. A concise inventory of affected contracts and tests.
2. The exact Phase A prompt and reporting changes.
3. Confirmation that Phase A does not alter runtime behavior.

The agent then executes phases in order. It does not deploy until Phase F and
does not run live update from a dirty checkout.
