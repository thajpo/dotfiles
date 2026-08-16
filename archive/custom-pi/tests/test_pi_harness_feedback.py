import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-harness-feedback.py"
spec = importlib.util.spec_from_file_location("pi_harness_feedback", SCRIPT)
assert spec and spec.loader
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


class HarnessFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "dotfiles"
        self.repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.repo)], check=True)
        self.agent_dir = self.root / "agent"
        self.records = self.agent_dir / "feedback" / "records"
        self.old_agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
        os.environ["PI_CODING_AGENT_DIR"] = str(self.agent_dir)
        self.project_id = feedback._repository_identity(str(self.repo))[1]

    def tearDown(self):
        if self.old_agent_dir is None:
            os.environ.pop("PI_CODING_AGENT_DIR", None)
        else:
            os.environ["PI_CODING_AGENT_DIR"] = self.old_agent_dir
        self.tmp.cleanup()

    def add_record(self, feedback_id, project_id, outcome="unreviewed", repository=None):
        self.records.mkdir(parents=True, exist_ok=True)
        source = {"agent": "worker"}
        if project_id is not None:
            source["projectId"] = project_id
        if repository is not None:
            source["repository"] = str(repository)
        value = {
            "schemaVersion": 1,
            "feedbackId": feedback_id,
            "createdAt": f"2026-08-05T00:00:0{len(list(self.records.glob('*.json')))}+00:00",
            "updatedAt": "2026-08-05T00:00:00+00:00",
            "source": source,
            "reason": "progress_update",
            "form": {
                "schema": "agent-feedback.v1",
                "kind": "harness-improvement",
                "title": feedback_id,
                "evidence": ["bounded evidence"],
                "recommendation": "make the next failure cheaper",
            },
            "contentDigest": "a" * 64,
            "lifecycle": "reviewed" if outcome != "unreviewed" else "delivered",
            "outcome": outcome,
            "raw": {"message": "secret prompt content"},
        }
        (self.records / f"{feedback_id}.json").write_text(json.dumps(value), encoding="utf-8")

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = feedback.main(list(args))
        self.assertEqual(result, 0)
        return output.getvalue()

    def test_default_view_is_central_across_projects_and_redacts_raw(self):
        self.add_record("current", self.project_id)
        self.add_record("other", "b" * 64)
        self.add_record("accepted", self.project_id, "accepted")
        current_path = self.records / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current["secretPayload"] = "must not escape the normalized projection"
        current["form"]["title"] = "safe\u001b]52;c;Y2xpcGJvYXJk\u0007 title"
        current["form"]["recommendation"] = "Bearer abcdefghijklmnop"
        current_path.write_text(json.dumps(current), encoding="utf-8")

        records = json.loads(self.invoke("--format", "json"))
        self.assertEqual({record["feedbackId"] for record in records}, {"current", "other"})
        self.assertTrue(all("raw" not in record and "secretPayload" not in record for record in records))
        projected = next(record for record in records if record["feedbackId"] == "current")
        self.assertNotIn("\u001b", projected["form"]["title"])
        self.assertNotIn("\u0007", projected["form"]["title"])
        self.assertEqual(projected["form"]["recommendation"], "[redacted]")

    def test_repository_filter_and_reviewed_records(self):
        self.add_record("current", self.project_id)
        self.add_record("fallback", None, repository=self.repo)
        self.add_record("other", "b" * 64)
        self.add_record("accepted", self.project_id, "accepted")

        records = json.loads(self.invoke("--repository", str(self.repo), "--format", "json"))
        self.assertEqual({record["feedbackId"] for record in records}, {"current", "fallback"})
        records = json.loads(self.invoke("--repository", str(self.repo), "--include-reviewed", "--format", "json"))
        self.assertEqual({record["feedbackId"] for record in records}, {"current", "fallback", "accepted"})

    def test_repository_and_all_projects_cannot_be_combined(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
            feedback.main(["--repository", str(self.repo), "--all-projects"])
        self.assertIn("mutually exclusive", error.getvalue())

    def test_sanitized_markdown_export_stays_in_repository(self):
        self.add_record("current", self.project_id)
        result = self.invoke(
            "--repository", str(self.repo), "--format", "markdown",
            "--output", "pi/HARNESS_FEEDBACK_LOG.md",
        )
        self.assertIn("Wrote sanitized", result)
        exported = (self.repo / "pi/HARNESS_FEEDBACK_LOG.md").read_text(encoding="utf-8")
        self.assertIn("current", exported)
        self.assertNotIn("secret prompt content", exported)
        self.assertEqual((self.repo / "pi/HARNESS_FEEDBACK_LOG.md").stat().st_mode & 0o777, 0o600)

    def test_export_refuses_an_existing_symlink(self):
        self.add_record("current", self.project_id)
        target = self.repo / "target.md"
        target.write_text("unchanged", encoding="utf-8")
        link = self.repo / "feedback.md"
        link.symlink_to(target)
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            result = feedback.main([
                "--repository", str(self.repo), "--format", "markdown",
                "--output", "feedback.md",
            ])
        self.assertEqual(result, 2)
        self.assertIn("must not be a symlink", error.getvalue())
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()
