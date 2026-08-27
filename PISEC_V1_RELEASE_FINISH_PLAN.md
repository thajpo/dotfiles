# Pisec v1 Release Finish Plan

Status: user-approved final release addendum and new-chat handoff

Prepared: 2026-08-26

Repository: `/home/j/dotfiles`

## 1. Authority and references

Use this file as the short execution entry point for finishing Pisec v1.

1. `PISEC_V1_FINALIZATION_PLAN.md` remains authoritative for architecture,
   safety boundaries, state vocabulary, scenario semantics, and Definition of
   Done.
2. `PISEC_V1_RELEASE_COMPLETION_PLAN.md` remains useful for the Phase 10 defect
   history, source-gate commands, and release mechanics.
3. `PISEC_V1_IMPLEMENTATION_STATUS.md` remains the compact durable journal and
   must contain only phase status, commit OID, checks/results, and current
   blocker.
4. This addendum supersedes both older plans only where they conflict with the
   user decisions below: Codex version, permanent project inventory, fleet
   membership, final Herdr topology, and the ordered last-mile execution.

Do not restart Phases 0–9. Do not redesign Pisec. Use MAJOR program control,
BUILD discipline inside each stage, and learning overlay OFF.

## 2. Final user decisions

### 2.1 Codex release version

- “Latest Codex” means the exact currently installed release snapshot,
  `codex-cli 0.150.0`.
- Pin `0.150.0` everywhere the v1 contract requires an exact Codex version:
  adapter validation, runtime-surface identity, installer/doctor checks,
  manifests, examples, tests, and active documentation.
- Do not use a floating `latest` check and do not chase another Codex release
  during this release run. A later upgrade is a separate bounded compatibility
  change after the v1 tag.

### 2.2 Permanent project inventory

The final fresh v1 database contains exactly these nine project registrations.

Non-fleet (`coordination_mode = project`):

| Display identity | Canonical repository |
|---|---|
| VLA Lens | `/home/j/Projects/vla-lens` |
| vla-infra | `/home/j/Projects/vla-infra` |
| Dreamer³ / SleepyDreamy | `/home/j/Projects/SleepyDreamyV3` |
| CSV Agent | `/home/j/Projects/csv-agent` |
| ScaleTraining | `/home/j/Projects/ScaleTraining` |

Fleet (`coordination_mode = fleet`):

| Display identity | Canonical repository |
|---|---|
| dotfiles | `/home/j/dotfiles` |
| mlre-transition | `/home/j/Projects/mlre-transition` |
| investing | `/home/j/Projects/investing` |
| jpo.github.io | `/home/j/Projects/jpo.github.io` |

`dotfiles` is the canonical First Mate control project.

The following current registrations are not permanent and must be absent from
the final database:

- `/home/j/Projects/OpenVLA_Patching_Experiment`
- `/home/j/Projects/Tokenthing`
- `/home/j/Projects/lerobot`
- `/tmp/pisec-live-acceptance-epoch3`
- `/home/j/Projects/rust-interpreter`
- `/home/j/Projects/so-arm`

Discard only their Pisec registrations, Pisec-owned runtimes, worker/session
state, and Pisec-created Herdr surfaces through the reviewed archive/reset and
exact cleanup inventory. Preserve every real source repository and its Git
data. Archive the disposable `/tmp` acceptance repository before removing it.

### 2.3 Final Herdr topology

Herdr session `main` must end with exactly six top-level Pisec workspaces:

1. one workspace for each of the five non-fleet projects; and
2. one `Pisec First Mate` workspace containing the four fleet project
   Secretary tabs.

Workers are temporary tabs under their owning project workspace and must not
leave a top-level workspace after retirement/cleanup. No stale Pisec-created
workspace from an earlier cutover may remain. Unrelated Herdr workspaces and
histories remain untouched.

## 3. Current observed handoff

At preparation time:

- Git `HEAD` is `e3ea3cc14f03a2bb5c0efe3ff2fe9c4fd20d0bc1` on `master`.
- Phases 0–9 are complete; Phase 10 is incomplete.
- Installed Codex is `codex-cli 0.150.0`; active source/config still pins
  `0.147.0`.
- The deployed final candidate is still `f2135c0`; its compatible LKG is
  `fac003d`.
- Doctor is false only for Codex worker
  `ws_12ca9ca265e17f359ce7d8f5549f4131`, which is
  `starting/needs_attention` with a stale generation.
- The original Codex launch failed before Codex execution because the launcher
  rejected the normal root-owned `/usr/bin/node`.
- The uncommitted narrow correction is in `pisec/runtime-bin/codex` and
  `tests/test_pisec_phase10_scope_parity.py`. A Luna read-only audit found the
  ownership change plan-consistent and scoped to Pisec, but requested stronger
  coverage for all three external executable paths and unsafe modes.
- Herdr `main` currently has 102 top-level workspaces. Current database
  bindings reference 16 top-level IDs; the rest are repeated stale Pisec
  surfaces. The four current fleet projects are also incorrectly left as
  separate top-level workspaces.
- `change_project_mode` changes the database mode without moving the Secretary
  surface and durable project workspace into or out of the First Mate
  workspace. This is a source defect, not merely cleanup residue.
- Current unrelated dirty Neovim work must be preserved and excluded from all
  Pisec commits. Reinspect `git status` before acting because the exact dirty
  paths may change.
- No local `pisec-v1.0.0` tag or final external acceptance record exists.

Treat these facts as a resume lead and revalidate them read-only. Do not infer
that the current status journal or old candidate identities are final.

## 4. Anti-loop execution rules

1. Freeze the decisions in Section 2 before writing code.
2. Treat all known source defects as one correction cluster. Do not create a
   new “final candidate” after each narrow edit.
3. During implementation, run fail-first and focused adjacent tests only.
4. Run the complete clean-source gate only after the correction cluster is
   coherent and independently audited, then once more at the required final
   documentation/status descendant. These are the two planned full gates.
5. Do not create repeated status-only candidate commits. Update the compact
   journal after the corrected core commit and after the final descendant.
6. Use an isolated clean checkout/worktree for clean gates so unrelated dirty
   user work in the primary checkout is neither committed nor disturbed.
7. Run all 51 authoritative scenarios, but map automated coverage to scenario
   numbers and group the genuinely live proofs. Do not manually replay the
   complete matrix after every source edit.
8. Perform one final archive/reset after live acceptance on the corrected
   source, then replay only the nine-project final inventory.
9. Never edit live database rows manually, delete by broad path/age, or wipe
   unrelated Herdr, OMP, Codex, project, or Git state.
10. Never push commits or tags.

## 5. Ordered remaining execution

### Stage A — Reconcile and establish fail-first evidence

1. Reread this file, the referenced Phase 10 and Definition of Done sections,
   the compact status journal, recent Git history, current status/diff, updater
   status, sanitized doctor output, and Herdr workspace inventory.
2. Preserve the current Pisec launcher/test draft and all unrelated dirty work.
3. Record fail-first evidence for:
   - exact Codex `0.150.0` rejection under the old pin;
   - root-owned regular `0755` external Node/Codex/Fence executables;
   - rejection of symlinked, non-regular, group/world-writable, or
     non-executable external launch inputs;
   - public project-mode to fleet-mode transition failing to move the
     Secretary surface, project workspace identity, and binding into First
     Mate;
   - fleet-mode to project-mode transition failing to create/move back to one
     project-owned workspace;
   - stale Pisec Herdr workspace inventory and exact unrelated-workspace
     preservation; and
   - exact final nine-project/six-workspace topology.
4. Use production adapters and public dispatch/socket paths where identity,
   movement, or runtime attestation is under test. Do not rely on fixture-only
   state mutation.

### Stage B — Implement the one correction cluster

1. Replace the exact Codex `0.147.0` pin with `0.150.0` on every active v1
   surface and nowhere unrelated.
2. Finish the launcher correction so UID 0 is permitted only for explicitly
   marked external host executables. Keep generated launchers, descriptors,
   tokens, policies, binding state, and immutable surfaces strictly
   user-owned. Preserve symlink/type/mode/execute checks.
3. Make the guarded public project-mode transition move the existing Secretary
   surface and durable identities into the First Mate workspace before
   reporting fleet success. Make the reverse transition create or select one
   project-owned workspace and move the Secretary surface back before
   reporting project success.
4. Keep one Secretary per active project. Sharing the First Mate top-level
   workspace changes presentation/placement, not supervisory authority.
5. Implement only the bounded, manifest-driven cleanup needed to archive and
   close identified Pisec-owned Herdr workspaces while preserving unrelated
   Herdr state. Do not add a generic session-management framework.
6. Update focused production-path tests and exact inventory/topology
   assertions. Preserve all prior crash/retry, attestation, permission,
   attention, Git, recovery, and safety regressions.

### Stage C — Audit, commit, and freeze source

1. Run focused Codex, workspace/mode, Herdr, project, runtime, updater, and
   adjacent regression suites.
2. Run one independent Luna max-reasoning read-only audit against this file and
   the authoritative v1 contract. Resolve every concrete finding.
3. Commit only the coherent source/test correction as the new
   `bootstrapV1Commit` candidate. Exclude unrelated dirty files.
4. From an exact clean checkout of that commit, run its required source gate.
5. Update only final documentation truth, this handoff if factual OIDs must be
   recorded, and the compact status journal. Commit one descendant as the new
   `finalV1Commit` candidate.
6. From an exact clean checkout of that descendant, run the complete Phase
   10.1 gate:

```text
git status --short
git diff --check
python3 -m compileall -q scripts tests
bash -n scripts/*.sh
python3 scripts/generate-pisec-operation-catalogue.py --check
bun build omp/extensions/pisec.ts --target bun --outdir <mktemp-directory>
python3 -m unittest discover -s tests
bun test omp/extensions/pisec.test.ts
codex --version
```

Any tracked source change after this gate creates one replacement candidate
and requires the gate again. Operational evidence files outside the repository
do not change the candidate.

### Stage D — Corrected live acceptance before final reset

1. Install/deploy the frozen candidate through the stable updater without
   manually repairing the current database.
2. Refresh/reconcile the current state only enough to prove the corrected
   Codex route and workspace transition behavior. Do not preserve obsolete
   registrations by adding compatibility code.
3. Complete the real Codex project-mode worker lifecycle using the disposable
   acceptance project already present: launch, authenticated attestation,
   local commit, ready-review, human acceptance, `ff-only` integration,
   retirement, and cleanup.
4. Complete the required real fleet remediation, attention restart, Reviewr,
   Collie, permission, auth/Fence, updater/recovery, and interrupted-operation
   proofs on the same frozen source.
5. Map every authoritative Phase 10.2 scenario to exact automated or live
   evidence. Record each scenario once; do not rerun already-green unaffected
   scenarios merely for narration.
6. Quiesce live state and inventory every Pisec-created Herdr workspace,
   runtime, worker repository, session, operation, and database file. Give each
   exact archive/retain/close disposition. Preserve unrelated state.

### Stage E — One final archive/reset and exact cutover

1. Through the verified stable updater, archive/reset the current Pisec and
   Pisec-worker state. Preserve all earlier archives and the pre-v1 archive.
2. Archive and close every inventoried Pisec-created Herdr workspace, including
   stale duplicates and the disposable acceptance project. Do not close an
   unclassified workspace.
3. Deploy and verify the new `bootstrapV1Commit` on the fresh v1 database.
4. Register exactly the nine canonical repositories from Section 2.2, all
   initially in project mode. Stop on the first Secretary attestation failure.
5. Ensure the one First Mate from active `dotfiles`.
6. Transition exactly `dotfiles`, `mlre-transition`, `investing`, and
   `jpo.github.io` to fleet mode. Each transition must move its Secretary tab
   into the First Mate workspace and preserve exact binding attestation.
7. Apply the reviewed exact permissions through normal protected operations.
8. Deploy `finalV1Commit`, prove `bootstrapV1Commit` becomes compatible LKG,
   run `--recover-previous`, prove bootstrap health, then update back to final.
9. Restart components in dependency order and rerun doctor/reconcile and
   installed-artifact-dependent checks.

### Stage F — Final proof and tag

Require all of the following simultaneously:

- `codex --version` reports exact `codex-cli 0.150.0`;
- the database contains exactly nine projects, all active, with five project
  and four fleet modes matching Section 2.2;
- there are exactly nine active Secretary workstreams and one active First
  Mate, with no active worker left from acceptance;
- Herdr `main` has exactly six top-level Pisec workspaces: five non-fleet and
  one First Mate containing the four fleet Secretary tabs;
- no stale Pisec-created Herdr workspace remains and unrelated Herdr state is
  unchanged;
- every active binding is usable, current, attested, and unreserved;
- `pisec doctor --json` returns `ok: true` with no unexplained
  `needs_attention`;
- all 51 authoritative scenarios and added whole-path integrations have bound
  evidence;
- deployed `current` is exact `finalV1Commit` and LKG is the recovery-proven
  `bootstrapV1Commit`;
- the owner-only external acceptance record is canonical, digest-validated,
  and binds source/tree/bundle/schema/archive/current/LKG/pins/scenarios; and
- local annotated tag `pisec-v1.0.0` points to exact `finalV1Commit`.

Never push the commit or tag. Preserve the pre-v1 archive.

## 6. Stop conditions

Continue automatically through ordinary failures, candidate replacement,
focused repair, archive/reset, and exact replay. Stop only if:

- existing dirty work cannot be preserved without discarding or overwriting
  it;
- two authoritative invariants genuinely contradict or a tested invariant is
  impossible;
- completion requires broader authority or product scope;
- live inventory contains unclassified state that would be deleted or changed;
  or
- an action would push, publish, alter a remote system, or delete unarchived
  user data.

No further user decision is needed for Codex `0.150.0`, the nine-project
inventory, four fleet projects, `dotfiles` control ownership, or the six-space
Herdr topology.

## 7. New-chat `/goal` command

```text
/goal Finish and verify Pisec v1 using
/home/j/dotfiles/PISEC_V1_RELEASE_FINISH_PLAN.md as the immediate execution
contract. Treat /home/j/dotfiles/PISEC_V1_FINALIZATION_PLAN.md as authoritative
for architecture, safety, scenario semantics, and Definition of Done, with the
explicit Codex 0.150.0, nine-project inventory, four-fleet-project, and
six-top-level-Herdr-workspace decisions in the finish plan superseding stale
facts in older plans. Continue from the current Phase 10 handoff without
restarting Phases 0–9. Preserve and exclude unrelated dirty work. Use MAJOR
program control, BUILD discipline within stages, and learning overlay OFF.
Establish fail-first evidence, implement the one known correction cluster,
audit it independently, create only the required corrected-core and final
descendant commits, run the clean gates, complete corrected live acceptance,
perform one final reviewed archive/reset, replay exactly the nine permanent
projects, prove the exact six-workspace Herdr topology, write external evidence,
and create the local pisec-v1.0.0 tag. Do not push. Continue automatically
unless a stop condition in the finish plan is met.
```
