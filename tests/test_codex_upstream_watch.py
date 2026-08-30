from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "codex-upstream-watch"
CONFIG = ROOT / "codex" / "upstream-watch.conf"
SERVICE = ROOT / "systemd" / "user" / "codex-upstream-watch.service"
TIMER = ROOT / "systemd" / "user" / "codex-upstream-watch.timer"
PIN = "94cbbddafc1776d5e377bca1b05932c697e82238"
CANDIDATE = "1111111111111111111111111111111111111111"
LATER = "2222222222222222222222222222222222222222"


class CodexUpstreamWatchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.fake_bin = self.root / "bin"
        self.repo = self.root / "codex"
        self.state = self.root / "state"
        self.fake_bin.mkdir()
        self.repo.mkdir()
        self.remote_sha = self.root / "remote-sha"
        self.remote_sha.write_text(PIN + "\n", encoding="utf-8")
        self.spawn_mode = self.root / "spawn-mode"
        self.spawn_mode.write_text("success\n", encoding="utf-8")
        self.spawn_log = self.root / "spawn-calls"
        self.config = self.root / "watch.conf"
        self.config.write_text(
            "\n".join(
                [
                    "CODEX_UPSTREAM_RELEASE=0.151.0",
                    f"CODEX_PIN_SHA={PIN}",
                    "CODEX_UPSTREAM_REMOTE=https://example.invalid/codex.git",
                    "CODEX_UPSTREAM_REF=refs/heads/main",
                    f'CODEX_SOURCE_REPO="{self.repo}"',
                    "CODEX_FORK_BASE=main",
                    f'CODEX_SPAWN="{self.fake_bin / "spawn"}"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._write_executable(
            self.fake_bin / "git",
            """#!/bin/sh
if [ "$1 $2 $3" != "ls-remote --exit-code --refs" ]; then
  printf 'unexpected git args: %s\\n' "$*" >&2
  exit 90
fi
sha=$(cat "$FAKE_REMOTE_SHA")
printf '%s\\t%s\\n' "$sha" "$5"
""",
        )
        self._write_executable(
            self.fake_bin / "spawn",
            """#!/bin/sh
{
  printf 'HERDR_ENV=%s\\n' "${HERDR_ENV:-}"
  printf 'PWD=%s\\n' "$PWD"
  printf 'ARGS='; printf '<%s>' "$@"; printf '\\n'
} >> "$FAKE_SPAWN_LOG"
[ "$(cat "$FAKE_SPAWN_MODE")" = success ]
""",
        )
        for command in ("herdr", "jq", "codex"):
            self._write_executable(self.fake_bin / command, "#!/bin/sh\nexit 0\n")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _write_executable(path: Path, contents: str) -> None:
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    def run_watch(
        self, *arguments: str, path: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": path or f"{self.fake_bin}:{environment['PATH']}",
                "FAKE_REMOTE_SHA": str(self.remote_sha),
                "FAKE_SPAWN_MODE": str(self.spawn_mode),
                "FAKE_SPAWN_LOG": str(self.spawn_log),
                "CODEX_WATCH_NOW": "2026-08-30T12:00:00Z",
            }
        )
        return subprocess.run(
            [
                str(SCRIPT),
                "--config",
                str(self.config),
                "--state-dir",
                str(self.state),
                *arguments,
            ],
            env=environment,
            text=True,
            capture_output=True,
        )

    def test_unchanged_sha_never_invokes_spawn(self):
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged", result.stdout)
        self.assertFalse(self.spawn_log.exists())
        self.assertFalse((self.state / "pending.env").exists())
        observed = (self.state / "observed.env").read_text(encoding="utf-8")
        self.assertIn(f"UPSTREAM_SHA={PIN}", observed)
        self.assertIn("PIN_RELEASE=0.151.0", observed)

    def test_changed_sha_spawns_once_with_safety_packet(self):
        self.remote_sha.write_text(CANDIDATE + "\n", encoding="utf-8")
        first = self.run_watch()
        second = self.run_watch()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

        calls = self.spawn_log.read_text(encoding="utf-8")
        self.assertEqual(calls.count("HERDR_ENV=1"), 1)
        self.assertIn(f"PWD={self.repo}", calls)
        self.assertIn("<--base><main><-k><codex>", calls)
        self.assertIn("<-b><codex-upstream-111111111111>", calls)
        self.assertIn(f"Candidate upstream SHA: {CANDIDATE}", calls)
        self.assertIn("configured fork base (main)", calls)
        self.assertIn("Never install a Codex binary", calls)
        self.assertIn("Never push any branch", calls)

        pending = (self.state / "pending.env").read_text(encoding="utf-8")
        spawned = (self.state / "spawned.env").read_text(encoding="utf-8")
        self.assertIn(f"BASE_PIN_SHA={PIN}", pending)
        self.assertIn(f"CANDIDATE_SHA={CANDIDATE}", pending)
        self.assertIn(f"CANDIDATE_SHA={CANDIDATE}", spawned)

    def test_later_remote_sha_does_not_supersede_pending_campaign(self):
        self.remote_sha.write_text(CANDIDATE + "\n", encoding="utf-8")
        self.assertEqual(self.run_watch().returncode, 0)
        self.remote_sha.write_text(LATER + "\n", encoding="utf-8")
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("latest observed SHA", result.stdout)
        self.assertEqual(self.spawn_log.read_text().count("HERDR_ENV=1"), 1)
        pending = (self.state / "pending.env").read_text(encoding="utf-8")
        self.assertIn(f"CANDIDATE_SHA={CANDIDATE}", pending)
        observed = (self.state / "observed.env").read_text(encoding="utf-8")
        self.assertIn(f"UPSTREAM_SHA={LATER}", observed)

    def test_failed_spawn_stays_pending_and_retries(self):
        self.remote_sha.write_text(CANDIDATE + "\n", encoding="utf-8")
        self.spawn_mode.write_text("fail\n", encoding="utf-8")
        failed = self.run_watch()
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue((self.state / "pending.env").exists())
        self.assertFalse((self.state / "spawned.env").exists())
        attempt = (self.state / "last-attempt.env").read_text(encoding="utf-8")
        self.assertIn("RESULT=failed", attempt)

        self.spawn_mode.write_text("success\n", encoding="utf-8")
        retried = self.run_watch()
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.spawn_log.read_text().count("HERDR_ENV=1"), 2)
        self.assertTrue((self.state / "spawned.env").exists())

    def test_missing_codex_on_service_path_fails_pending_before_spawn(self):
        self.remote_sha.write_text(CANDIDATE + "\n", encoding="utf-8")
        (self.fake_bin / "codex").unlink()
        result = self.run_watch(path=f"{self.fake_bin}:/usr/bin:/bin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("codex is required on PATH", result.stderr)
        self.assertTrue((self.state / "pending.env").exists())
        self.assertFalse(self.spawn_log.exists())

    def test_dry_run_is_read_only(self):
        self.remote_sha.write_text(CANDIDATE + "\n", encoding="utf-8")
        result = self.run_watch("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("normal mode would record", result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.spawn_log.exists())

    def test_pin_advance_archives_previous_campaign(self):
        self.remote_sha.write_text(CANDIDATE + "\n", encoding="utf-8")
        self.assertEqual(self.run_watch().returncode, 0)
        updated = self.config.read_text(encoding="utf-8").replace(
            f"CODEX_PIN_SHA={PIN}", f"CODEX_PIN_SHA={CANDIDATE}"
        ).replace("CODEX_UPSTREAM_RELEASE=0.151.0", "CODEX_UPSTREAM_RELEASE=0.152.0")
        self.config.write_text(updated, encoding="utf-8")
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unchanged", result.stdout)
        history = self.state / "history"
        self.assertTrue((history / f"{PIN}.{CANDIDATE}.pending.env").exists())
        self.assertTrue((history / f"{PIN}.{CANDIDATE}.spawned.env").exists())
        self.assertFalse((self.state / "pending.env").exists())

    def test_checked_in_pin_and_units_are_explicit(self):
        config = CONFIG.read_text(encoding="utf-8")
        self.assertIn("CODEX_UPSTREAM_RELEASE=0.151.0", config)
        self.assertIn(f"CODEX_PIN_SHA={PIN}", config)
        self.assertIn("CODEX_FORK_BASE=main", config)
        service = SERVICE.read_text(encoding="utf-8")
        timer = TIMER.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("HERDR_ENV=1", service)
        self.assertIn("HERDR_SESSION=main", service)
        local_bin = service.index("%h/.local/bin")
        nvm_bin = service.index("%h/.nvm/versions/node/v22.19.0/bin")
        self.assertLess(local_bin, nvm_bin)
        self.assertIn("OnUnitActiveSec=6h", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
