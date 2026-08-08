# pi-sandbox-control provenance

This first-party package is the Phase 5B maintained target for the Pi runtime
adapter. It was extracted without editing the installed `node_modules` copy.

## Source

- Upstream package: `@kjrjay/pi-sandbox`
- Upstream version: `0.2.0`
- Upstream package metadata SHA-256:
  `cbf043c8ee7c3d434c5a1b8898871a7a87b75ada3d226db726b384ba5822a199`
- Upstream license SHA-256:
  `75d2ba3e6418c86c191fcb0f26498b7a4a6471be062b09563e1a372c719c3cfe`
- Unmodified upstream source (`index.ts.orig`) SHA-256:
  `826b43df0ef974858d6c3dd2738fe94712947c6821bc38cdbd1993bfcdac7e27`
- Reviewed patched source extracted before first-party adapter integration SHA-256:
  `2be39ad741d284e3a669efad35423beebe5507692ca70d29c40c17c7e1193fa3`
- Current first-party `src/index.ts` SHA-256 after the manifest adapter and lifecycle gate integration:
  `b2498e87a2971f085aff8774b7f4063108a85dfdbe94e6d10426d4ef249e9223`
- First-party `src/manifest-adapter.ts` SHA-256:
  `33f823d875d47f7041b62413eab2e14c0548b45222968d38134eeca9b1490d4c`

The extracted source is the repository's reviewed patched `pi/npm/node_modules`
copy at the Phase 5B gate. Its patch history is preserved in the repository
under `pi/patches/`, including the runtime-contract, task-routing,
user-workspace, child-lifecycle, and fast-mode review patches. The extraction
freezes the resulting source hash so subsequent maintenance changes are normal
first-party source changes rather than an accumulating patch chain.

## Phase 5B boundary

This package contains the selected adapter source, license, and provenance
only. It does not activate Docker, change installed host `node_modules`, mutate
Pi sessions, perform deployment, or authorize a live canary. Disposable runtime
proof belongs to Phase 5C; staged artifact and rollback proof belong to Phase
5D.
