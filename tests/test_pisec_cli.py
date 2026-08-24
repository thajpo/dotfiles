from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from scripts.pisec.cli import format_result, main, parser


class PisecCliTests(unittest.TestCase):
    def test_no_arguments_prints_help_instead_of_argparse_error(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main([])
        self.assertEqual(result, 0)
        self.assertIn("Pisec is the host-side workflow broker", output.getvalue())
        self.assertIn("pisec project register --path ~/src/project", output.getvalue())

    def test_json_flag_is_supported_before_or_after_a_command(self):
        for arguments in (
            ["--json", "status"],
            ["status", "--json"],
            ["project", "--json", "list"],
            ["project", "list", "--json"],
            ["project", "open", "demo", "--json"],
            ["project", "refresh", "--all", "--wait-seconds", "0", "--json"],
            ["release", "build", "--json"],
            ["release", "install", "--wait-seconds", "0", "--json"],
            ["release", "list", "--json"],
            ["release", "activate", "rel_" + "a" * 32, "--json"],
        ):
            with self.subTest(arguments=arguments):
                self.assertTrue(parser().parse_args(arguments).json_output)

    def test_project_refresh_requires_explicit_all(self):
        parsed = parser().parse_args(["project", "refresh", "--all", "--wait-seconds", "12"])
        self.assertTrue(parsed.all)
        self.assertEqual(parsed.wait_seconds, 12)
        with self.assertRaises(SystemExit):
            parser().parse_args(["project", "refresh"])
    def test_release_activation_accepts_refresh_wait(self):
        parsed = parser().parse_args(["release", "activate", "rel_" + "a" * 32, "--wait-seconds", "12"])
        self.assertEqual(parsed.wait_seconds, 12)

    def test_release_install_builds_activates_and_reconciles(self):
        release_id = "rel_" + "a" * 32
        calls = []

        def fake_call(operation, payload, **kwargs):
            calls.append((operation, payload, kwargs))
            if operation == "runtime.release.build":
                return {"release_id": release_id}
            if operation == "runtime.release.activate":
                return {"refresh": {"ok": True, "pending": [], "failed": []}}
            return {"integrations": []}

        output = StringIO()
        with patch("scripts.pisec.cli._call", side_effect=fake_call), redirect_stdout(output):
            result = main(["release", "install", "--wait-seconds", "0", "--json"])

        self.assertEqual(result, 0)
        self.assertEqual([call[0] for call in calls], [
            "runtime.release.build",
            "runtime.release.activate",
            "system.reconcile",
        ])
        self.assertTrue(json.loads(output.getvalue())["converged"])

    def test_release_install_returns_failure_when_refresh_does_not_converge(self):
        release_id = "rel_" + "b" * 32

        def fake_call(operation, payload, **kwargs):
            if operation == "runtime.release.build":
                return {"release_id": release_id}
            if operation == "runtime.release.activate":
                return {"refresh": {"ok": False, "pending": ["ws_busy"], "failed": []}}
            return {"integrations": []}

        output = StringIO()
        with patch("scripts.pisec.cli._call", side_effect=fake_call), redirect_stdout(output):
            result = main(["release", "install", "--wait-seconds", "0", "--json"])

        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())["converged"])


    def test_secretary_commands_are_not_public_cli(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(["secretary", "ensure", "demo"])

    def test_status_has_human_readable_default(self):
        output = format_result(
            ("status",),
            {
                "schema": "pisec-core",
                "version": 3,
                "projects": [
                    {"display_name": "demo", "project_id": "prj_" + "a" * 32, "default_ref": "main"},
                ],
            },
        )
        self.assertIn("Pisec status", output)
        self.assertIn("NAME", output)
        self.assertIn("demo", output)
        self.assertNotIn('"projects"', output)

    def test_json_output_remains_machine_readable(self):
        value = {"projects": [], "schema": "pisec-core", "version": 3}
        output = format_result(("status",), value, as_json=True)
        self.assertEqual(json.loads(output), value)


if __name__ == "__main__":
    unittest.main()
