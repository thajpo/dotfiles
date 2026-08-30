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
BASELINE = "94cbbddafc1776d5e377bca1b05932c697e82238"
TAG_151_OBJECT = "d8673cb68e349c208659b986697773d3145dbb14"
TAG_151_COMMIT = "78c290807ce710180111df227df3b7a4fe845452"
CANDIDATE_OBJECT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CANDIDATE_SHA = "1111111111111111111111111111111111111111"
LATER_OBJECT = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
LATER_SHA = "2222222222222222222222222222222222222222"


def annotated_tag(version: str, tag_object: str, commit: str) -> str:
    tag = f"refs/tags/rust-v{version}"
    return f"{tag_object}\t{tag}\n{commit}\t{tag}^{{}}\n"


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
        self.remote_refs = self.root / "remote-refs"
        self.set_remote_refs(annotated_tag("0.151.0", TAG_151_OBJECT, TAG_151_COMMIT))
        self.spawn_mode = self.root / "spawn-mode"
        self.spawn_mode.write_text("success\n", encoding="utf-8")
        self.spawn_log = self.root / "spawn-calls"
        self.config = self.root / "watch.conf"
        self.config.write_text(
            "\n".join(
                [
                    "CODEX_INSTALLED_RELEASE=0.151.0",
                    f"CODEX_FORK_UPSTREAM_BASELINE_SHA={BASELINE}",
                    "CODEX_UPSTREAM_REMOTE=https://example.invalid/codex.git",
                    "CODEX_STABLE_TAG_PREFIX=rust-v",
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
if [ "$1" != ls-remote ] || [ "$2" != --tags ] || [ "$4" != "refs/tags/rust-v*" ]; then
  printf 'unexpected git args: %s\\n' "$*" >&2
  exit 90
fi
cat "$FAKE_REMOTE_REFS"
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

    def set_remote_refs(self, contents: str) -> None:
        self.remote_refs.write_text(contents, encoding="utf-8")

    def add_stable_candidate(self) -> None:
        self.set_remote_refs(
            annotated_tag("0.152.0", CANDIDATE_OBJECT, CANDIDATE_SHA)
            + annotated_tag("0.151.0", TAG_151_OBJECT, TAG_151_COMMIT)
        )

    def run_watch(
        self, *arguments: str, path: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "PATH": path or f"{self.fake_bin}:{environment['PATH']}",
                "FAKE_REMOTE_REFS": str(self.remote_refs),
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

    def test_same_stable_version_never_spawns_despite_distinct_baseline(self):
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("does not advance", result.stdout)
        self.assertFalse(self.spawn_log.exists())
        self.assertFalse((self.state / "pending.env").exists())
        observed = (self.state / "observed.env").read_text(encoding="utf-8")
        self.assertIn("LATEST_STABLE_RELEASE=0.151.0", observed)
        self.assertIn(f"LATEST_STABLE_SHA={TAG_151_COMMIT}", observed)
        self.assertIn(f"FORK_UPSTREAM_BASELINE_SHA={BASELINE}", observed)
        self.assertNotEqual(TAG_151_COMMIT, BASELINE)

    def test_prerelease_tags_are_ignored(self):
        prerelease_tag = "refs/tags/rust-v9.0.0-alpha.1"
        self.set_remote_refs(
            annotated_tag("0.151.0", TAG_151_OBJECT, TAG_151_COMMIT)
            + f"{LATER_OBJECT}\t{prerelease_tag}\n"
            + f"{LATER_SHA}\t{prerelease_tag}^{{}}\n"
        )
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.spawn_log.exists())
        observed = (self.state / "observed.env").read_text(encoding="utf-8")
        self.assertIn("LATEST_STABLE_RELEASE=0.151.0", observed)
        self.assertNotIn("9.0.0", observed)

    def test_newer_stable_release_spawns_once_with_peeled_commit(self):
        self.add_stable_candidate()
        first = self.run_watch()
        second = self.run_watch()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

        calls = self.spawn_log.read_text(encoding="utf-8")
        self.assertEqual(calls.count("HERDR_ENV=1"), 1)
        self.assertIn(f"PWD={self.repo}", calls)
        self.assertIn("<--base><main><-k><codex>", calls)
        self.assertIn("<-b><codex-upstream-0-152-0-111111111111>", calls)
        self.assertIn("Candidate stable release: 0.152.0", calls)
        self.assertIn("Candidate stable tag: rust-v0.152.0", calls)
        self.assertIn(f"Candidate dereferenced tag commit SHA: {CANDIDATE_SHA}", calls)
        self.assertNotIn(CANDIDATE_OBJECT, calls)
        self.assertIn("configured fork base (main)", calls)
        self.assertIn("Never install a Codex binary", calls)
        self.assertIn("Never push any branch", calls)

        pending = (self.state / "pending.env").read_text(encoding="utf-8")
        spawned = (self.state / "spawned.env").read_text(encoding="utf-8")
        self.assertIn("BASE_INSTALLED_RELEASE=0.151.0", pending)
        self.assertIn(f"BASE_FORK_UPSTREAM_BASELINE_SHA={BASELINE}", pending)
        self.assertIn("CANDIDATE_RELEASE=0.152.0", pending)
        self.assertIn("CANDIDATE_TAG=rust-v0.152.0", pending)
        self.assertIn(f"CANDIDATE_SHA={CANDIDATE_SHA}", pending)
        self.assertNotIn(CANDIDATE_OBJECT, pending)
        self.assertIn("CANDIDATE_RELEASE=0.152.0", spawned)
        self.assertIn(f"CANDIDATE_SHA={CANDIDATE_SHA}", spawned)

    def test_later_stable_release_does_not_supersede_pending_campaign(self):
        self.add_stable_candidate()
        self.assertEqual(self.run_watch().returncode, 0)
        self.set_remote_refs(
            annotated_tag("0.152.0", CANDIDATE_OBJECT, CANDIDATE_SHA)
            + annotated_tag("0.153.0", LATER_OBJECT, LATER_SHA)
        )
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("latest stable 0.153.0 observed", result.stdout)
        self.assertEqual(self.spawn_log.read_text().count("HERDR_ENV=1"), 1)
        pending = (self.state / "pending.env").read_text(encoding="utf-8")
        self.assertIn("CANDIDATE_RELEASE=0.152.0", pending)
        self.assertIn(f"CANDIDATE_SHA={CANDIDATE_SHA}", pending)
        observed = (self.state / "observed.env").read_text(encoding="utf-8")
        self.assertIn("LATEST_STABLE_RELEASE=0.153.0", observed)
        self.assertIn(f"LATEST_STABLE_SHA={LATER_SHA}", observed)

    def test_failed_spawn_stays_pending_and_retries(self):
        self.add_stable_candidate()
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
        self.add_stable_candidate()
        (self.fake_bin / "codex").unlink()
        result = self.run_watch(path=f"{self.fake_bin}:/usr/bin:/bin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("codex is required on PATH", result.stderr)
        self.assertTrue((self.state / "pending.env").exists())
        self.assertFalse(self.spawn_log.exists())

    def test_dry_run_is_read_only(self):
        self.add_stable_candidate()
        result = self.run_watch("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("newer stable release", result.stdout)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.spawn_log.exists())

    def test_manual_promotion_archives_previous_campaign(self):
        self.add_stable_candidate()
        self.assertEqual(self.run_watch().returncode, 0)
        updated = self.config.read_text(encoding="utf-8").replace(
            "CODEX_INSTALLED_RELEASE=0.151.0", "CODEX_INSTALLED_RELEASE=0.152.0"
        ).replace(
            f"CODEX_FORK_UPSTREAM_BASELINE_SHA={BASELINE}",
            f"CODEX_FORK_UPSTREAM_BASELINE_SHA={CANDIDATE_SHA}",
        )
        self.config.write_text(updated, encoding="utf-8")
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("does not advance", result.stdout)
        history = self.state / "history"
        stem = f"0.151.0.0.152.0.{CANDIDATE_SHA}"
        self.assertTrue((history / f"{stem}.pending.env").exists())
        self.assertTrue((history / f"{stem}.spawned.env").exists())
        self.assertFalse((self.state / "pending.env").exists())

    def test_half_advanced_promotion_config_fails_closed(self):
        self.add_stable_candidate()
        self.assertEqual(self.run_watch().returncode, 0)
        updated = self.config.read_text(encoding="utf-8").replace(
            "CODEX_INSTALLED_RELEASE=0.151.0", "CODEX_INSTALLED_RELEASE=0.152.0"
        )
        self.config.write_text(updated, encoding="utf-8")
        result = self.run_watch()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("advance CODEX_INSTALLED_RELEASE", result.stderr)

    def test_checked_in_facts_and_units_are_explicit(self):
        config = CONFIG.read_text(encoding="utf-8")
        self.assertIn("CODEX_INSTALLED_RELEASE=0.151.0", config)
        self.assertIn(f"CODEX_FORK_UPSTREAM_BASELINE_SHA={BASELINE}", config)
        self.assertIn("CODEX_STABLE_TAG_PREFIX=rust-v", config)
        self.assertNotIn("CODEX_UPSTREAM_REF=refs/heads/main", config)
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
