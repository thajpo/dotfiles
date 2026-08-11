"""C1c CLI protocol-v2 shapes and bounded projected errors."""

from __future__ import annotations

import json
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from scripts.pi_control.greenfield_store import GreenfieldStore


ROOT = Path(__file__).resolve().parents[2]


class CLIProtocolV2Tests(unittest.TestCase):
    def _run(self, root: Path, request: dict) -> subprocess.CompletedProcess[str]:
        path = root / "request.json"
        path.write_text(json.dumps(request))
        return subprocess.run(
            [str(ROOT / "bin/pi-control"), "--state-root", str(root / "state"), "--json", "protocol", "--request-json", str(path)],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT)},
        )

    def test_cli_negotiates_v2_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with GreenfieldStore(root / "state"):
                pass
            result = self._run(root, {"protocolVersion": 2, "operation": "negotiate", "request": {}})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["protocolVersion"], 2)

    def test_cli_host_only_and_malformed_requests_are_projected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with GreenfieldStore(root / "state"):
                pass
            result = self._run(root, {"protocolVersion": 2, "operation": "activation.apply", "request": {}})
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stdout)["error"]
            self.assertEqual(error["code"], "CP_PROTOCOL_OPERATION")
            self.assertNotIn("Traceback", result.stdout + result.stderr)
            result = self._run(root, {"protocolVersion": 1, "operation": "negotiate", "request": {}})
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "CP_PROTOCOL_VERSION")


if __name__ == "__main__":
    unittest.main()
