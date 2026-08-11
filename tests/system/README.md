# Greenfield System Tests

This directory contains the machine-readable action catalog and the disposable
installed-system test infrastructure for the fresh Pi product.

## Evidence Tiers

1. **Contract:** validate active documents, manifests, and required source
   surfaces.
2. **Source/process:** exercise controller behavior and deterministic process
   fixtures from the repository.
3. **Staged installed:** run the exact staged artifact from disposable HOME,
   state, data, cache, runtime, and Git roots.
4. **Docker:** exercise the controller-created coding runtime and its manifest,
   mounts, tools, stop, and recovery behavior.
5. **Rollback:** prove the installed command generation can be restored without
   losing new state or work.

No tier inherits acceptance from a lower tier. Help output, syntax checks,
direct imports, fabricated Docker observations, idle panes, skipped actions, or
planned actions are not release evidence. Every envelope must bind cataloged
action, scenario, and tier values and explicitly record installed-product,
production-mutation, and remote-provider observations.

Program phase status is recorded only in `PI_GREENFIELD_IMPLEMENTATION_PLAN.md`.
P0 remains source-only. P1 source tests build the production generation with a
disposable local Pi-core package input; the installed runner requires the exact
pinned Pi core from the offline npm cache and reports STOP/77 when it is absent.

## Primary Commands

```bash
python3 tests/system/validate_plan_docs.py
python3 -m unittest tests.system.test_action_manifest
bash tests/system/run-source-gate.sh
bash tests/system/run-contract.sh
bash tests/system/run-staged-installed.sh
bash tests/system/run-docker.sh
bash tests/system/run-p6-installed.sh
bash tests/system/run-rollback.sh
```

Capability-dependent runners must return STOP/77 when their required runtime is
unavailable. They must never convert missing evidence into a pass.

The staged installed runner builds the production generation twice, requires
equal build IDs, and executes durable secretary resume plus real investigator
and exact-revision reviewer Pi processes from each generation. The reviewer
continues to inspect its assigned revision after the fixture branch moves. The
runner prints the retained evidence directory. Set
`PI_SYSTEM_EVIDENCE_DIR` to choose an existing external destination; evidence
paths inside this repository are rejected.

The Docker runner uses the final staged `bin/pi-system-container-run` and the
already-local pinned Python image. It does not build or pull an image and does
not contain a direct container-create fixture. It records the real broker tool
calls, host Pi/container PID split, second-writer refusal, working-copy delta,
isolation checks, and exact managed-label/name cleanup in an external evidence
file. Missing Docker or the exact local image is STOP/77.

## Catalog Files

- `action-manifest.v1.json` distinguishes source implementation from planned or
  out-of-scope actions and binds each action to evidence tiers.
- `launcher-surface.v1.json` separates release canaries, planned launchers, and
  excluded launcher paths.
- `loaded-extensions.v1.json` records packaged extension role mappings and
  whether installed-process evidence exists.
- `configured-packages.v1.json` records the exact package sources and versions
  packaged by the production builder.
- `pi/greenfield-resources.v1.json` is the production role/resource catalog;
  the builder emits its installed relative-path inventory as
  `release-resources.json` before calculating the build ID.
- `evidence.schema.json` defines the common evidence envelope. The release
  verifier rejects planned actions and PASS evidence without an observed
  installed product action.

The catalog describes required coverage. An action is accepted only when its
runner invokes the exact staged or installed product path and records the
observable result.
