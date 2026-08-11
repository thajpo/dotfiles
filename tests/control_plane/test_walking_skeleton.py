from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.pi_control.client import ControllerClient
from scripts.pi_control.run_manifest import capability_hash
from scripts.pi_control.store import ControllerStore


class WalkingSkeletonTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> None:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Skeleton", "GIT_AUTHOR_EMAIL": "skeleton@example.invalid", "GIT_COMMITTER_NAME": "Skeleton", "GIT_COMMITTER_EMAIL": "skeleton@example.invalid"}
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_complete_disposable_client_change_review_integration_twice(self) -> None:
        for iteration in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repo = root / "repo"
                repo.mkdir()
                self._git(repo, "init", "-q", "-b", "main")
                self._git(repo, "config", "user.name", "Skeleton")
                self._git(repo, "config", "user.email", "skeleton@example.invalid")
                (repo / "base.txt").write_text("base\n", encoding="utf-8")
                self._git(repo, "add", "base.txt")
                self._git(repo, "commit", "-qm", "base")
                base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
                self._git(repo, "branch", "target", base)
                project_id = "prj_" + f"{iteration + 1:01x}" * 32
                wc_id = "wc_" + f"{iteration + 2:01x}" * 32
                conv_id = "conv_" + f"{iteration + 3:01x}" * 32
                review_wc_id = "wc_" + f"{iteration + 4:01x}" * 32
                review_conv_id = "conv_" + f"{iteration + 5:01x}" * 32
                review_run_id = "run_" + f"{iteration + 6:01x}" * 32
                review_secret = "review-secret-" + f"{iteration + 1:01x}" * 48
                state = root / "state"
                with ControllerStore(state) as store:
                    store.conn.execute("CREATE TABLE IF NOT EXISTS dependency_changes(dependency_change_id TEXT PRIMARY KEY,change_id TEXT NOT NULL,revision INTEGER NOT NULL,disposition TEXT NOT NULL,lock_digest TEXT,exact_version TEXT)")
                    store.register_build("build", source_tree_hash="tree", artifact_manifest_hash="manifest", pi_version="pi", package_lock_hash="lock", status="active")
                    store.conn.execute("INSERT INTO projects(project_id,display_name,git_common_dir,git_common_device,git_common_inode,primary_checkout,object_format,trust_mode,policy_hash,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (project_id, "skeleton", str(repo / ".git"), 1, 1, str(repo), "sha1", "trusted", "policy", "active", "ready", 1, "t", "t"))
                    store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wc_id, project_id, "primary", "primary", "personal", str(repo), "refs/heads/main", "trusted-live", "present", "ready", 0, 1, 1, "t", "t"))
                    store.conn.execute("INSERT INTO working_copies(working_copy_id,project_id,display_name,kind,purpose,path,branch_ref,effective_mode,desired_state,observed_state,writer_epoch,resource_version,controller_owned,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (review_wc_id, project_id, "review", "review", "review", str(root / "review"), None, "read-only", "present", "ready", 0, 1, 1, "t", "t"))
                    store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (conv_id, project_id, wc_id, "personal", "skeleton", "skeleton-session", str(root / "session.jsonl"), "active", "ready", 1, "t", "t"))
                    store.conn.execute("INSERT INTO conversations(conversation_id,project_id,working_copy_id,role,display_name,pi_session_id,session_file,desired_state,observed_state,resource_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (review_conv_id, project_id, review_wc_id, "review", "review", "review-session", str(root / "review.jsonl"), "active", "ready", 1, "t", "t"))
                    store.create_run(run_id=review_run_id, conversation_id=review_conv_id, project_id=project_id, working_copy_id=review_wc_id, authority="read-only", runtime_spec_hash="runtime", build_id="build", capability_hash=capability_hash(review_secret))
                    store.conn.execute("UPDATE runs SET observed_state='running' WHERE run_id=?", (review_run_id,))
                    client = ControllerClient(state)
                    self.assertEqual(client.status(project_id, refresh=False)["project"]["project_id"], project_id)
                    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
                    request = {"projectId": project_id, "workingCopyId": wc_id, "targetRef": "refs/heads/target", "title": "Skeleton change", "summary": "complete flow", "captureMode": "dirty", "selectedPaths": ["candidate.txt"], "excludedPaths": [], "expectedStatusHash": None, "idempotencyKey": f"skeleton-{iteration}", "conversationId": conv_id, "actorType": "personal", "actorId": "personal", "authorizationId": None}
                    change = client.submit(request)
                    (repo / "candidate.txt").unlink()
                    analysis = client.analyze_integration({"projectId": project_id, "changeId": change["changeId"], "revision": 1, "targetWorkingCopyId": wc_id, "targetRef": "refs/heads/target", "integrationId": None})
                    evidence = {"integrationId": analysis["integrationId"], "analysisDigest": analysis["analysisDigest"], "targetOid": analysis["targetOid"]}
                    review = client.request_review({"changeId": change["changeId"], "revision": 1, "reviewerConversationId": review_conv_id, "reviewerRunId": review_run_id, "reviewerActorId": "reviewer", "reviewerCapabilitySecret": review_secret, "evidence": evidence, "reviewId": None})
                    client.submit_review({"reviewId": review["reviewId"], "verdict": "accept", "summary": "", "findings": "", "evidence": evidence, "reviewerRunId": review_run_id, "reviewerActorId": "reviewer", "reviewerCapabilitySecret": review_secret})
                    auth = client.authorize_integration({"integrationId": analysis["integrationId"], "actorId": "user", "requestContextId": f"skeleton-auth-{iteration}", "expiresAt": "2099-01-01T00:00:00Z", "reviewId": review["reviewId"]})
                    result = client.integrate({"integrationId": analysis["integrationId"], "authorizationId": auth["authorizationId"], "expectedResourceVersion": None})
                    self.assertEqual(result["state"], "succeeded")
                    self.assertEqual(subprocess.check_output(["git", "rev-parse", "refs/heads/target"], cwd=repo, text=True).strip(), change["tipOid"])


if __name__ == "__main__":
    unittest.main()
