from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import unittest

from scripts.pi_control.artifacts import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactStore,
    TerminalStateError,
    index_and_reconcile_child_terminal,
    reconcile_child_terminal,
    register_artifact,
)
from scripts.pi_control.errors import ConstraintError, IdempotencyConflictError, InvalidRequestError, ResourceStaleError
from scripts.pi_control.snapshot import SnapshotPolicy, capture_snapshot
from scripts.pi_control.store import ControllerStore


class ArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Artifact Test")
        self._git("config", "user.email", "artifact@example.invalid")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "tracked.txt")
        self._git("commit", "-qm", "base")
        self.snapshot = capture_snapshot(self.repo, self.state / "snapshots", snapshot_id="snap_" + "a" * 32)
        os.chmod(self.state, 0o700)
        self.project_id = "prj_" + "1" * 32
        self.parent_wc = "wc_" + "2" * 32
        self.parent_conv = "conv_" + "3" * 32
        self.child_conv = "conv_" + "4" * 32
        self.parent_run = "run_" + "5" * 32
        self.child_run = "run_" + "6" * 32
        self.child_id = "child_" + "7" * 32
        self.plan_source = {
            "snapshotId": self.snapshot.snapshot_id,
            "snapshotRef": self.snapshot.ref_name,
            "snapshotCommitOid": self.snapshot.snapshot_commit_oid,
            "snapshotTreeOid": self.snapshot.snapshot_tree_oid,
            "sourceHeadOid": self.snapshot.source_head_oid,
            "sourceTreeOid": self.snapshot.source_tree_oid,
            "authority": "read-only",
        }
        with ControllerStore(self.state) as store:
            self._seed_store(store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Artifact Test",
            "GIT_AUTHOR_EMAIL": "artifact@example.invalid",
            "GIT_COMMITTER_NAME": "Artifact Test",
            "GIT_COMMITTER_EMAIL": "artifact@example.invalid",
        }
        subprocess.run(["git", *args], cwd=self.repo, env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _seed_store(self, store: ControllerStore) -> None:
        store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
        store.conn.execute(
            "INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.project_id, "p", str(self.repo / ".git"), 1, 1, str(self.repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"),
        )
        store.conn.execute(
            "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.parent_wc, self.project_id, "primary", "primary", "personal", str(self.repo), "trusted-live", "present", "ready", 0, 1, 1, "t", "t"),
        )
        store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.parent_conv, self.project_id, self.parent_wc, "personal", "parent", "parent-session", str(self.root / "parent.jsonl"), "active", "ready", 1, "t", "t"),
        )
        store.conn.execute(
            "INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.child_conv, self.project_id, "personal", "child", "child-session", str(self.root / "child.jsonl"), "active", "ready", 1, "t", "t"),
        )
        store.create_run(run_id=self.parent_run, conversation_id=self.parent_conv, project_id=self.project_id, working_copy_id=self.parent_wc, authority="read-only", runtime_spec_hash="runtime", build_id="build", capability_hash="cap")
        store.create_run(
            run_id=self.child_run, conversation_id=self.child_conv, project_id=self.project_id, authority="read-only",
            parent_run_id=self.parent_run, parent_conversation_id=self.parent_conv, child_source=self.plan_source,
            runtime_spec_hash="runtime", build_id="build", capability_hash="cap",
        )

    def _artifact(self, *, artifact_id: str | None = None, content: bytes = b"hello", retention_class: str = "run"):
        return ArtifactStore(self.state).put(
            run_id=self.child_run,
            project_id=self.project_id,
            producer_child_id=self.child_id,
            source=self.plan_source,
            content=content,
            content_type="text/plain",
            sensitive=True,
            retention_class=retention_class,
            provenance={"sourceDigest": "sha256:" + "a" * 64},
            artifact_id=artifact_id,
        )

    def _changed(self, *, dirty: bool = False, unknown: bool = False) -> dict[str, object]:
        return {
            "headOid": None if unknown else self.snapshot.source_head_oid,
            "treeOid": None if unknown else self.snapshot.source_tree_oid,
            "dirty": dirty,
            "dirtyFingerprint": "b" * 64 if dirty else None,
        }

    def test_manifest_is_exact_secure_and_checksum_verified(self) -> None:
        artifacts = ArtifactStore(self.state)
        record = self._artifact()
        directory = self.state / "artifacts" / record.artifact_id
        self.assertEqual(stat.S_IMODE((self.state / "artifacts").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (directory / "content.bin", directory / "manifest.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(artifacts.verify(record.artifact_id), record)
        manifest = directory / "manifest.json"
        original = manifest.read_text(encoding="utf-8")
        manifest.write_text(original.replace('"manifestDigest":"sha256:', '"manifestDigest":"sha256:0'), encoding="utf-8")
        with self.assertRaises(ArtifactIntegrityError):
            artifacts.verify(record.artifact_id)
        manifest.write_text(original, encoding="utf-8")
        (directory / "content.bin").write_bytes(b"tampered")
        with self.assertRaises(ArtifactIntegrityError):
            artifacts.verify(record.artifact_id)

    def test_artifact_identity_cannot_be_overwritten_or_symlinked(self) -> None:
        artifacts = ArtifactStore(self.state)
        record = self._artifact(artifact_id="art_" + "8" * 32)
        with self.assertRaises(ArtifactConflictError):
            self._artifact(artifact_id=record.artifact_id, content=b"other")
        link_id = "art_" + "9" * 32
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.state / "artifacts" / link_id)
        with self.assertRaises(ArtifactIntegrityError):
            self._artifact(artifact_id=link_id)
        self.assertFalse((outside / "content.bin").exists())
        self.assertEqual(artifacts.verify(record.artifact_id), record)

    def test_index_and_terminal_success_are_atomic_and_replay_idempotent(self) -> None:
        artifacts = ArtifactStore(self.state)
        record = self._artifact()
        with ControllerStore(self.state) as store:
            terminal = index_and_reconcile_child_terminal(
                store, artifacts, artifact=record, artifact_id=record.artifact_id,
                child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id,
                terminal_class="success", changed_state=self._changed(), source=self.plan_source,
                result_summary="complete", expected_resource_version=1,
            )
            self.assertEqual(terminal.observed_state, "stopped")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM artifact_manifests").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM child_terminal_records").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='child.terminal'").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE child_terminal_records SET terminal_class='failed' WHERE child_run_id=?", (self.child_run,))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM child_terminal_records WHERE child_run_id=?", (self.child_run,))
            replay = reconcile_child_terminal(
                store, artifact_id=record.artifact_id, child_run_id=self.child_run, parent_run_id=self.parent_run,
                child_id=self.child_id, terminal_class="success", changed_state=self._changed(), source=self.plan_source,
                result_summary="complete", expected_resource_version=1,
            )
            self.assertEqual(replay.terminal_digest, terminal.terminal_digest)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM control_events WHERE event_kind='child.terminal'").fetchone()[0], 1)
            with self.assertRaises(IdempotencyConflictError):
                reconcile_child_terminal(
                    store, artifact_id=record.artifact_id, child_run_id=self.child_run, parent_run_id=self.parent_run,
                    child_id=self.child_id, terminal_class="success", changed_state=self._changed(), source=self.plan_source,
                    result_summary="different", expected_resource_version=1,
                )

    def test_terminal_rejects_wrong_lineage_and_stale_resource(self) -> None:
        with ControllerStore(self.state) as store:
            with self.assertRaises(ConstraintError):
                reconcile_child_terminal(
                    store, child_run_id=self.child_run, parent_run_id="run_" + "a" * 32, child_id=self.child_id,
                    terminal_class="success", changed_state=self._changed(), source=self.plan_source,
                )
            wrong_source = dict(self.plan_source)
            wrong_source["snapshotId"] = "snap_" + "f" * 32
            wrong_source["snapshotRef"] = "refs/pi/snapshots/" + wrong_source["snapshotId"]
            with self.assertRaises(ConstraintError):
                reconcile_child_terminal(
                    store, child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id,
                    terminal_class="success", changed_state=self._changed(), source=wrong_source,
                )
            with self.assertRaises(ResourceStaleError):
                reconcile_child_terminal(
                    store, child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id,
                    terminal_class="success", changed_state=self._changed(), source=self.plan_source,
                    expected_resource_version=99,
                )
            self.assertIsNone(store.conn.execute("SELECT * FROM child_terminal_records").fetchone())

    def test_terminal_classification_maps_failure_lost_and_attention(self) -> None:
        with ControllerStore(self.state) as store:
            failed = reconcile_child_terminal(
                store, child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id,
                terminal_class="failed", changed_state=self._changed(unknown=True), source=self.plan_source,
                result_summary="failed",
            )
            self.assertEqual(failed.observed_state, "failed")
        # A fresh fixture is used for the remaining classifications.
        with ControllerStore(self.state) as store:
            run_id = "run_" + "a" * 32
            conv = "conv_" + "a" * 32
            store.conn.execute(
                "INSERT INTO conversations(conversation_id,project_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (conv, self.project_id, "personal", "child2", "child2-session", str(self.root / "child2.jsonl"), "active", "ready", 1, "t", "t"),
            )
            store.create_run(run_id=run_id, conversation_id=conv, project_id=self.project_id, authority="read-only", parent_run_id=self.parent_run, parent_conversation_id=self.parent_conv, child_source=self.plan_source, runtime_spec_hash="runtime", build_id="build", capability_hash="cap")
            lost = reconcile_child_terminal(store, child_run_id=run_id, parent_run_id=self.parent_run, child_id="child_" + "a" * 32, terminal_class="lost", changed_state=self._changed(unknown=True), source=self.plan_source)
            self.assertEqual(lost.observed_state, "lost")

    def test_dirty_read_only_child_and_invalid_terminal_combinations_are_rejected(self) -> None:
        with ControllerStore(self.state) as store:
            with self.assertRaises(TerminalStateError):
                reconcile_child_terminal(store, child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id, terminal_class="success", changed_state=self._changed(dirty=True), source=self.plan_source)
            with self.assertRaises(TerminalStateError):
                reconcile_child_terminal(store, child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id, terminal_class="lost", changed_state=self._changed(unknown=True), source=self.plan_source, submitted_change_id="chg_" + "b" * 32, submitted_revision=1)

    def test_attention_preserves_writer_claim_fail_closed(self) -> None:
        with ControllerStore(self.state) as store:
            child_wc = "wc_" + "b" * 32
            child_conv = "conv_" + "b" * 32
            child_run = "run_" + "b" * 32
            writer_source = {**self.plan_source, "authority": "writer"}
            store.conn.execute(
                "INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (child_wc, self.project_id, "child", "worktree", "workstream", str(self.root / "writer"), "trusted-live", "present", "ready", 0, 1, 1, "t", "t"),
            )
            store.conn.execute(
                "INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (child_conv, self.project_id, child_wc, "workstream", "writer-child", "writer-session", str(self.root / "writer.jsonl"), "active", "ready", 1, "t", "t"),
            )
            store.create_run(
                run_id=child_run, conversation_id=child_conv, project_id=self.project_id, working_copy_id=child_wc,
                authority="writer", parent_run_id=self.parent_run, parent_conversation_id=self.parent_conv,
                child_source=writer_source, expected_working_copy_version=1, writer_epoch=1,
                runtime_spec_hash="runtime", build_id="build", capability_hash="cap",
            )
            with self.assertRaises(ConstraintError):
                reconcile_child_terminal(
                    store, child_run_id=child_run, parent_run_id=self.parent_run, child_id="child_" + "b" * 32,
                    terminal_class="success", changed_state=self._changed(), source=writer_source,
                    submitted_change_id="chg_" + "c" * 32, submitted_revision=1,
                )
            terminal = reconcile_child_terminal(
                store, child_run_id=child_run, parent_run_id=self.parent_run, child_id="child_" + "b" * 32,
                terminal_class="attention", changed_state=self._changed(dirty=True), source=writer_source,
                result_summary="dirty writer requires review",
            )
            self.assertEqual(terminal.observed_state, "needs_attention")
            self.assertEqual(store.conn.execute("SELECT active_writer_run_id FROM working_copies WHERE working_copy_id=?", (child_wc,)).fetchone()[0], child_run)

    def test_retention_eligibility_honors_reference_holds_and_explicit_delete(self) -> None:
        artifacts = ArtifactStore(self.state)
        record = self._artifact()
        future = (datetime.fromisoformat(record.created_at.replace("Z", "+00:00")) + timedelta(days=31)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.assertEqual(artifacts.eligible_cleanup(now=future, referenced_artifact_ids=[record.artifact_id]), [])
        candidates = artifacts.eligible_cleanup(now=future)
        self.assertEqual(candidates[0]["artifactId"], record.artifact_id)
        with self.assertRaises(InvalidRequestError):
            artifacts.eligible_cleanup(now=future, dry_run=False)
        artifacts.eligible_cleanup(now=future, dry_run=False, authorize=True)
        with self.assertRaises(ArtifactIntegrityError):
            artifacts.verify(record.artifact_id)

    def test_sensitive_content_is_not_in_sqlite_or_terminal_event(self) -> None:
        artifacts = ArtifactStore(self.state)
        secret = b"fixture-secret-content"
        record = self._artifact(content=secret)
        with ControllerStore(self.state) as store:
            register_artifact(store, record)
            stored = store.conn.execute("SELECT * FROM artifact_manifests").fetchone()
            self.assertNotIn(secret.decode(), " ".join(str(item) for item in stored))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("UPDATE artifact_manifests SET checksum='sha256:' || printf('%064d', 0) WHERE artifact_id=?", (record.artifact_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("DELETE FROM artifact_manifests WHERE artifact_id=?", (record.artifact_id,))
            terminal = reconcile_child_terminal(store, artifact_id=record.artifact_id, child_run_id=self.child_run, parent_run_id=self.parent_run, child_id=self.child_id, terminal_class="success", changed_state=self._changed(), source=self.plan_source)
            payload = store.conn.execute("SELECT payload_json FROM control_events WHERE event_kind='child.terminal'").fetchone()[0]
            self.assertNotIn(secret.decode(), payload)
            self.assertEqual(terminal.observed_state, "stopped")


if __name__ == "__main__":
    unittest.main()
