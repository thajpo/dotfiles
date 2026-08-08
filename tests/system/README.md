# C0b static action catalog

This directory contains the **machine-readable target mapping** for the
configured Pi harness. It is contract/test infrastructure, not system-tier
acceptance evidence. The runners and scenarios described by
`pi/control-plane/SYSTEM_INTEGRATION_TEST_PLAN.md` are not created by C0b.
Missing future runners remain STOP/77 and no action in this catalog means that a
scenario has passed.

## Validation

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.system.test_action_manifest tests.system.test_slice_briefs
python3 tests/system/validate_plan_docs.py
git diff --check -- tests/system
```

`validate_plan_docs.py` is read-only. It uses only the Python standard library,
JSON, Python AST parsing, regular-expression source scans, and bounded file
reads. It never imports or executes product code, launchers, extensions,
packages, Git, Docker, tmux, Herdr, or a provider.

## Catalog files

- `action-manifest.v1.json` has stable HA IDs from System Plan §7. Statuses are
  explicitly `supported`, `compatibility`, `planned`, `host-only`, or
  `out-of-scope`. Planned rows name their future C sub-slice; out-of-scope rows
  name refusal scenarios.
- `launcher-surface.v1.json` records actions and flags discovered from launcher
  source, including public and internal-only forms.
- `loaded-extensions.v1.json` is the provenance allowlist for dynamic installed
  extension paths and registrations. Each dynamic resource has a source,
  owning launcher/profile, and stable provenance. An unlisted registration is an
  error; a missing package remains explicitly unavailable rather than green.
- `configured-packages.v1.json` mirrors all 13 `pi/settings.json` packages,
  installed metadata when available, planned first-party staged replacements,
  loaded resources, representative action/scenario, and remote-capability class.
- `action-manifest.schema.json` is the machine-readable shape; the validator
  adds cross-file discovery and status rules that JSON Schema alone cannot
  express.

The manifest validates discovery in both directions: every statically found
CLI argument/subcommand, launcher action/flag, literal `-e`/`--tools` resource,
loaded registration, package resource, and documented host operation must have
an owning HA entrypoint, and every current HA entrypoint must be discoverable.
Only `planned:` and `refusal:` entrypoints are intentionally non-current.
