from pathlib import Path
import os
import plistlib
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYNC_INSTALL = ROOT / "scripts" / "dotfiles-sync-install.sh"
WORKFLOW_INSTALL = ROOT / "scripts" / "agent-workflow-install.sh"
DOCTOR = ROOT / "scripts" / "agent-workflow-doctor.sh"


class MacOSWorkflowTests(unittest.TestCase):
    def command_environment(self, home: Path, fake_bin: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({"HOME": str(home), "PATH": f"{fake_bin}:{environment['PATH']}"})
        return environment

    def test_sync_install_writes_idempotent_launchd_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            repo = root / "dotfiles"
            fake_bin.mkdir()
            (repo / "scripts").mkdir(parents=True)
            (repo / "scripts" / "dotfiles-sync.sh").write_text("#!/bin/bash\n")

            (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
            (fake_bin / "launchctl").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/launchctl.log"\n'
            )
            for command in ("uname", "launchctl"):
                (fake_bin / command).chmod(0o755)

            environment = self.command_environment(home, fake_bin)
            environment["DOTFILES_DIR"] = str(repo)
            for _ in range(2):
                result = subprocess.run(
                    [str(SYNC_INSTALL)],
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            plist_path = home / "Library" / "LaunchAgents" / "com.user.dotfiles-sync.plist"
            document = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(document["Label"], "com.user.dotfiles-sync")
            self.assertEqual(document["StartInterval"], 7200)
            self.assertEqual(document["ProgramArguments"][-1], str(repo / "scripts" / "dotfiles-sync.sh"))
            calls = (home / "launchctl.log").read_text().splitlines()
            self.assertEqual(sum(line.startswith("bootstrap ") for line in calls), 2)
            self.assertEqual(sum(line.startswith("bootout ") for line in calls), 2)

    def test_skills_only_installer_selects_and_links_macos_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            repo = root / "dotfiles"
            (repo / "opencode").mkdir(parents=True)
            (repo / "opencode" / "opencode.jsonc").write_text("{}\n")
            (repo / "machines").mkdir()
            (repo / "machines" / "macos-arm64.env").write_text("DOTFILES_MACHINE_ID=macos-arm64\n")
            fake_bin.mkdir()
            (fake_bin / "uname").write_text(
                "#!/bin/sh\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n"
            )
            (fake_bin / "uname").chmod(0o755)

            environment = self.command_environment(home, fake_bin)
            environment["DOTFILES_DIR"] = str(repo)
            result = subprocess.run(
                [str(WORKFLOW_INSTALL), "--skills-only"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = home / ".config" / "dotfiles" / "machine.env"
            self.assertEqual(profile.resolve(), (repo / "machines" / "macos-arm64.env").resolve())

    def test_full_pisec_install_fails_closed_before_macos_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            repo = root / "dotfiles"
            (repo / "machines").mkdir(parents=True)
            (repo / "machines" / "macos-arm64.env").write_text("DOTFILES_MACHINE_ID=macos-arm64\n")
            fake_bin.mkdir()
            (fake_bin / "uname").write_text(
                "#!/bin/sh\nif [ \"${1:-}\" = \"-m\" ]; then printf 'arm64\\n'; else printf 'Darwin\\n'; fi\n"
            )
            (fake_bin / "uname").chmod(0o755)

            environment = self.command_environment(home, fake_bin)
            environment["DOTFILES_DIR"] = str(repo)
            result = subprocess.run(
                [
                    str(WORKFLOW_INSTALL),
                    "--collie-host",
                    "example.ts.net",
                    "--collie-trusted-user",
                    "user@example.com",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("full Pisec installation currently requires Linux", result.stderr)
            self.assertFalse(home.exists())

    def test_doctor_skips_linux_only_pisec_checks_on_macos(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            repo = home / "dotfiles"
            (repo / "opencode").mkdir(parents=True)
            (repo / "skills").mkdir()
            (repo / "opencode" / "opencode.jsonc").write_text("{}\n")
            (home / ".config" / "opencode").mkdir(parents=True)
            (home / ".codex").mkdir()
            (home / ".config" / "opencode" / "opencode.jsonc").symlink_to(repo / "opencode" / "opencode.jsonc")
            (home / ".skills").symlink_to(repo / "skills")
            (home / ".config" / "opencode" / "skills").symlink_to(home / ".skills")
            (home / ".codex" / "skills").symlink_to(home / ".skills")
            fake_bin.mkdir()
            (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
            for command in ("git", "tmux", "gh", "omp", "pi"):
                (fake_bin / command).write_text("#!/bin/sh\nexit 0\n")
            for command in ("uname", "git", "tmux", "gh", "omp", "pi"):
                (fake_bin / command).chmod(0o755)

            result = subprocess.run(
                [str(DOCTOR)],
                cwd=ROOT,
                env=self.command_environment(home, fake_bin),
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("full Pisec stack is Linux-only", result.stdout)


if __name__ == "__main__":
    unittest.main()
