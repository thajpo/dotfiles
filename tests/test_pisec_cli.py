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
            ["first-mate", "ensure", "dotfiles", "--json"],
            ["first-mate", "focus", "--json"],
        ):
            with self.subTest(arguments=arguments):
                self.assertTrue(parser().parse_args(arguments).json_output)

    def test_project_refresh_requires_explicit_all(self):
        parsed = parser().parse_args(["project", "refresh", "--all", "--wait-seconds", "12"])
        self.assertTrue(parsed.all)
        self.assertEqual(parsed.wait_seconds, 12)
        with self.assertRaises(SystemExit):
            parser().parse_args(["project", "refresh"])
    def test_secretary_commands_are_not_public_cli(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(["secretary", "ensure", "demo"])

    def test_reconcile_allows_the_full_live_project_set_to_converge(self):
        with patch("scripts.pisec.cli._call", return_value={"reconciled": True}) as call:
            result = main(["reconcile", "--json"])
        self.assertEqual(result, 0)
        call.assert_called_once_with("system.reconcile", {}, timeout=300.0)

    def test_first_mate_lifecycle_is_available_through_the_public_cli(self):
        value = {
            "reused": False,
            "workstream": {"desired_state": "active", "provisioning_state": "bound"},
            "binding": {"observed_state": "idle"},
        }
        output = StringIO()
        with patch("scripts.pisec.cli._call", return_value=value) as call, redirect_stdout(output):
            result = main(["first-mate", "ensure", "dotfiles"])
        self.assertEqual(result, 0)
        call.assert_called_once_with("first_mate.ensure", {"project": "dotfiles"}, timeout=60.0)
        self.assertIn("First Mate ready", output.getvalue())
        self.assertIn("Runtime: idle", output.getvalue())

        output = StringIO()
        with patch("scripts.pisec.cli._call", return_value={"focused": True}) as call, redirect_stdout(output):
            result = main(["first-mate", "focus"])
        self.assertEqual(result, 0)
        call.assert_called_once_with("first_mate.focus", {})
        self.assertIn("First Mate focused", output.getvalue())

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
