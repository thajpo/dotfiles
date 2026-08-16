# pi-subagents-control provenance

This package began as the reviewed `pi-subagents@0.35.1` extraction. P7
replaces its production entrypoint with a narrow controller-channel broker.
The retained upstream source is historical input only and is excluded by the
package `files` allowlist.

## Upstream identity

- Package: `pi-subagents@0.35.1`
- Author: Nico Bailon
- License metadata: MIT
- Registry lock integrity:
  `sha512-nIH6liO541FZ1RoeEu58Ligd59tiNw0/ODPgHh7uvx9Dk4UpWH08F84/l1+hXCzUgC85OCmyVtngWkZjcK94Cg==`
- Upstream package metadata SHA-256:
  `973af20d8872aac1006f470f6bfec75529319d672a681567522fba2d7aeb65cf`
- Extracted npm-pack file count: `147`
- Extracted source tree manifest SHA-256:
  `sha256:f4503fccf1e25be7453963caaec6b3e2552bc330a523e1828ddc82a6572cdc5b`
- Patch driver SHA-256 at extraction:
  `b3177a934912fd50523f1a65335e7e9054bbc5d13025479d029f69d2a831b3ca`

The extracted tree includes the upstream package metadata, entrypoint,
install helper, agents, prompts, skills, and every npm-pack-visible `src/*.ts`
file. The source bytes are unchanged. The first-party package adds only this
provenance document and the MIT license text because the upstream npm package
exposes MIT only through package metadata.

## Package boundary

The package keeps the original npm name and extension entrypoint. Its packed
artifact contains only `index.ts`, `src/controller-broker.ts`, metadata, and
license/provenance documents. It has no child-process, worktree, persistence,
credential-discovery, intercom, or inherited-environment implementation. The
broker can request only semantic read-only roles over the authenticated
controller channel; the host controller creates every child resource.
