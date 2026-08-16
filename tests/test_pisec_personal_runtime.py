import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.pisec.harnesses.omp_personal import materialize_personal_runtime

ROOT = Path(__file__).resolve().parents[1]


def make_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o700)


def make_config(root: Path, real_omp: Path, fence: Path) -> dict:
    token = root / "gateway.token"
    token.write_text("g" * 48 + "\n")
    token.chmod(0o600)
    return {
        "schemaVersion": 3,
        "fencePath": str(fence),
        "harness": {
            "id": "omp",
            "config": {
                "executablePath": str(real_omp),
                "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(token)},
                "modelRoles": {"default": "openai-codex/model", "smol": "openai-codex/model"},
                "network": {"registryDomains": [], "developmentEndpoints": []},
            },
        },
        "workspace": {
            "id": "herdr",
            "config": {"sessionName": "pisec", "socketPath": str(root / "herdr.sock")},
        },
    }


class PersonalRuntimeTests(unittest.TestCase):
    def test_profiles_are_stable_per_cwd_and_real_fence_denies_sibling(self):
        fence = shutil.which("fence")
        if fence is None:
            self.skipTest("Fence is unavailable")
        with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir(mode=0o700)
            (home / ".omp" / "agent" / "extensions").mkdir(parents=True)
            first = root / "first"
            second = root / "second"
            first.mkdir(mode=0o755)
            second.mkdir(mode=0o755)
            (second / "secret.txt").write_text("sibling secret\n")
            real_omp = root / "real-omp"
            make_executable(real_omp, "#!/bin/sh\nexit 0\n")
            config = make_config(root, real_omp, Path(fence))
            environment = {"HOME": str(home), "XDG_STATE_HOME": str(root / "state")}
            with patch.dict(os.environ, environment, clear=False):
                first_profile = materialize_personal_runtime(first, config=config)
                replay = materialize_personal_runtime(first, config=config)
                second_profile = materialize_personal_runtime(second, config=config)

            self.assertEqual(first_profile["profile_id"], replay["profile_id"])
            self.assertNotEqual(first_profile["profile_id"], second_profile["profile_id"])
            self.assertNotEqual(first_profile["omp_agent_dir"], second_profile["omp_agent_dir"])
            first_policy = json.loads(Path(first_profile["fence_policy_path"]).read_text())
            self.assertIn(str(first), first_policy["filesystem"]["allowWrite"])
            self.assertNotIn(str(second), first_policy["filesystem"]["allowRead"])

            allowed = subprocess.run(
                [fence, "--settings", first_profile["fence_policy_path"], "--", "python3", "-c", "from pathlib import Path; Path('owned.txt').write_text('ok')"],
                cwd=first,
                text=True,
                capture_output=True,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual((first / "owned.txt").read_text(), "ok")
            denied = subprocess.run(
                [fence, "--settings", first_profile["fence_policy_path"], "--", "python3", "-c", f"from pathlib import Path; print(Path({str(second / 'secret.txt')!r}).read_text())"],
                cwd=first,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertNotIn("sibling secret", denied.stdout)

    def test_launcher_uses_synthetic_home_and_scrubs_provider_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir(mode=0o700)
            extension_dir = home / ".omp" / "agent" / "extensions"
            extension_dir.mkdir(parents=True)
            make_executable(extension_dir / "placeholder", "placeholder\n")
            cwd = root / "project"
            cwd.mkdir(mode=0o755)
            real_omp = root / "real-omp"
            make_executable(real_omp, "#!/bin/sh\nexit 0\n")
            fake_fence = root / "fence"
            make_executable(fake_fence, "#!/bin/sh\nprintf 'HOME=%s\\nPI=%s\\nOPENAI=%s\\nARGS=%s\\n' \"$HOME\" \"$PI_CODING_AGENT_DIR\" \"${OPENAI_API_KEY-unset}\" \"$*\"\n")
            config = make_config(root, real_omp, fake_fence)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "PISEC_CONFIG": str(config_path),
                    "PYTHONPATH": str(ROOT),
                    "PISEC_SURFACE_ID": "w1:p1",
                    "OPENAI_API_KEY": "must-not-cross",
                }
            )
            result = subprocess.run(
                ["python3", "-m", "scripts.pisec.harnesses.omp_personal"],
                cwd=cwd,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in result.stdout.splitlines())
            self.assertTrue(values["HOME"].startswith(str(root / "state" / "pisec-personal")))
            self.assertTrue(values["PI"].startswith(values["HOME"]))
            self.assertEqual(values["OPENAI"], "unset")
            self.assertIn(str(real_omp), values["ARGS"])
            self.assertIn("--config", values["ARGS"])

    def test_launcher_rejects_non_restore_arguments_before_materialization(self):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        result = subprocess.run(
            ["python3", "-m", "scripts.pisec.harnesses.omp_personal", "--print", "prompt"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 126)
        self.assertIn("unsupported personal OMP arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
