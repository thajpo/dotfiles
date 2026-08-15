# Pi Web Control Plane Plan

Status: proposed implementation plan

## 1. Product Decision

Pi Web is a browser control surface over the Pi controller.

It is not:

- a web terminal;
- a tmux session browser;
- a second controller;
- a separate chat product;
- a generic observability dashboard.

The primary object is a project. The browser helps the user understand what is
happening across projects, find work that needs attention, inspect changes and
runs, and act on exact controller decisions. A polished conversation view is a
detail page inside that product.

The product should answer these questions quickly:

1. What needs me now?
2. What is working, waiting, failed, or ready?
3. What is happening in this project?
4. What did this agent say or do?
5. What exact decision am I being asked to make?

The user should never need to understand tmux grids, session files, run IDs,
container names, generated worktree paths, writer epochs, or controller
resource versions during healthy use.

## 2. Product Boundaries

The following boundaries are non-negotiable.

- The controller remains authoritative for projects, conversations, runs,
  workstreams, messages, changes, reviews, integration, attention, and exact
  authorizations.
- Pi session JSONL remains authoritative for conversation content.
- Pi Web derives all identity from controller records. Browser route parameters
  never create project, conversation, or run identity.
- Tmux remains an optional presentation client. Pi Web does not scrape pane
  text, issue `send-keys`, or infer state from pane layout.
- Pi Web does not open the controller SQLite database from frontend code.
- All mutations use existing semantic controller operations with exact request
  identity, scope, expiry, idempotency, and replay protection.
- Chat access must attach to the controller-selected conversation and session
  file. It must not start a parallel generic Pi conversation.
- Thinking text, system prompts, secrets, raw provider payloads, and unbounded
  tool output are not sent to the browser.
- The first release is single-user and private-tailnet only.

## 3. UX Model

The user-facing hierarchy is intentionally small:

```text
Home
  -> Project
       -> Work item
       -> Change
       -> Conversation
       -> Decision
Inbox
  -> Decision or attention item
```

There is no top-level `pisec`, `pi-personal`, tmux, pane, or session route.
Secretary, personal, workstream, reviewer, and investigator are conversation or
agent roles shown within a project.

### 3.1 Global Navigation

Mobile has three persistent destinations:

```text
Home       Projects       Inbox
```

- **Home** summarizes current activity and recent outcomes across projects.
- **Projects** lists registered projects with concise state and attention
  counts.
- **Inbox** contains only items that need a user decision or acknowledgement.

Inbox is the canonical cross-project action list. Home and project pages show
bounded previews that link to the same inbox items; they do not maintain
independent read or resolution state.

Desktop uses the same information architecture. It expands navigation into a
left rail instead of inventing additional pages.

Chat is deliberately absent from global navigation. A conversation belongs to
a project and is opened from project context, a work item, a change, a message,
or an inbox item.

### 3.2 Home

Home is a quiet operational summary, not a wall of telemetry.

It contains these sections in order:

1. Needs attention
2. Working now
3. Changes awaiting review or integration
4. Completed recently

Each row shows:

- project name;
- human title;
- role or work type;
- plain-language state;
- relative time;
- whether user action is required.

Rows are derived from `attention`, `project_messages`, `work_index`, changes,
reviews, and active runs. Healthy implementation details stay hidden.

When there is no attention, the page should say so plainly and emphasize active
work rather than displaying an empty dashboard.

### 3.3 Projects

The project list is optimized for recognition rather than administration.

Each row shows:

- project display name;
- current activity summary;
- open attention count;
- open change count;
- last useful update.

Registration paths, trust hashes, Git common directories, and controller IDs
belong in technical details, not the list.

Search filters by project display name. Sorting defaults to attention first,
then recent activity.

### 3.4 Project Workspace

A project page is the main control-plane workspace. It is one vertically
ordered page on mobile, not a dense tab matrix.

Sections appear only when they have content:

1. Needs attention
2. Working now
3. Changes
4. Conversations
5. Investigations and reviewers
6. Recent outcomes

The existing `work_index` projection provides the initial structure. The web
projection should normalize its current display buckets into stable typed
items rather than exposing section names as an API contract.

The Conversations section normally includes:

- project secretary;
- project personal agent;
- active workstreams;
- relevant reviewer or integration conversations.

Inactive and archived conversations are available through `Show history`; they
do not crowd the default page.

When a project has little or no managed work, the page says `No active Pi work`
and offers its available secretary or personal conversation. It does not
collapse into an unexplained empty screen.

### 3.5 Object Detail

Attention items, work items, changes, investigations, and conversations open a
detail route. Mobile uses a full-screen route with a normal back action.
Desktop opens the detail in a right-hand pane while retaining project context.

A detail page always answers:

- what this is;
- where it belongs;
- its current state;
- the latest useful evidence;
- what the user can do next.

Technical details are available through one collapsed disclosure. They are
never mixed into the primary state sentence.

## 4. Conversation UX

Conversation is the richest detail view, but it does not define the app shell.

### 4.1 Header

The header shows:

- back to project;
- project name;
- role and conversation title;
- current state: idle, working, waiting, interrupted, or unavailable;
- compact overflow menu.

The overflow menu contains secondary actions such as compact, inspect session
details, focus in tmux, and stop. Destructive or uncommon actions do not occupy
the main header.

Compaction opens a confirmation that explains it will summarize the active
model context while retaining the full durable session history. The action is
disabled while the runtime cannot safely accept compaction.

### 4.2 Timeline

The timeline renders normalized entries:

- user messages;
- assistant Markdown;
- compact tool activity;
- durable project messages;
- change submissions;
- investigation results;
- decision requests;
- failures and continuity summaries.

Tool activity is summarized in human terms, for example:

```text
Read 4 files
Ran focused tests (18 passed)
Started reviewer: API boundaries
Submitted change revision 2
```

Each activity row can expand to bounded technical details. Raw tool arguments,
unbounded output, hidden thinking, and provider payloads remain omitted.

Streaming assistant text updates in place. A reconnect first loads the durable
timeline, then resumes from a stable cursor so text is neither duplicated nor
lost.

### 4.3 Composer

The composer is fixed above the mobile safe area and supports multiline input.

When the conversation is idle, the primary action is `Send`.

When the conversation is working, the UI presents two explicit choices:

- `Steer now` delivers guidance at the next safe point.
- `After current work` queues a follow-up after the current run settles.

The UI must not hide this distinction behind Enter versus Alt-Enter. The
selected behavior is visible before submission.

Queued messages appear above the composer and can be removed before delivery.
`Stop` is visible only while work is active and always requires a confirm step.

When a conversation is waiting for the user, the composer returns to the normal
`Send` action and any exact pending decision is pinned immediately above it.
When liveness is unknown, input is disabled until the bridge or controller
proves whether the current run can accept it.

### 4.4 Unavailable Conversations

If no live run can accept input, the timeline remains readable. The composer is
replaced by a clear action:

```text
This conversation is not running.
[Show recovery options]
```

The first release does not start a stopped conversation from the browser.
Recovery options explain the installed launcher or focus action. A later web
start action requires a dedicated semantic controller operation; the gateway
must never spawn an unregistered Pi process or issue tmux `send-keys`.

### 4.5 Conversation History

History is accessed inside the project or current conversation. It is not a
global session-file browser.

The history list shows human title, role, state, and last useful update. Branch
and compaction structure can be added after the core control-plane workflows
are accepted.

## 5. Inbox And Decisions

Inbox is a projection of unresolved user attention, not a second messaging
system.

It includes:

- needs-user project messages;
- interrupted or failed work requiring a choice;
- workstream proposals;
- review requests requiring confirmation;
- integration decisions;
- package and host-command requests;
- ambiguous recovery choices.

Reading an item does not silently resolve it. Acknowledge and resolve are
separate explicit actions.

### 5.1 Decision Card

The same decision component is used in Inbox and inline in Conversation.

The compact card shows:

- requested action;
- project and originating conversation;
- consequence in user terms;
- expiry or stale state;
- `Review decision` action.

`Review decision` opens a full-height mobile sheet or desktop side panel. It
shows:

- exact action and purpose;
- affected project, working copy, target, or revision;
- expected effect and known risk;
- what has already changed;
- what remains preserved if rejected;
- expiry;
- bounded technical details;
- Approve and Reject actions.

There is no approve button on a collapsed summary row. The user sees the full
request before deciding.

### 5.2 Authorization Semantics

Approval is bound to:

- authenticated web identity;
- operation kind;
- decision-kind-specific request context, including request ID and digest where
  present;
- project and conversation;
- exact revision, target, or command scope;
- current resource version where applicable;
- active controller build and restart epoch;
- expiry;
- one-use authorization state.

The gateway re-reads and revalidates the request immediately before approval
and execution. A stale card changes to `Request changed` and cannot submit the
old decision.

High-consequence approvals use passkey step-up authentication. The first
release includes host commands and package operations. Integration approval
ships in the first release only if the controller exposes and tests a stable
passkey-bound request context. Workstream creation may use the normal
authenticated web session after the exact card is shown. Publish, activation,
rollback, and destructive cleanup remain unavailable from the first web
release.

TTY approval remains available as a fallback. Web approval adds another
explicit user actor; it does not weaken or simulate `/dev/tty`.

## 6. Responsive Design

### 6.1 Mobile

Below 720 CSS pixels, every route is single-column.

- Bottom navigation contains Home, Projects, and Inbox.
- Detail pages replace the list and use a normal back transition.
- Conversation uses the full viewport and safe-area-aware composer.
- Decision review uses a full-height sheet with the final actions in thumb
  reach.
- Touch targets are at least 44 by 44 CSS pixels.
- No horizontal tables are required for normal workflows.
- Dense Git and review data becomes stacked label/value rows.

### 6.2 Tablet

From 720 to 1099 CSS pixels, the app uses two columns where useful:

- navigation or project list;
- selected project or detail.

Conversation can take the full content column. Decision sheets remain modal so
their scope is visually isolated.

### 6.3 Desktop

At 1100 CSS pixels and above, the app uses a restrained master-detail layout:

```text
Global rail | Project workboard | Selected detail or conversation
```

Columns collapse when not needed. The app must not turn every controller table
into a permanent panel.

## 7. Visual Language

The visual direction is a quiet engineering operations console, not a terminal
theme and not a generic chat clone.

- Use warm neutral surfaces with strong text contrast.
- Use one accent color for actions and restrained role accents for recognition.
- Always pair role color with a text label or icon.
- Use proportional UI text; reserve monospace for commands, paths, diffs, and
  exact identifiers.
- Prefer grouped rows and section rhythm over nested cards.
- Use motion only for route transitions, streaming state, and decision state
  changes.
- Avoid giant headers, metric tiles, gradients, decorative charts, and status
  badge proliferation.

The default theme should support dark and light system preferences. Dark mode
must not use pure black for every surface. Code and diff rendering must remain
legible at phone width without forced horizontal scrolling for ordinary lines.

## 8. Accessibility

- Meet WCAG 2.2 AA contrast and keyboard requirements.
- Preserve visible focus and logical focus order.
- Announce streaming completion, errors, and new decision requests through
  polite live regions.
- Do not continuously announce streaming token deltas.
- Expose state through text, not color alone.
- Respect reduced motion.
- Keep approval actions semantically named and separated to prevent accidental
  taps.
- Restore focus to the originating item after closing a detail or decision.

## 9. Technical Architecture

```text
Phone or desktop browser
        |
        | HTTPS inside the tailnet
        v
Tailscale Serve
        |
        | loopback only
        v
Pi Web gateway
        |                         |
        | controller API         | per-run private bridge
        v                         v
PiControllerClient           Pi web-session extension
        |                         |
        v                         v
controller SQLite            live Pi AgentSession
                                  |
                                  v
                         controller-selected session JSONL
```

### 9.1 Web Gateway

Implement one host-local gateway responsible for:

- authenticated HTTP API;
- static frontend assets;
- controller projection reads;
- exact semantic mutations;
- event streaming;
- safe conversation timeline projection;
- proxying input and lifecycle control to the exact live conversation bridge.

The gateway binds only to loopback. Tailscale Serve provides tailnet HTTPS and
forwards authenticated identity headers. Loopback processes run inside the
same-user host trust boundary; the design does not claim that forwarded headers
are meaningful against a malicious process already running as the same user.

The initial implementation should use Python with an async HTTP/SSE library so
it can call `PiControllerClient` directly without a second SQLite stack. Any
new dependency must be exact-pinned, reviewed, offline-materialized, and added
to installed acceptance.

### 9.2 Frontend

Build a small TypeScript SPA under `pi/web/`.

Candidate stack, to be selected by the preflight slice:

- Preact for view composition without a component framework;
- Vite for deterministic production assets;
- a small router;
- sanitized CommonMark rendering;
- plain CSS with design tokens;
- no client state library until demonstrated necessary.

The frontend stores only transient view state. Controller state and chat
history are always reloadable from the gateway.

Do not cache API responses, conversation content, or approval details in a
service worker. An installable web manifest may be added without offline data
caching.

### 9.3 Controller Projections

Add stable, versioned web projections rather than exposing raw table rows.

Required projections:

- global summary;
- project list summary;
- project workspace;
- inbox;
- work item detail;
- change detail;
- conversation summary;
- normalized conversation timeline;
- exact decision display.

The current `project_status` and `work_index` implementations are source
material, not final public JSON contracts. New projections should use
camelCase, explicit schema versions, stable item kinds, and bounded result
sizes.

Normal list and detail reads must not launch an LLM, recursively scan a
repository, or refresh Git unless the user explicitly requests a live refresh.

### 9.4 Live Conversation Bridge

Add a controller-installed Pi extension for headful secretary, personal,
workstream, and integration conversations.

On `session_start`, the extension opens a private per-run Unix socket under the
Pi state root. The supervisor provides exact run and conversation capability
material. The socket is mode `0600`, owned by the current user, and removed on
`session_shutdown`.

The bridge capability is bound to one controller build, restart epoch, run,
conversation, session ID, and child process start identity. The gateway sends
all expected bindings during the handshake. A mismatch closes the socket
before history, events, or input become available.

The bridge exposes a small versioned protocol:

- subscribe to normalized session lifecycle events;
- send an idle prompt;
- send a steering message;
- queue a follow-up;
- remove an undelivered queued input;
- abort current work;
- request compaction;
- inspect bounded runtime state.

The extension uses Pi's supported `sendUserMessage` behavior. Existing
subagent steering already demonstrates correlated extension-sourced steering.
The preflight slice must prove idle, steer, follow-up, abort, compact, and event
subscription behavior against the pinned Pi version before the bridge protocol
is accepted.

Tmux and browser input are two UI sources for one Pi process. The bridge does
not claim an exclusive lease over the TTY. Pi serializes accepted input. Each
browser submission carries an idempotency key and receives an accepted order
and delivery state. If an idle prompt races with TTY input and Pi is no longer
idle, the bridge rejects it with a conflict and asks the browser to choose
`Steer now` or `After current work`; it never silently changes delivery mode.
Multiple browsers may observe. Concurrent browser submissions follow the same
idempotent acceptance rules rather than acquiring a new controller authority.

Browser input is not automatically replayed across a Pi run replacement. The
bridge records a correlated acceptance marker in the durable session before it
reports `accepted`. If the run ends with an accepted steer or follow-up still
pending, the UI reports `not delivered` when proved or `delivery uncertain`
after an unclean loss. Gateway reconnect to the same live run may resume
observation; a new run never silently receives old queued input. The user must
review and resend uncertain input explicitly.

### 9.5 Timeline Projection

The gateway resolves the session file only through the selected controller
conversation. It verifies that the path remains beneath the Pi state root and
matches the conversation binding before reading it.

The timeline projector returns only browser-safe entries:

- visible user and assistant text;
- custom display messages;
- bounded tool summaries;
- usage and activity summaries;
- compaction continuity cards;
- controller messages and decisions linked by resource ID.

The projector omits:

- thinking blocks;
- system and developer prompts;
- raw provider requests and responses;
- secret-bearing environment data;
- full command output unless explicitly safe and bounded;
- internal capability material.

Tool summaries come only from an explicit allowlist of typed Pi lifecycle
events and structured tool-result details. They never count tests, files, or
changes by parsing arbitrary stdout. Unknown tools render as `Tool completed`
or `Tool failed` with no inferred claim.

Durable entries use the Pi session entry ID as cursor. Live bridge events use a
run-scoped sequence and reconcile to durable entries after append.

### 9.6 Event Delivery

Use bounded polling for controller projections in the read-only release. Add
Server-Sent Events with the live conversation bridge.

- Controller events are read from `control_events` after a sequence cursor.
- Conversation events are proxied from the private run bridge.
- Reconnect uses `Last-Event-ID` and refetches the affected projection.
- Events invalidate projections; they do not carry unrestricted database rows.
- A bounded heartbeat detects mobile network suspension.

WebSocket is unnecessary until a concrete bidirectional streaming limitation is
observed. Commands remain ordinary authenticated POST requests.

## 10. Local API

The gateway exposes `/api/v1` only through the authenticated tailnet origin.

Initial read endpoints:

```text
GET /api/v1/bootstrap
GET /api/v1/summary
GET /api/v1/projects
GET /api/v1/projects/{projectId}
GET /api/v1/projects/{projectId}/changes/{changeId}
GET /api/v1/projects/{projectId}/conversations/{conversationId}
GET /api/v1/projects/{projectId}/conversations/{conversationId}/timeline?after={entryId}
GET /api/v1/inbox
GET /api/v1/decisions/{decisionKind}/{requestId}
```

Slice 3 adds the authenticated event stream:

```text
GET /api/v1/events
```

Initial conversation commands:

```text
POST /api/v1/conversations/{conversationId}/input
POST /api/v1/conversations/{conversationId}/abort
POST /api/v1/conversations/{conversationId}/compact
DELETE /api/v1/conversations/{conversationId}/queue/{inputId}
```

Initial decision commands are separate per decision kind even when the routes
share a visual component. Command, package, workstream, and integration
requests retain their distinct exact binding fields.

Initial decision commands:

```text
POST /api/v1/decisions/{decisionKind}/{requestId}/approve
POST /api/v1/decisions/{decisionKind}/{requestId}/reject
POST /api/v1/messages/{messageId}/acknowledge
POST /api/v1/messages/{messageId}/resolve
```

Every mutation request includes:

- idempotency key;
- expected request digest or resource version;
- CSRF token;
- browser session identity;
- passkey assertion when required.

Authorization revalidation also binds the active controller build and restart
epoch. Integration is not enabled in the web UI until its controller operation
exposes a stable request context that can be bound into the passkey challenge
and revalidated immediately before mutation.

Responses use the control-plane failure contract and state whether anything
changed, what was preserved, and the safe next choices.

## 11. Authentication And Security

### 11.1 Network Boundary

- Bind the gateway to loopback.
- Publish only through Tailscale Serve HTTPS.
- Do not bind a raw HTTP server to `0.0.0.0` or the LAN interface.
- Do not place bearer credentials in URLs.
- Refuse non-loopback direct binding in the first release.

### 11.2 Browser Session

- Establish identity from trusted Tailscale Serve headers.
- Issue a short-lived `Secure`, `HttpOnly`, `SameSite=Strict` session cookie.
- Enforce exact Origin and Host checks.
- Protect mutations with CSRF tokens and content-type checks.
- Rotate sessions after passkey enrollment and step-up authentication.
- Log only bounded actor/resource/action metadata, never chat content or
  approval secrets.

### 11.3 Passkeys

Use WebAuthn for high-consequence approval step-up.

- Registration is local and requires the existing controlling-TTY flow.
- Credential public keys and counters live under the protected Pi state root.
- Challenges bind web session, decision-kind-specific request context, action,
  controller build, restart epoch, and expiry.
- A successful assertion is single-use.
- Counter rollback, changed decision context, stale resource state, or expired
  challenge fails closed.

Enrollment is a separate TTY-gated ceremony:

1. The user runs `pi-web passkey enroll` at a controlling TTY.
2. The CLI creates a short-lived one-use enrollment transaction and prints a
   code that must be typed into the already authenticated web settings page.
3. The browser creates a WebAuthn credential for the tailnet HTTPS origin.
4. The CLI displays the Tailscale identity, credential fingerprint, and expiry.
5. The user types `APPROVE` or `REJECT` at the TTY.
6. Only approval stores the public credential and counter under the protected
   Pi state root; the private key remains in the phone authenticator.

Enrollment codes are never placed in URLs, are bound to the authenticated
tailnet identity, and expire without changing credential state.

### 11.4 Cross-Project Isolation

The gateway resolves every nested resource through its project. A valid
conversation ID from one project cannot be substituted into another project
route. Tests cover horizontal ID swapping for all reads and mutations.

## 12. Changed System Surfaces

### Public API

Add a private, versioned local web API and a private versioned run-bridge
protocol. Neither is public-Internet supported.

### Types And Schema

Add typed projection schemas and transport envelopes. Avoid a controller
database schema change in the first release. Runtime chat input is delivered
through the private live bridge, not a new durable command table.

### Persistence

Reuse controller SQLite, Pi session JSONL, and existing event records. Persist
only web authentication configuration, passkey public credentials, and bounded
gateway settings under the Pi state root.

### State Transitions

Do not invent web-specific lifecycle states. The UI maps existing controller
states into stable human-facing language.

### Authorization

Add `web-user` as an audited user actor for exact authorization records.
Preserve controller build, restart epoch, decision-specific context, digest
where present, expiry, scope, one-use, and immediate revalidation semantics.
Message acknowledge and resolve do not currently record an actor in controller
state; the first release records them in bounded gateway access logs and does
not claim durable per-user controller audit until the schema supports it.

### Transactions

Use current controller transactions for decisions and mutations. The web
gateway never performs half of an approval outside the controller transaction.

### Concurrency

Allow multiple observers. Serialize browser submissions through Pi's input API
and report conflicts with simultaneous TTY input explicitly. Preserve the
controller's one-writer-per-working-copy rule unchanged.

### Retries And Cancellation

Reads are safely retryable. Mutations require idempotency keys. Abort and input
delivery return accepted/delivered/completed states rather than claiming model
compliance.

### Error Semantics

Use the six-part product error contract. Network loss is distinct from agent
failure. Unknown liveness is shown as unknown and never grants authority.

### Dependencies

Add only the async HTTP/SSE server and minimal frontend dependencies. Pin exact
versions, review lifecycle scripts, materialize offline, and include them in
the installed resource inventory.

### Performance

Target these local-tailnet budgets on a mid-range Android phone over the same
tailnet, measured from browser Performance API marks and gateway monotonic
timers over at least 30 warm runs:

- initial shell under 1.5 seconds on warm load;
- project summary response under 250 ms p95 without Git refresh;
- timeline reconnect under 500 ms for the latest 200 entries;
- live text delta visible within 250 ms p95 after receipt;
- initial JavaScript under 250 KiB compressed unless measured UX requires more.

Slice 5 turns these targets into recorded acceptance evidence. Earlier slices
report measurements without claiming a release gate.

### Deployment And Operations

Run the gateway as a systemd user service. Manage Tailscale Serve separately
through an explicit install command. Activation remains the reviewed Pi
control-plane path.

### Observability And Rollback

Expose local health, connected clients, event lag, bridge availability, and
bounded error counts. Rollback disables the user service and Tailscale Serve
route without modifying controller projects, conversations, runs, or sessions.

## 13. Implementation Slices

### Slice 0A: UX Fixture Prototype

Goal: validate information architecture before live mutations.

Build:

- static responsive shell;
- Home, Projects, Inbox, Project Workspace, Change Detail, Conversation Detail;
- representative fixtures for idle, busy, failed, stale, and approval states;
- mobile widths 360, 390, and 430 pixels;
- desktop master-detail layout.

Acceptance:

- a user can locate an attention item, identify its project, inspect the
  originating conversation, and return without seeing session or tmux jargon;
- a user can distinguish secretary, personal, and workstream conversations;
- an approval cannot be submitted from a collapsed row;
- keyboard and screen-reader navigation pass focused checks.

Stop condition: do not build the live gateway until the route model works on a
phone without explanatory text.

### Slice 0B: Runtime And Network Feasibility

Goal: reject unsupported bridge and identity assumptions before production
architecture depends on them.

Prove against the pinned Pi version:

- extension-sourced idle prompt;
- steer and follow-up with correlated acceptance;
- abort and compact from extension context;
- message and tool lifecycle subscriptions;
- browser and TTY input race behavior;
- clean socket teardown and run replacement;
- Tailscale Serve HTTPS and identity headers on this host;
- passkey operation in the resulting secure browser origin.

Acceptance:

- each bridge operation has a focused executable test or the plan is revised;
- simultaneous TTY and browser input has an observed, documented ordering or
  conflict result;
- no feasibility probe mutates controller schema or production project state;
- unsupported capabilities are removed rather than simulated with tmux
  `send-keys`.

### Slice 1: Read-Only Control Plane

Goal: replace fixtures with safe controller projections.

Build:

- authenticated tailnet gateway;
- bootstrap, summary, projects, project workspace, inbox, and change detail;
- bounded refresh and foreground polling;
- normalized errors;
- no mutations and no live chat input.

Acceptance:

- web projections agree with `pi-control project status`, `work-index`, message
  lists, and change lists;
- cross-project ID substitution fails;
- no LLM, Git refresh, tmux command, container command, or remote provider is
  invoked by normal browsing;
- read endpoints cannot invoke reconciliation or presentation mutation;
- restart preserves browser-visible controller state.

### Slice 2: Durable Conversation Reader

Goal: make project conversations pleasant to read.

Build:

- controller-resolved timeline projection;
- Markdown, code, tool summaries, continuity cards, and durable message cards;
- stable cursor pagination;
- sensitive-content filters;
- conversation history inside a project.

Acceptance:

- timeline content matches visible durable Pi messages without exposing
  thinking, prompts, secrets, or raw provider payloads;
- compaction and restart retain continuity;
- large sessions remain responsive through bounded pagination;
- a partial final JSONL line is ignored until complete and never returned as a
  malformed or duplicated timeline entry.

### Slice 3: Live Chat Bridge

Goal: interact with the exact existing Pi process from browser and tmux.

Build:

- installed web-session extension;
- private run socket and versioned handshake;
- live message and tool event streaming;
- idle prompt, steer, follow-up, queue removal, abort, and compact;
- explicit TTY/browser arbitration and reconnect behavior.

Acceptance:

- browser and tmux observe one conversation and one session file;
- browser input appears once in the durable session;
- steer and follow-up preserve their distinct Pi semantics;
- reconnect does not duplicate text or commands;
- a stale run socket cannot accept input;
- stopping the gateway does not stop Pi;
- restarting Pi invalidates the old bridge and reconnects to the new exact run;
- simultaneous TTY and browser submissions produce the preflighted ordering or
  explicit conflict behavior;
- accepted but undelivered input is reported as not delivered or uncertain and
  is never replayed automatically into a replacement run.

### Slice 4: Exact Decisions

Goal: handle controller decisions safely in the browser.

Build:

- decision projection and review sheet;
- acknowledge and resolve;
- workstream proposal approval;
- review and integration decisions;
- package and host-command approval;
- passkey enrollment and step-up;
- exact authorization actor records and bounded gateway access logs.

Acceptance:

- approve, reject, expiry, replay, changed digest, changed target, and changed
  resource version all have deterministic tests;
- passkey enrollment requires the exact TTY-approved identity and credential,
  while rejection, expiry, and code replay store nothing;
- high-consequence approval requires a fresh passkey assertion;
- identical decisions cannot execute twice;
- integration remains disabled until its exact passkey-bound request context is
  executable and independently tested;
- browser and TTY approval paths produce equivalent controller receipts with
  distinct actor identities;
- a generic chat `yes` never authorizes a decision;
- each decision kind is tested against its own binding fields rather than a
  generic request envelope.

### Slice 5: Installed Product Acceptance

Goal: make Pi Web a supported launcher and service.

Build:

- staged resources and exact dependency inventory;
- `pi-web` service/start/status/stop launcher;
- systemd user unit installation;
- Tailscale Serve setup instructions and doctor checks;
- installed browser journey using disposable controller state;
- release evidence and rollback verification.

Acceptance:

- final installed paths serve the UI and exact controller state;
- no host OpenCode/Pi configuration drift;
- no public listener exists;
- teardown leaves no gateway, bridge socket, test process, or fixture data;
- existing tmux, controller, Docker, recovery, and activation journeys still
  pass;
- rollback removes web reachability without altering durable Pi work.

## 14. Test Strategy

### Projection Tests

- empty state;
- multiple projects;
- active and archived conversations;
- open and resolved attention;
- review and integration queues;
- stale and unknown liveness;
- bounded payloads and stable ordering.

### Security Tests

- no identity header or untrusted proxy;
- expired browser session;
- CSRF and cross-origin submission;
- URL token leakage absence;
- horizontal project/conversation/resource swaps;
- session path traversal and symlink substitution;
- stale run bridge and replayed handshake;
- bridge capability mismatch for build, epoch, run, conversation, session, and
  child process identity;
- decision digest and resource-version mismatch;
- passkey challenge replay and counter rollback.

### Browser Tests

- 360 by 780 mobile viewport;
- 390 by 844 mobile viewport;
- tablet and desktop breakpoints;
- virtual keyboard and safe-area composer;
- back navigation and focus restoration;
- offline/reconnect banner;
- streaming, queued input, and interrupted run;
- reduced motion, high zoom, and keyboard-only operation.

### Installed Journeys

- day-open summary;
- inspect project work index;
- open secretary conversation and send prompt;
- steer a busy personal conversation;
- queue a follow-up;
- lose a run with queued browser input and verify no automatic replay;
- approve and reject exact workstream proposals;
- approve, replay, expire, and reject an exact host command;
- inspect and authorize an exact integration;
- restart grid while browser is connected;
- stop gateway while Pi continues;
- restart gateway and recover durable state.

## 15. File Map

Expected new areas:

```text
pi/web/                              TypeScript frontend
pi/extensions/web-session/          live Pi conversation bridge
scripts/pi-web-gateway.py            installed gateway entrypoint
scripts/pi_control/web_api.py        HTTP API and authentication boundary
scripts/pi_control/web_projection.py stable controller read models
scripts/pi_control/web_timeline.py   safe Pi session projection
scripts/pi_control/web_identity.py   Tailscale and passkey identity
tests/web/                           frontend and browser tests
tests/control_plane/test_web_*.py    projection, auth, decision tests
tests/system/fixtures/installed-web.py
```

Expected changed areas:

```text
scripts/pi_control/pi_client.py
scripts/pi_control/pi_protocol.py
scripts/pi_control/host_supervisor.py
scripts/pi_control/pi_install.py
pi/pi-resources.v1.json
tests/system/launcher-surface.v1.json
bin/pi-install
README.md
pi/README.md
```

Do not add a controller schema migration unless a later accepted requirement
cannot be met through existing controller records and the live bridge.

## 16. Explicit Non-Goals

- No raw terminal as the primary web experience.
- No tmux grid or active-set administration in the first release.
- No generic database browser.
- No raw session JSONL download in the normal UI.
- No public Internet deployment.
- No multi-user teams, role-based access control, or shared approvals.
- No mobile editing environment or browser IDE.
- No arbitrary shell command form.
- No replacement for GitHub pull requests or hosted CI.
- No remote activation, rollback, or control-plane upgrade from the first web
  release.
- No dashboard charts without a concrete user decision they improve.
- No push notifications until Inbox is useful without them.
- No offline cache of project or conversation content.

## 17. Decisions By Gate

Before Slice 1:

1. Confirm Preact and the selected async Python server after dependency review.
2. Confirm Tailscale Serve identity-header behavior on this host.

Before Slice 2:

1. Define the normalized timeline allowlist for every current Pi message type.

Before Slice 4:

1. Choose the WebAuthn credential storage format and recovery procedure.
2. Establish the exact browser-session and passkey step-up expiry durations.
3. Define exact passkey challenge fields for every supported decision kind.

These decisions do not block Slice 0A. Slice 0B settles the runtime and network
questions before Slice 1 begins.

## 18. First Implementation Target

The first implementation target is Slice 0A and Slice 0B, followed by Slice 1.
Do not begin production live chat or approval plumbing before both preflight
slices pass.

The first reviewable demo should show this phone journey:

1. Open Pi Web.
2. See two items requiring attention across projects.
3. Open one project.
4. Understand its active work and pending change without technical IDs.
5. Open the secretary conversation and read its durable history.
6. Return to Inbox with normal mobile navigation.

If that journey is not immediately understandable, adding streaming chat or
approval buttons will make the wrong product more expensive rather than making
it better.
