from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.migration import InventoryV2Report
from scripts.pi_control.migration_importer import shadow_import_v2
from scripts.pi_control.migration_planner import create_resolution_manifest


class MigrationImportV2Tests(unittest.TestCase):
    def _report_and_resolution(self):
        payload = {"schemaVersion": 2, "createdAt": None, "host": {"platform": "test", "visibility": "bounded"}, "sources": [], "records": [{"record_id": "rec_" + "1" * 32, "adapter_kind": "git", "source_kind": "git-repository", "source_locator": "/repo", "source_digest": "sha256:" + "2" * 64, "resource_kind": "project-observation", "identity": {"commonDir": "/repo/.git"}, "normalized": {"common_dir": "/repo/.git", "top_level": "/repo", "object_format": "sha1"}, "observation_state": "observed"}], "relationships": [], "contradictions": [], "adapterStates": []}
        import hashlib, json
        digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report = InventoryV2Report(payload, digest, "inv_" + "3" * 32)
        resolution = create_resolution_manifest(report, decisions=[{"recordId": "rec_" + "1" * 32, "disposition": "import", "resourceType": "project", "resourceId": None, "reason": "exact", "expectedDigest": "sha256:" + "2" * 64}])
        return report, resolution

    def test_disposable_import_is_idempotent_and_has_no_active_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, resolution = self._report_and_resolution()
            root = Path(temporary) / "shadow"
            first = shadow_import_v2(report, resolution, root, idempotency_key="import-1")
            second = shadow_import_v2(report, resolution, root, idempotency_key="import-1")
            self.assertEqual(first["state"], "succeeded")
            self.assertTrue(second["idempotent"])
            from scripts.pi_control.store import ControllerStore
            with ControllerStore(root, read_only=True) as store:
                self.assertEqual(store.conn.execute("SELECT count(*) FROM runs WHERE authority='writer'").fetchone()[0], 0)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM working_copies WHERE active_writer_run_id IS NOT NULL").fetchone()[0], 0)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_import_materializes_worktrees_and_exact_conversations_without_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            import hashlib, json
            project_id = "rec_" + "1" * 32
            conversation_id = "rec_" + "2" * 32
            binding_id = "rec_" + "3" * 32
            project_digest = "sha256:" + "1" * 64
            conversation_digest = "sha256:" + "2" * 64
            binding_digest = "sha256:" + "3" * 64
            payload = {
                "schemaVersion": 2, "createdAt": None, "host": {}, "sources": [],
                "records": [
                    {"record_id": project_id, "adapter_kind": "git", "source_kind": "git-repository", "source_locator": "/repo", "source_digest": project_digest, "resource_kind": "project-observation", "identity": {"commonDir": "/repo/.git"}, "normalized": {"common_dir": "/repo/.git", "top_level": "/repo", "object_format": "sha1", "worktrees": [{"path": "/repo", "git_dir": "/repo/.git", "branch_ref": "refs/heads/main", "head_oid": "a" * 40, "tree_oid": "b" * 40, "state": "ready", "exists": True}]}, "observation_state": "observed"},
                    {"record_id": conversation_id, "adapter_kind": "root_sessions", "source_kind": "session", "source_locator": "/legacy/session.jsonl", "source_digest": conversation_digest, "resource_kind": "conversation-observation", "identity": {"sessionId": "ws-session"}, "normalized": {"header": {"cwd": "/repo/legacy-worktree", "id": "ws-session", "type": "session", "version": 3}}, "observation_state": "observed"},
                    {"record_id": binding_id, "adapter_kind": "root_sessions", "source_kind": "root-registry", "source_locator": "/legacy/root-registry.json", "source_digest": binding_digest, "resource_kind": "conversation-binding-observation", "identity": {"path": "/legacy/root-registry.json"}, "normalized": {"records": [{"conversationId": "ws-session", "profile": "root", "repository": "/repo", "worktree": "/repo/legacy-worktree", "status": "active"}]}, "observation_state": "observed"},
                ],
                "relationships": [], "contradictions": [], "adapterStates": [],
            }
            digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            report = InventoryV2Report(payload, digest, "inv_" + "4" * 32)
            resolution = create_resolution_manifest(report, decisions=[
                {"recordId": project_id, "disposition": "import", "resourceType": "project", "resourceId": None, "reason": "exact Git authority", "expectedDigest": project_digest},
                {"recordId": conversation_id, "disposition": "import", "resourceType": "conversation", "resourceId": None, "reason": "exact root registry binding", "expectedDigest": conversation_digest},
                {"recordId": binding_id, "disposition": "observe", "resourceType": "conversation-binding", "resourceId": None, "reason": "binding authority is preserved as observation", "expectedDigest": binding_digest},
            ])
            result = shadow_import_v2(report, resolution, Path(temporary) / "shadow", idempotency_key="lifecycle-import")
            self.assertEqual(result["importedProjects"], 1)
            self.assertEqual(result["importedWorkingCopies"], 2)
            self.assertEqual(result["importedConversations"], 1)
            from scripts.pi_control.store import ControllerStore
            with ControllerStore(Path(temporary) / "shadow", read_only=True) as store:
                wc = store.conn.execute("SELECT * FROM working_copies WHERE path=?", ("/repo/legacy-worktree",)).fetchone()
                self.assertIsNotNone(wc)
                self.assertEqual(wc["controller_owned"], 0)
                conversation = store.conn.execute("SELECT * FROM conversations WHERE pi_session_id=?", ("ws-session",)).fetchone()
                self.assertEqual(conversation["role"], "workstream")
                self.assertEqual(conversation["working_copy_id"], wc["working_copy_id"])
                self.assertEqual(store.conn.execute("SELECT count(*) FROM runs WHERE authority='writer'").fetchone()[0], 0)

    def test_failed_attempt_resumes_and_completes_at_both_boundaries(self):
        for boundary in ("shadow.manifest.before", "shadow.mappings.after"):
            with tempfile.TemporaryDirectory() as temporary:
                report, resolution = self._report_and_resolution()
                class Failpoint:
                    def __init__(self): self.count = 0
                    def hit(self, name, detail):
                        if name == boundary and self.count == 0:
                            self.count += 1
                            raise RuntimeError("injected")
                root = Path(temporary) / "shadow"
                with self.assertRaises(RuntimeError):
                    shadow_import_v2(report, resolution, root, idempotency_key=f"resume-{boundary}", failpoint=Failpoint())
                resumed = shadow_import_v2(report, resolution, root, idempotency_key=f"resume-{boundary}")
                self.assertEqual(resumed["state"], "succeeded")
                self.assertNotIn("idempotent", resumed)
                self.assertEqual(resumed["importedProjects"], 1)
                replay = shadow_import_v2(report, resolution, root, idempotency_key=f"resume-{boundary}")
                self.assertTrue(replay["idempotent"])
                self.assertEqual(replay["importedProjects"], 1)
                from scripts.pi_control.store import ControllerStore
                with ControllerStore(root, read_only=True) as store:
                    row = store.conn.execute("SELECT state,step FROM migration_runs").fetchone()
                    self.assertEqual((row["state"], row["step"]), ("succeeded", "complete"))
                    self.assertEqual(store.conn.execute("SELECT count(*) FROM projects").fetchone()[0], 1)

    def test_role_classification_secretary_personal_workstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            import hashlib, json
            project_id = "rec_" + "1" * 32
            conv_ids = ["rec_" + str(i) * 32 for i in (2, 3, 4)]
            binding_id = "rec_" + "5" * 32
            digests = {project_id: "sha256:" + "1" * 64}
            for record_id in conv_ids:
                digests[record_id] = "sha256:" + str(conv_ids.index(record_id) + 2) * 64
            binding_digest = "sha256:" + "6" * 64
            worktrees = [
                {"path": "/repo", "git_dir": "/repo/.git", "branch_ref": "refs/heads/main", "head_oid": "a" * 40, "tree_oid": "b" * 40, "state": "ready", "exists": True},
                {"path": "/repo/wt-a", "git_dir": "/repo/.git/worktrees/wt-a", "branch_ref": "refs/heads/pi/wt-a", "head_oid": "c" * 40, "tree_oid": "d" * 40, "state": "ready", "exists": True},
                {"path": "/repo/wt-b", "git_dir": "/repo/.git/worktrees/wt-b", "branch_ref": "refs/heads/pi/wt-b", "head_oid": "e" * 40, "tree_oid": "f" * 40, "state": "ready", "exists": True},
            ]
            sessions = [
                {"record_id": conv_ids[0], "session_id": "sec-primary", "cwd": "/repo", "profile": "secretary", "worktree": "/repo"},
                {"record_id": conv_ids[1], "session_id": "personal-wt", "cwd": "/repo/wt-a", "profile": "personal", "worktree": "/repo/wt-a"},
                {"record_id": conv_ids[2], "session_id": "root-wt", "cwd": "/repo/wt-b", "profile": "root", "worktree": "/repo/wt-b"},
            ]
            records = [
                {"record_id": project_id, "adapter_kind": "git", "source_kind": "git-repository", "source_locator": "/repo", "source_digest": digests[project_id], "resource_kind": "project-observation", "identity": {"commonDir": "/repo/.git"}, "normalized": {"common_dir": "/repo/.git", "top_level": "/repo", "object_format": "sha1", "worktrees": worktrees}, "observation_state": "observed"},
            ]
            for item in sessions:
                records.append({"record_id": item["record_id"], "adapter_kind": "root_sessions", "source_kind": "session", "source_locator": f"/legacy/{item['session_id']}.jsonl", "source_digest": digests[item["record_id"]], "resource_kind": "conversation-observation", "identity": {"sessionId": item["session_id"]}, "normalized": {"header": {"cwd": item["cwd"], "id": item["session_id"], "type": "session", "version": 3}}, "observation_state": "observed"})
            records.append({"record_id": binding_id, "adapter_kind": "root_sessions", "source_kind": "root-registry", "source_locator": "/legacy/root-registry.json", "source_digest": binding_digest, "resource_kind": "conversation-binding-observation", "identity": {"path": "/legacy/root-registry.json"}, "normalized": {"records": [{"conversationId": item["session_id"], "profile": item["profile"], "repository": "/repo", "worktree": item["worktree"], "status": "active"} for item in sessions]}, "observation_state": "observed"})
            payload = {"schemaVersion": 2, "createdAt": None, "host": {}, "sources": [], "records": records, "relationships": [], "contradictions": [], "adapterStates": []}
            digest = "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            report = InventoryV2Report(payload, digest, "inv_" + "6" * 32)
            decisions = [{"recordId": project_id, "disposition": "import", "resourceType": "project", "resourceId": None, "reason": "exact Git authority", "expectedDigest": digests[project_id]}]
            for record_id in conv_ids:
                decisions.append({"recordId": record_id, "disposition": "import", "resourceType": "conversation", "resourceId": None, "reason": "exact root registry binding", "expectedDigest": digests[record_id]})
            decisions.append({"recordId": binding_id, "disposition": "observe", "resourceType": "conversation-binding", "resourceId": None, "reason": "binding authority preserved", "expectedDigest": binding_digest})
            resolution = create_resolution_manifest(report, decisions=decisions)
            result = shadow_import_v2(report, resolution, Path(temporary) / "shadow", idempotency_key="roles")
            self.assertEqual(result["importedConversations"], 3)
            self.assertEqual(result["importedWorkingCopies"], 3)  # primary + wt-a + wt-b, all Git-observed
            from scripts.pi_control.store import ControllerStore
            with ControllerStore(Path(temporary) / "shadow", read_only=True) as store:
                by_session = {row["pi_session_id"]: row for row in store.conn.execute("SELECT * FROM conversations")}
                self.assertEqual(by_session["sec-primary"]["role"], "secretary")
                self.assertIsNone(by_session["sec-primary"]["working_copy_id"])
                self.assertEqual(by_session["personal-wt"]["role"], "personal")
                personal_wc = store.conn.execute("SELECT working_copy_id FROM working_copies WHERE path=?", ("/repo/wt-a",)).fetchone()[0]
                self.assertEqual(by_session["personal-wt"]["working_copy_id"], personal_wc)
                self.assertEqual(by_session["root-wt"]["role"], "workstream")
                root_wc = store.conn.execute("SELECT working_copy_id FROM working_copies WHERE path=?", ("/repo/wt-b",)).fetchone()[0]
                self.assertEqual(by_session["root-wt"]["working_copy_id"], root_wc)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM working_copies WHERE controller_owned=1").fetchone()[0], 0)
                self.assertEqual(store.conn.execute("SELECT count(*) FROM runs WHERE authority='writer'").fetchone()[0], 0)

    def test_resume_refuses_tampered_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, resolution = self._report_and_resolution()
            class Failpoint:
                def hit(self, name, detail):
                    if name == "shadow.mappings.after": raise RuntimeError("injected")
            root = Path(temporary) / "shadow"
            with self.assertRaises(RuntimeError):
                shadow_import_v2(report, resolution, root, idempotency_key="tamper", failpoint=Failpoint())
            manifest = root / "source-inventory-v2.json"
            manifest.chmod(0o600)  # an attacker (or bug) must first relax the immutable mode
            manifest.write_bytes(manifest.read_bytes() + b"\ntampered\n")
            from scripts.pi_control.errors import IdempotencyConflictError
            with self.assertRaises(IdempotencyConflictError):
                shadow_import_v2(report, resolution, root, idempotency_key="tamper")

    def test_live_root_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, resolution = self._report_and_resolution()
            with self.assertRaises(ValueError):
                shadow_import_v2(report, resolution, Path.home() / ".local/state/pi-control", idempotency_key="live")


if __name__ == "__main__":
    unittest.main()
