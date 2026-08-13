# pi-sandbox-control provenance

This first-party package began as the Phase 5 adapter extraction and is now the
small Pi manifest/channel broker. Installed `node_modules` is never
edited in place.

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
- Current broker-only `src/index.ts` SHA-256:
  `f14255ceae1b262aa7ac7b0aebfe9f2e6855686c496349f77452fe209f851205`
- First-party `src/manifest-adapter.ts` SHA-256:
  `42085ec0159e0e2733527924b84777433170f5e43606a1f2305bade2aa36f04c`

The extracted source is the repository's reviewed patched `pi/npm/node_modules`
copy at the Phase 5B gate. Its patch history is preserved in the repository
under `pi/patches/`, including the runtime-contract, task-routing,
user-workspace, child-lifecycle, and fast-mode review patches. The extraction
freezes the resulting source hash so subsequent maintenance changes are normal
first-party source changes rather than an accumulating patch chain.

## P5 boundary

The active source contains only canonical manifest validation and four
model-visible tools forwarded over the inherited controller channel. It has no
Docker, host shell, Git, workspace, route, package installation, network,
checkpoint, ref, or lifecycle implementation. Docker lifecycle and tool exec
belong exclusively to the host controller. Packaging or testing this source
does not activate it, alter live Pi paths, deploy, or authorize a live canary.
