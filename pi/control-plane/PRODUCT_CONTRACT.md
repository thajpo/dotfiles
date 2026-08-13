# Pi Harness Product Contract

Owns: user-visible roles, capabilities, and release-1 scope.

Status: normative target. The daily surface and headless orchestration repair
program restores the interaction model defined here; component source exists
for most surfaces, and full-system acceptance is re-run against the repaired
product.

## 1. Product objective

Pi should provide a seamless, performant, and safe local engineering workflow
without requiring the user to manage routes, containers, permissions, session
files, generated worktree paths, transition markers, child leases, or sandbox
refs.

The user interacts with projects, conversations, working copies, branches,
changes, reviews, integration decisions, and headless subagent assignments.
Internal distributed-systems mechanisms exist to make those interactions
reliable; they are not the normal user vocabulary.

The product is successful when:

- repository tools do not fail randomly because another component retained
  stale lifecycle state;
- restarting Pi preserves the intended conversation and exact work;
- a child sees the exact state selected by its parent;
- the secretary can explain the project and every relevant working copy;
- `pi-personal` works directly in the registered primary checkout by default
  and can intentionally create separate work without losing the shared safety
  guarantees;
- headless subagents run asynchronously under controller ownership and report
  durable progress, and workers never bypass the one-writer rule;
- every implementation is submitted as an immutable Git change revision;
- the secretary and user inspect and integrate submitted changes separately;
- failures say what was protected, what did not happen, and what decision is
  needed;
- normal healthy operation is quiet;
- the user can inspect the continuity summary retained after compaction.

The Pi controller strengthens and enforces these behaviors; it does not
redefine them. A control-plane mechanism that narrows a user-visible
capability described in this contract is a product defect, not an improvement.

## 2. User-facing concepts

The normal product vocabulary is intentionally small.

### 2.1 Project

A registered Git repository and its project history, policies, secretary,
working copies, conversations, submitted changes, and headless subagent
records.

### 2.2 Secretary

One persistent, read-only project-level conversation used to:

- inspect recent direction and current project state;
- inventory Git worktrees, active conversations, and active headless
  investigations;
- reason about candidate directions and dependencies;
- launch scoped read-only investigations;
- create or focus durable headful implementation workstreams;
- receive submitted changes from any implementation source;
- inspect target movement, merge conflicts, semantic overlap, and test impact;
- create a separate integration workstream when needed;
- perform an explicitly authorized integration through the controller;
- coordinate retention and cleanup after integration.

The secretary is not an implementation worker and does not silently edit
product files, reinterpret review evidence as merge authorization, or make
arbitrary Git changes. "Read-only secretary" means it has no arbitrary
`write`/`edit`/shell/Git interface. It MAY call narrowly semantic controller
operations that create a workstream, commit an exact already-edited path set,
integrate, publish, or clean up only under that operation's separate exact
current authorization. Those exceptions are audited mutations and must never
be described as ordinary read access or inferred from a review verdict.

### 2.3 `pi-personal`

A persistent direct agent for one project. It is not a lesser secretary and is
not required to use secretary workstream ceremony for ordinary work.

It MAY:

- work in the primary checkout on an ordinary branch;
- operate with pre-existing uncommitted work after recording an exact baseline;
- create or request a separate working copy when isolation is useful;
- launch headless read-only helpers and isolated headless workers;
- delegate exclusive implementation ownership under the execution contract;
- submit a change to the project integration queue.

It MUST inherit the same project identity, trust, runtime, exact-state child,
recovery, change-submission, and plain-language failure guarantees as a
secretary-created workstream.

It MUST NOT integrate its own submitted change into the target branch.

### 2.4 Workstream

A named, durable, headful implementation conversation with one assigned working
copy. Secretary-created workstreams always use a separate Git worktree.

A workstream:

- has a human title and bounded brief;
- has one active writer at a time;
- can discuss design directly with the user;
- can use headless read-only helpers and explicitly delegated workers;
- submits immutable change revisions;
- remains available for follow-up until explicitly retired;
- does not merge itself into the target.

### 2.5 Working copy

The primary checkout or a Git worktree used for work. "Working copy" is the
human-facing term; controller IDs, Git administrative directories, generated
paths, and container mount paths are technical details.

### 2.6 Change

A local change request analogous to a pull request or Gerrit change. A change
has one target branch and one or more immutable submitted revisions. Reviews
and integration attempts bind to an exact revision.

The normal phrase is "submit this change to the secretary" or "changes awaiting
integration," not "publish a sandbox ref" or "park a materialization."

### 2.7 Integration

A separate project-level activity in which the secretary and user decide how a
submitted revision should enter its target. Integration may be a guarded
fast-forward, an approved project merge strategy, or a separate integration
workstream. Submission never implies integration.

### 2.8 Headless subagent

A controller-supervised child assignment that is not a tmux pane and not an
ordinary headful conversation. Headless subagents include:

- read-only roles: scout, investigator, researcher, reviewer, planner, oracle,
  and delegate/context builder;
- a mutable worker role that executes in an isolated controller-owned working
  copy and writer container under the same one-writer rule as headful writers.

Headless subagents start with fresh bounded context by default; an explicit
full-history fork is permitted only when the complete parent history is
required. They may run in parallel and asynchronously, expose status, wait,
interrupt, stop, steer, and resume controls, escalate decisions or send
progress to the parent through the supervisor channel, and produce durable
results and lifecycle records outside the repository. Workers never share a
working copy with another writer, and ordinary children cannot launch
uncontrolled descendants. No automatic time, token, turn, or tool limit
applies; the same semantic boundaries that constrain headful agents constrain
headless children.

## 3. Presentation ownership

### 3.1 Tmux grids

Presentation chrome answers **where and which conversation**:

```text
mlre-transition · personal
mlre-transition · secretary
mlre-transition · retry cancellation
host maintenance
```

Tmux MUST consume controller-provided human labels. It MUST NOT own project,
working-copy, conversation, run, or headless-subagent identity.

The secretary grid (`pisec`) and the personal grid (`pi-personal`) each expose
one durable conversation per project for their own role. Each grid has an
independent, explicitly configured ordered active project set with
register/list/activate/swap operations; grid membership and ordering are
controller-owned presentation preferences, never conversation identity.
Desktop layout groups conversations into side-by-side panes; mobile layout
uses one full-width pane per window. Navigation and repair never kill a live
thinking conversation.

Herdr is deferred for this release. Presentation concepts remain
backend-neutral: switching presentation never migrates or kills a live worker,
and a stopped conversation may be relaunched into any supported backend.

### 3.2 Pi footer

Pi's footer answers **which model and what observable activity**:

```text
Luna Max · reasoning max · reading files · context 38%
Luna Max · reasoning max · running tests · context 41%
Luna Max · 2 reviewers working · context 43%
Luna Max · waiting for your decision · context 43%
```

The existing statusline selection of model, reasoning, tools/activity, and
context is the intended division. Healthy runtime implementation details MUST
NOT appear there.

Do not display during healthy operation:

- container or sandbox names;
- route IDs or session hashes;
- resource versions or fencing tokens;
- Git object IDs;
- lease state;
- "synced" without a named comparison;
- runtime specification hashes;
- generated worktree paths.

### 3.3 Technical details

Exact paths, OIDs, process identity, route manifests, container labels, and
controller operation records remain available through a read-only diagnostic
view such as `/observe` or a future `/control status --technical`. They are not
the primary workflow UI.

## 4. Secretary workflow

### 4.1 Project status

A request such as "current project stats" MUST refresh live controller and Git
observations before answering. The secretary MUST distinguish observed fact,
recorded intent, and inference.

Minimum response shape:

```text
Recent direction
• bounded synthesis of recent commits and recorded project decisions

Working copies
Work                Purpose                 Branch              State / attention
──────────────────  ──────────────────────  ──────────────────  ─────────────────
Personal MLRE       Direct project work     feature/workspaces  working
Retry cancellation  Fix late cancellation  pi/retry-cancel     needs one decision

Changes awaiting integration
Change              Target  Revision  Target movement  Interaction
──────────────────  ──────  ────────  ───────────────  ─────────────────────
Transition recovery main    3         unchanged        independent
Retry cancellation  main    1         moved            overlaps child lifecycle

Unmanaged Git worktrees
• path/branch/status visible for inspection; no controller ownership implied
```

A clean, old, inactive, or merged branch MUST NOT be described as abandoned,
complete, safe to delete, or owned without stronger evidence.

### 4.2 Direction and dependency discussion

The secretary SHOULD answer cheap questions from deterministic project state
and bounded history. It SHOULD launch headless read-only investigators only
when code-surface or semantic analysis is needed.

It MUST distinguish:

- textual overlap;
- shared API/schema/state-machine surfaces;
- ordering dependencies;
- target ancestry;
- decision dependencies;
- speculation requiring investigation.

Example:

> **User:** I am interested in X, Y, and Z. What are the dependencies?
>
> **Secretary:** X can start from current main. Y consumes the API currently
> being changed by workstream P, so its interface is not stable. Z is code-
> independent but depends on the schema-format decision. I recommend starting X,
> mapping the exact Y/P seam, and deciding the schema before starting Z.

### 4.3 Creating a workstream

Before creation the secretary MUST refresh:

- target ref and commit;
- project trust;
- existing workstream titles and branches;
- working-copy ownership;
- known changed surfaces;
- active integration operations.

The approval card MUST state:

- human title and purpose;
- selected target and starting point in human terms;
- known overlap or dependency;
- whether a separate worktree and headful conversation will be created;
- what the implementation agent will receive;
- that creation does not integrate anything.

The secretary reports "ready" only after the controller verifies the worktree,
session, run, and runtime assignment. Partial setup is reported as incomplete
with no claim that an agent started.

### 4.4 Progress and attention

Routine progress is durable but non-interrupting. Attention is separate from
lifecycle state.

Useful attention categories:

- decision needed;
- implementation interrupted, work retained;
- checks failing;
- state ambiguous;
- submitted revision superseded;
- integration conflict;
- runtime unavailable;
- headless subagent needs a decision or reported failure.

The secretary does not relay arbitrary chat between sessions. It focuses the
headful conversation when user discussion is required.

### 4.5 Submitted changes

When an implementation submits a change, the secretary presents it in the local
change queue. It does not automatically create a review, mark it accepted, or
integrate it.

The user may ask the secretary to:

- inspect the change;
- compare it with current target and other work;
- request an exact-revision review;
- focus the originating agent;
- create an integration workstream;
- integrate after explicit approval;
- close without integration;
- retain or clean resources after integration.

## 5. `pi-personal` workflow

### 5.1 Starting direct work

The default personal behavior MUST be explicit in controller policy rather
than inferred from checkout dirtiness.

Supported choices:

1. use the registered primary checkout on its current or explicitly selected
   branch (the default);
2. create a separate worktree and separate conversation;
3. inspect without starting implementation.

A clean checkout MUST NOT silently force a worktree while a dirty checkout
silently selects in-place behavior.

A separate feature worktree SHOULD receive a separate durable headful
conversation. The persistent personal conversation SHOULD remain bound to its
project home rather than changing cwd under a live session.

### 5.2 Informal direct work

`pi-personal` may begin without a formal secretary brief. Before the first
mutation, the controller records:

- working copy and branch;
- target branch if known;
- exact Git baseline;
- existing staged, unstaged, and untracked state;
- writer ownership.

This baseline allows later change submission to distinguish task changes from
pre-existing work. If attribution becomes ambiguous, the system asks rather
than combining files silently.

### 5.3 Submission

At implementation handoff:

> **Personal agent:** The launcher implementation is submitted to the Dotfiles
> secretary. Seven checks passed. I did not integrate it or clean your working
> folder.

The exact semantics are defined in `CHANGE_INTEGRATION_CONTRACT.md`.

## 6. Implementation-agent workflow

An implementation agent ends one bounded implementation cycle by doing one of:

- submit an exact change revision;
- report a decision blocker while retaining work;
- report that no change is warranted, with evidence;
- report an interrupted or failed state without pretending it submitted a
  revision.

"Complete" in an agent report means its assigned implementation and validation
are complete. It never means the project target contains the change.

## 7. Integration workflow

### 7.1 Analysis

For a selected revision, the secretary obtains:

- target commit now and at submission;
- ancestry and merge base;
- deterministic textual-conflict preview;
- changed files and diffstat;
- active and submitted changes touching the same surfaces;
- review evidence for the exact revision;
- acceptance evidence supplied by the implementation;
- focused semantic analysis when warranted.

### 7.2 Decision

The secretary describes consequences, not internal mechanism:

```text
Git reports no textual conflict, but both changes assign responsibility for
child completion. Integrating the workspace change first will require adapting
the cancellation change and rerunning one race test.
```

The user chooses whether to:

- integrate now;
- request further review;
- return to the implementation agent;
- create an integration workstream;
- defer;
- close the change without integration.

### 7.3 Mutation

Only an explicit current-turn integration authorization permits target
mutation. A review acceptance, generic "yes," workstream-creation approval, or
cleanup approval is not integration authorization.

Integration revalidates the target immediately before compare-and-swap. Target
movement never causes an implicit rebase, reset, or overwrite.

## 8. Human-facing state language

Prefer workflow sections over status proliferation:

- **Working now**
- **Changes awaiting integration**
- **Needs attention**
- **Integrated recently**

Change lifecycle terms are:

- **Open** — at least one revision has been submitted and not integrated or
  explicitly closed;
- **Merged** — an authorized integration record proves the target contains the
  accepted result;
- **Closed** — explicitly closed without integration or superseded by another
  change with recorded provenance.

Review, conflict, target movement, checks, attention, and headless-subagent
states are evidence or conditions, not independent change lifecycle states.

## 9. Failure-message contract

Every actionable control-plane error MUST answer:

1. what operation was attempted;
2. what disagreement or risk was observed in user terms;
3. whether files, refs, sessions, or containers changed;
4. what state was preserved;
5. the safe next choices;
6. how to inspect technical details.

Examples:

### Wrong working state

```text
I stopped before editing. This conversation reopened an older version of the
MLRE work than the version last used here. I found and preserved both versions.

• Resume the newer work
• Compare the versions
• Keep them separate
• Show technical details
```

### Existing writer

```text
Another Pi conversation is currently changing this working copy.

• Focus that conversation
• Wait for it to finish
• Create separate work
• Show technical details
```

### Runtime permission failure

```text
I could not prepare the tool environment because part of its private working
area has the wrong ownership. Your project files were not changed.

• Retry safe recreation
• Open read-only
• Show technical details
```

### Ambiguous recovery

```text
I found two different continuations and cannot prove that either should replace
the other. I changed nothing.

• Compare them
• Continue from the first
• Continue from the second
• Keep both
```

## 10. Compaction continuity

After successful compaction, Pi MUST render a user-visible continuity card with:

- retained project/conversation goal;
- current task or program slice;
- accepted decisions;
- work submitted or changed so far;
- unresolved decisions and blockers;
- the first retained entry or equivalent boundary;
- a command or action to inspect the full compacted summary.

The card MUST omit hidden chain-of-thought, secrets, raw system prompts, and
unbounded transcripts. The model-visible and user-visible summaries MUST derive
from the same compacted result so they cannot silently contradict each other.

The user may collapse the card, but it remains recoverable after resume.

## 11. Action vocabulary and authorization

The product uses these meanings consistently:

- **Inspect / show / plan / compare** — read-only. These actions do not imply a
  later mutation and normally need no confirmation beyond access policy.
- **Start work here** — create a draft work record and acquire the selected
  working-copy writer assignment; it does not create a separate worktree unless
  stated.
- **Create a workstream** — allocate a separate worktree and durable headful
  conversation after a dedicated confirmation.
- **Commit** — create a Git commit in the currently assigned source branch. It
  does not submit, integrate, push, or clean up unless those actions are
  separately requested.
- **Submit this change** — create an immutable local change revision for the
  secretary. It does not modify the target or push remotely. A configured
  implementation workflow may submit automatically at its accepted completion
  boundary only when the exact task scope is unambiguous and that policy was
  established when work began; ambiguous personal changes require confirmation.
- **Review** — inspect one exact submitted revision and return evidence. It
  grants no mutation authority.
- **Integrate / merge this change** — apply one exact submitted revision to one
  exact expected local target under a dedicated current authorization.
- **Publish / push** — perform a remote operation. It is always separate from
  local integration and requires explicit current authorization.
- **Clean up / remove** — delete exact controller-owned retained resources after
  a dry-run plan and dedicated authorization. It is never implied by merge.
- **Resume / focus** — reopen an existing conversation/run context. It does not
  retry a failed mutation.
- **Retry** — re-execute the same idempotent operation after reconciliation. It
  does not create a new intent or choose a different source/target.
- **Approve / yes** — valid only for the exact interactive request currently
  displayed. It is not a reusable or cross-action authorization.

Authorizations are bound to operation kind, project/resource, exact revision or
resource version/OID where applicable, request context, and expiry. They are
cancelled by changed scope. Natural-language interpretation may propose the
operation, but a consequential semantic tool presents/binds the final exact
request. Stale, replayed, generic, or ambiguous approvals fail closed.

## 12. Performance contract

Normal project-state inspection uses controller projections and bounded Git
queries. It MUST NOT launch an LLM or recursively scan project content merely
to list current work.

Semantic dependency analysis is on demand and cacheable by exact source and
target revisions. Caches are observations and may be discarded; cache keys do
not define identity.

`ACCEPTANCE_PLAN.md` defines baseline measurements and regression thresholds.
No unsafe state reuse is justified solely by startup performance.

## 13. Product non-goals

- The secretary is not an autonomous product manager or merge authority.
- `pi-personal` is not forced into a worktree for every task.
- Headless subagents never become an uncontrolled way to bypass the one-writer
  rule, scoped reads, or exact approvals.
- Every Git branch is not automatically controller-owned.
- An unmanaged worktree is inspectable but not mutable by controller operations
  until explicitly adopted.
- A submitted revision is not automatically reviewed or merged.
- Presentation backend selection does not migrate active processes.
- User-facing UI does not expose implementation jargon as a substitute for a
  consequence-oriented explanation.
