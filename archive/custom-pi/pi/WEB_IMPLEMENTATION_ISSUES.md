# Pi Web Implementation Issues

This is the local issue log for bugs and design gaps found while implementing
`WEB_CONTROL_PLANE_PLAN.md`. Entries stay local until explicitly published to
GitHub. Each entry records the evidence and disposition so a later release
does not silently lose a harness finding.

## Open

### WEB-001: Browser bridge was missing from the controller seam

- Severity: high
- Found: 2026-08-14, Slice 0B runtime audit
- Evidence: `controller-channel` only exposed startup attestation and
  request/response RPC. It had no Pi event subscription, user-message
  delivery, stop, compaction, or browser-session capability.
- Impact: implementing the web UI directly against session files or tmux
  would create a second authority and could race with TTY input.
- Disposition: implement `extension:web-session` as an exact per-run Unix
  bridge, gated by run, conversation, build, restart epoch, session, and
  capability identity. The direct bridge contract passes; the installed
  controller-run journey remains a release acceptance check.

### WEB-014: Shared Pi session-control dispatcher is still absent

- Severity: high
- Found: 2026-08-14, parity API audit
- Evidence: the web extension still owns its bridge protocol adapter, while
  session lifecycle and generic extension-command dispatch are not exposed
  through a shared controller operation surface used by TTY, RPC, and browser
  clients. The current parity slice now routes queue admission through pinned
  Pi preflight and exposes browser-owned queue removal, model selection, and
  thinking controls.
- Impact: the browser can drive the current live bridge, but it is not yet a
  complete Pi chat client and future browser-only behavior could diverge from
  TTY/RPC semantics.
- Disposition: add the pinned-Pi shared dispatcher only after the v2 bridge
  identity and delivery contract remain green. Keep host-only shell, login,
  sharing, trust, maintenance, and deployment operations outside this surface.
  The current parity slice adds a pinned 0.83.0 overlay that acknowledges
  extension input through `AgentSession.prompt()` preflight, binds browser
  idempotency keys to real Pi queue entries, exposes bounded queue state, and
  removes only browser-owned queued inputs. Session lifecycle and generic
  extension-command execution remain intentionally unavailable until their
  controller authority and browser-safe command allowlist are defined.

## Closed

### WEB-002: Controller-channel malformed-frame diagnostics had a latent NameError

- Severity: medium
- Found: 2026-08-14, controller-channel audit
- Evidence: `scripts/pi_control/controller_channel.py` referenced
  `os.environ` when `PI_DEBUG_FRAME_DUMP` was set, but did not import `os`.
- Impact: malformed-frame diagnostics failed with `NameError` instead of
  preserving the intended bounded debug dump and protocol error.
- Disposition: fixed in the current implementation and covered by
  `test_malformed_frame_debug_dump_preserves_protocol_error`.

## Deferred Checks

### WEB-003: Tailscale Serve identity behavior is not proven on this host

- Severity: high
- Found: 2026-08-14, Slice 0B network audit
- Evidence: no local Tailscale Serve identity-header contract or active Serve
  endpoint was available to test.
- Impact: the gateway must not trust an unvalidated identity header or expose a
  non-loopback listener.
- Disposition: current gateway binds loopback only and has no proxy-header
  authentication. Prove Tailscale Serve identity and browser-session binding
  before enabling remote tailnet access.

### WEB-004: Passkey enrollment and credential storage are not implemented

- Severity: high
- Found: 2026-08-14, Slice 1 security review
- Evidence: the controller has authorization records but no WebAuthn
  enrollment ceremony or protected credential store.
- Impact: high-consequence browser approvals must remain unavailable until
  enrollment, challenge binding, replay protection, and recovery are tested.
- Disposition: keep decision mutations out of the read-only release; implement
  only in Slice 4 after the TTY-gated enrollment decision is accepted.

## Closed (Parity Slice)

### WEB-011: Bridge identity conflated controller and installed build IDs

- Severity: high
- Found: 2026-08-14, parity bridge audit
- Evidence: the descriptor's single `buildId` was compared with the run's
  installed build while the supervisor exported the controller build ID.
- Impact: a valid staged run could be reported stale, or a stale process could
  pass a weaker connected reply check.
- Disposition: fixed in bridge protocol v2. Descriptors and handshakes now bind
  controller build, run build, manifest digest, child PID, child start
  identity, restart epoch, session, run, and capability. Installed-run
  acceptance remains open.

### WEB-012: Browser input had no idempotent delivery contract

- Severity: high
- Found: 2026-08-14, parity bridge audit
- Evidence: POST mutations generated only server-side request IDs and the Pi
  extension acknowledged `sendUserMessage` before its input gate ran.
- Impact: retries could duplicate prompts, idle prompts could silently change
  delivery mode, and an unclean run loss could not distinguish rejection from
  uncertain delivery.
- Disposition: fixed in the current bridge slice with client idempotency keys,
  durable `pi-web-input` session markers, pinned `AgentSession.prompt()`
  preflight after validation/queue insertion, browser-owned queue IDs, explicit
  `CP_INPUT_CONFLICT`/`CP_INPUT_REJECTED`/`CP_DELIVERY_UNCERTAIN` responses, and
  no replay across a replaced run. Installed-run and multi-client acceptance
  remain open.

### WEB-013: Timeline and SSE reconnects had no stable cursor

- Severity: high
- Found: 2026-08-14, parity bridge audit
- Evidence: projected timeline entries had no session entry IDs, SSE frames had
  no event IDs, and reconnect subscriptions had no replay cursor.
- Impact: events could be lost across the bounded stream window and the browser
  could not reconcile live events to durable session history.
- Disposition: fixed in the current bridge slice with `entryId`/`after` timeline
  pages, run-scoped event IDs, bounded bridge replay, SSE `id` fields, and
  `Last-Event-ID`/query forwarding. Long-gap replay beyond the bounded event
  buffer still falls back to durable timeline refetch.

### WEB-015: Oversized visible session text could break timeline reads

- Severity: medium
- Found: 2026-08-14, final parity review
- Evidence: the durable projector used the controller validator directly, so a
  visible message over 16 KiB raised instead of producing a bounded browser
  projection.
- Impact: one long user or assistant entry could turn an otherwise readable
  conversation into a failed timeline request.
- Disposition: closed by truncating visible text at the projection boundary and
  skipping oversized JSONL records without losing later readable records,
  retaining NUL removal, per-record/per-file limits, and total page bounds.

### WEB-005: Prototype could leave a live browser-terminal process

- Severity: medium
- Found: 2026-08-14, pre-implementation cleanup
- Evidence: the prototype used a standalone server and port `8787`.
- Disposition: prototype files, processes, and documentation references were
  removed; port `8787` was verified closed.

### WEB-006: Bootstrap could grow with every session timeline

- Severity: medium
- Found: 2026-08-14, Slice 2 payload review
- Evidence: the first projection draft embedded bounded timelines for every
  conversation in the bootstrap response. Per-session limits did not provide
  a bounded total response for a multi-project controller.
- Disposition: closed by making bootstrap metadata-only and loading one exact
  conversation timeline on demand through the bounded allowlist endpoint.

### WEB-007: Review found live-mode and bridge hardening defects

- Severity: high to low, grouped implementation review
- Found: 2026-08-14, Slice 3 review
- Evidence: the live Home empty state had an undeclared JavaScript assignment;
  bridge bind errors were unhandled; large sessions read the oldest records;
  change authors could render as `undefined`; prompt byte limits differed;
  aggregate bootstrap size and same-origin checks were incomplete.
- Disposition: closed in the current implementation. The Home path is covered
  by a live headless Chrome smoke check; bridge frames and lifecycle are covered
  by `pi/extensions/web-session/test/bridge-test.cjs`; payload, origin, socket,
  timeline-tail, and stale-bridge checks are enforced in the gateway/runtime.

### WEB-008: Launcher could report start before gateway readiness

- Severity: low
- Found: 2026-08-14, installed launcher smoke test
- Evidence: `pi-web start` returned immediately after spawning the gateway,
  before checking whether the child had exited on an invalid controller state.
- Disposition: closed by checking child liveness after startup and printing the
  bounded gateway log on failure.

### WEB-009: Final review found stale timeline, symlinked launcher, and stream errors

- Severity: medium to low
- Found: 2026-08-14, final Slice 5 review
- Evidence: timelines under 2 MB still returned the oldest 512 records;
  symlinked installed launchers resolved their source root incorrectly; and a
  dead bridge could escape the SSE error mapping.
- Disposition: closed by retaining the newest bounded records, resolving
  `bin/pi-web` through `readlink -f`, and mapping bridge protocol failures to a
  bounded service-unavailable response. Regression coverage now includes the
  newest-record window.

### WEB-010: SSE reconnect was conflated with stale-bridge failure

- Severity: medium
- Found: 2026-08-14, final live-stream review
- Evidence: the gateway intentionally closes an SSE connection after a
  bounded 25-second window, but the client closed `EventSource` on every error
  and never reopened it.
- Disposition: closed by sending explicit `stream_end` and `bridge_stale`
  events. Normal windows reconnect; stale controller identity becomes an
  actionable restart message.
