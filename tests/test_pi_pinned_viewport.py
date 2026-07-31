import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PinnedConversationViewportTests(unittest.TestCase):
    def test_editor_controls_remain_visible_while_scrolling_and_streaming(self):
        module = (ROOT / "pi/core/pinned-conversation-viewport.mjs").as_uri()
        script = f"""
import {{ PinnedConversationViewport, isMouseInput, parseWheelDirection, stripMouseInput }} from {module!r};
let lines = Array.from({{ length: 8 }}, (_, i) => `c${{i + 1}}`);
const tui = {{ terminal: {{ rows: 6 }}, renders: 0, requestRender() {{ this.renders++; }} }};
const conversation = {{ render: () => [...lines], invalidate() {{}} }};
const controls = {{ render: () => ["editor", "footer"], invalidate() {{}} }};
const viewport = new PinnedConversationViewport(tui, conversation, controls);
const equal = (actual, expected, label) => {{
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`${{label}}: ${{JSON.stringify(actual)}}`);
}};
equal(viewport.render(80), ["c5", "c6", "c7", "c8", "editor", "footer"], "latest");
viewport.scrollBy(2);
equal(viewport.render(80), ["c3", "c4", "c5", "c6", "editor", "footer"], "scrolled");
lines.push("c9");
equal(viewport.render(80), ["c3", "c4", "c5", "c6", "editor", "footer"], "stream anchor");
viewport.scrollToLatest();
equal(viewport.render(80), ["c6", "c7", "c8", "c9", "editor", "footer"], "latest again");
const up = "\\x1b[<64;10;4M";
const down = "\\x1b[<65;10;4M";
if (!isMouseInput(up) || !isMouseInput(up + down) || isMouseInput("plain")) throw new Error("mouse parser");
if (stripMouseInput(`a${{up}}b${{down}}`) !== "ab") throw new Error("mixed input stripping");
if (parseWheelDirection(up + up + down) !== 1) throw new Error("batched wheel");
if (parseWheelDirection("\\x1b[<66;10;4M") !== 0) throw new Error("horizontal wheel");
"""
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_patch_is_versioned_and_installed_transactionally(self):
        installer = (ROOT / "install.sh").read_text()
        patcher = (ROOT / "scripts/pi-patch-core").read_text()
        patch = (ROOT / "pi/patches/pi-coding-agent-0.82.1-pinned-editor.patch").read_text()
        self.assertIn('scripts/pi-patch-core" --check', installer)
        self.assertIn('PI_CORE_DIR="$CORE_STAGING" "$SCRIPT_DIR/scripts/pi-patch-core"', installer)
        self.assertIn("0.82.1", patcher)
        self.assertIn("pinned-conversation-viewport.mjs", patcher)
        self.assertIn("PinnedConversationViewport", patch)
        self.assertIn("stripMouseInput", patch)
        self.assertIn("conversationViewportContainer.addChild(this.widgetContainerBelow)", patch)
        self.assertIn("pinnedControlsContainer.addChild(this.editorContainer)", patch)
        self.assertIn("MOUSE_TRACKING_ENABLE_SEQUENCE", patch)


if __name__ == "__main__":
    unittest.main()
