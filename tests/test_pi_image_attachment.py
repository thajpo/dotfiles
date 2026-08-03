import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ImageAttachmentTests(unittest.TestCase):
    def test_pinned_native_image_configuration_and_no_tmp_contract(self):
        settings = json.loads((ROOT / "pi/settings.json").read_text())
        package = json.loads((ROOT / "pi/npm/package.json").read_text())
        keybindings = json.loads((ROOT / "pi/keybindings.json").read_text())
        image_config = json.loads((ROOT / "pi/pi-image-tools.json").read_text())
        self.assertEqual(package["dependencies"]["pi-image-tools"], "1.4.0")
        self.assertIn("npm:pi-image-tools@1.4.0", settings["packages"])
        self.assertEqual(keybindings["app.clipboard.pasteImage"], [])
        self.assertEqual(image_config["shortcuts"]["pasteImage"], ["ctrl+v"])
        self.assertTrue(image_config["shortcuts"]["suppressBuiltinConflictWarnings"])
        self.assertIn("ImageContent", (ROOT / "pi/README.md").read_text())
        self.assertNotIn("host /tmp", (ROOT / "bin/pi").read_text())

    def test_native_image_blocks_are_preserved_by_session_and_fork_shapes(self):
        session = [
            {"type": "session", "version": 3, "id": "image-root", "cwd": "/workspace"},
            {"type": "message", "id": "m1", "message": {"role": "user", "content": [
                {"type": "text", "text": "inspect this"},
                {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            ]}},
        ]
        fork = [dict(entry) for entry in session]
        self.assertEqual(fork[1]["message"]["content"][1]["type"], "image")
        self.assertEqual(fork[1]["message"]["content"][1]["data"], "aGVsbG8=")
        self.assertNotIn("/tmp/pi-clipboard", json.dumps(session))
        self.assertNotIn("/tmp/pi-clipboard", json.dumps(fork))

    def test_hidden_success_patch_preserves_root_trigger_and_visible_failures(self):
        patch = (ROOT / "pi/patches/pi-subagents-0.35.1-hidden-success.patch").read_text()
        config = json.loads((ROOT / "pi/extensions/subagent/config.json").read_text())
        self.assertIn("display: !hide", patch)
        self.assertIn("triggerTurn: true", patch)
        self.assertIn('details.every((detail) => detail.status === "completed")', patch)
        self.assertEqual(config["completionVisibility"], "hidden-success")


if __name__ == "__main__":
    unittest.main()
