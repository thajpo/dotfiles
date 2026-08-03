from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PiRestartTests(unittest.TestCase):
    def test_kills_tmux_server_before_booting_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            home_bin = home / ".local/bin"
            home_bin.mkdir(parents=True)
            fake_bin.mkdir()
            log = root / "calls.log"

            tmux = fake_bin / "tmux"
            tmux.write_text(
                "#!/bin/sh\n"
                "printf 'tmux %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n"
            )
            tmux.chmod(0o755)
            pi_start = home_bin / "pi-start"
            pi_start.write_text(
                "#!/bin/sh\n"
                "printf 'pi-start %s\\n' \"$*\" >> \"$PI_RESTART_LOG\"\n"
            )
            pi_start.chmod(0o755)

            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "PI_RESTART_LOG": str(log),
            })
            environment.pop("TMUX", None)
            result = subprocess.run(
                [str(ROOT / "bin/pi-restart")],
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(), ["tmux kill-server", "pi-start all"])


if __name__ == "__main__":
    unittest.main()
