#!/usr/bin/env python3
"""Behavioral tests for the stateless Herdr worker projection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "herdr-workers-state"
LOADER = SourceFileLoader("herdr_workers_state", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC
MODULE = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(MODULE)


def command(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class WorkersStateTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.worker = self.root / "worker"
        self.sessions = self.root / "sessions"
        self.repo.mkdir()
        command("git", "init", "-b", "master", cwd=self.repo)
        command("git", "config", "user.email", "test@example.com", cwd=self.repo)
        command("git", "config", "user.name", "Test User", cwd=self.repo)
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.repo)
        command("git", "commit", "-m", "base", cwd=self.repo)
        self.base_sha = command("git", "rev-parse", "HEAD", cwd=self.repo)
        command(
            "git",
            "worktree",
            "add",
            "-b",
            "worker-change",
            str(self.worker),
            cwd=self.repo,
        )
        (self.worker / "tracked.txt").write_text("worker\n", encoding="utf-8")
        command("git", "add", "tracked.txt", cwd=self.worker)
        command("git", "commit", "-m", "worker", cwd=self.worker)
        self.head_sha = command("git", "rev-parse", "HEAD", cwd=self.worker)
        self.session_id = "session-worker-1"
        self.write_rollout("gpt-6-luna", "high")

    def write_rollout(self, model: str, effort: str, malformed: bool = False) -> None:
        directory = self.sessions / "2026" / "09" / "02"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-test-{self.session_id}.jsonl"
        records = [
            {
                "type": "turn_context",
                "payload": {"model": model, "effort": effort},
            }
        ]
        text = "\n".join(json.dumps(record) for record in records) + "\n"
        if malformed:
            text += "{not-json\n"
        path.write_text(text, encoding="utf-8")

    def topology(self) -> dict:
        return {
            "source": {
                "source_workspace_id": "w-source",
                "source_checkout_path": str(self.repo),
                "repo_root": str(self.repo),
                "repo_name": "repo",
            },
            "worktrees": [
                {
                    "branch": "master",
                    "path": str(self.repo),
                    "is_linked_worktree": False,
                    "open_workspace_id": "w-source",
                },
                {
                    "branch": "worker-change",
                    "path": str(self.worker),
                    "is_linked_worktree": True,
                    "is_prunable": False,
                    "open_workspace_id": "w-worker",
                },
                {
                    "branch": "closed-worker",
                    "path": str(self.root / "closed"),
                    "is_linked_worktree": True,
                    "is_prunable": False,
                },
            ],
        }

    def agents(self, model: str = "gpt-6-luna", effort: str = "high") -> dict:
        return {
            "agents": [
                {
                    "agent": "codex",
                    "agent_status": "idle",
                    "cwd": str(self.worker),
                    "name": "worker-change",
                    "pane_id": "w-worker:p1",
                    "workspace_id": "w-worker",
                    "agent_session": {
                        "source": "herdr:codex",
                        "value": self.session_id,
                    },
                    "tokens": {
                        "worker_owner_workspace": "w-source",
                        "worker_base_sha": self.base_sha,
                        "worker_task": "change tracked behavior",
                        "worker_cohort": "tests",
                        "worker_model_requested": model,
                        "worker_effort_requested": effort,
                        "worker_subagent_model_requested": model,
                        "worker_subagent_effort_requested": effort,
                        "worker_launch_policy": "codex-luna-high-v2",
                        "worker_tests_status": "pass",
                        "worker_test_evidence": "unit suite",
                    },
                }
            ]
        }

    def project(self, **kwargs) -> dict:
        return MODULE.build_projection(
            kwargs.get("topology", self.topology()),
            kwargs.get("agents", self.agents()),
            kwargs.get("workspace", "w-source"),
            self.sessions,
            kwargs.get("expectations", {}),
        )

    def test_ready_worker_uses_native_topology_git_and_observed_model(self) -> None:
        result = self.project(expectations={"w-worker": self.head_sha})

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["counts"]["workers"], 1)
        worker = result["workers"][0]
        self.assertEqual(worker["workspace_id"], "w-worker")
        self.assertEqual(worker["base_sha"], self.base_sha)
        self.assertEqual(worker["head_sha"], self.head_sha)
        self.assertEqual(worker["changed_files"], ["tracked.txt"])
        self.assertEqual(worker["model"]["attestation"], "pass")
        self.assertEqual(worker["model"]["observed"]["effort"], "high")
        self.assertTrue(worker["readiness"]["ready"])

    def test_dirty_untracked_worktree_is_not_ready(self) -> None:
        (self.worker / "untracked.txt").write_text("draft\n", encoding="utf-8")

        worker = self.project()["workers"][0]

        self.assertFalse(worker["worktree_state"]["clean"])
        self.assertEqual(worker["worktree_state"]["untracked"], ["untracked.txt"])
        self.assertIn("worktree_dirty", worker["readiness"]["reasons"])

    def test_model_mismatch_fails_attestation(self) -> None:
        self.write_rollout("gpt-5.6-sol", "high")

        worker = self.project()["workers"][0]

        self.assertEqual(worker["model"]["attestation"], "fail")
        self.assertIn("model_attestation_not_pass", worker["readiness"]["reasons"])

    def test_malformed_rollout_fails_closed(self) -> None:
        self.write_rollout("gpt-6-luna", "high", malformed=True)

        worker = self.project()["workers"][0]

        self.assertEqual(worker["model"]["attestation"], "unknown")
        self.assertEqual(worker["model"]["observed"]["status"], "unknown")

    def test_worker_workspace_is_rejected_as_project_owner(self) -> None:
        with self.assertRaises(MODULE.StateError) as raised:
            self.project(workspace="w-worker")

        self.assertEqual(raised.exception.exit_code, 3)
        self.assertIn("source Space is w-source", str(raised.exception))

    def test_sha_drift_and_missing_worker_expectations_are_explicit(self) -> None:
        result = self.project(
            expectations={"w-worker": "0" * 40, "w-missing": "1" * 40}
        )

        self.assertFalse(result["expectations"]["match"])
        self.assertEqual(len(result["expectations"]["mismatches"]), 2)
        self.assertIn(
            "head_expectation_mismatch", result["workers"][0]["readiness"]["reasons"]
        )

    def test_cli_emits_versioned_json_and_returns_four_on_drift(self) -> None:
        topology = self.root / "topology.json"
        agents = self.root / "agents.json"
        topology.write_text(json.dumps({"result": self.topology()}), encoding="utf-8")
        agents.write_text(json.dumps({"result": self.agents()}), encoding="utf-8")

        result = subprocess.run(
            [
                str(SCRIPT),
                "--workspace",
                "w-source",
                "--worktrees-json",
                str(topology),
                "--agents-json",
                str(agents),
                "--sessions-dir",
                str(self.sessions),
                "--expect-sha",
                f"w-worker={'0' * 40}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)

        self.assertEqual(result.returncode, 4)
        self.assertEqual(payload["schema_version"], 1)
        self.assertFalse(payload["expectations"]["match"])


if __name__ == "__main__":
    unittest.main()
