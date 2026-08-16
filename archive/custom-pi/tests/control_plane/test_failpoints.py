"""Phase 1 deterministic fixture, clock, adapter, and failpoint coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import unittest

try:  # Package import for ``python -m unittest tests.control_plane...``.
    from .fake_adapters import (
        FakeEventEmitter,
        FakePresentationAdapter,
        FakeProcessAdapter,
        FakeRuntimeAdapter,
    )
    from .fake_clock import (
        FakeClock,
        MonotonicOriginMismatch,
        format_rfc3339_utc,
        parse_rfc3339_utc,
    )
    from .helpers import (
        REGISTERED_FAILPOINT_BASE_NAMES,
        REGISTERED_FAILPOINT_NAMES,
        ConfiguredFailpointController,
        DisposableEnvironment,
        FailpointController,
        FailpointRaised,
        GitObservationError,
        GitSafetyError,
        NoOpFailpointController,
        assert_snapshot_unchanged,
        default_failpoint_controller,
        run_failpoint_child,
        run_failpoint_then_recover,
        run_fresh_process,
        safe_temporary_directory,
        sanitize_failpoint_context,
        sanitize_git_environment,
        git_run,
        snapshot_filesystem,
        snapshot_git_repository,
        snapshot_sqlite_rows,
        supports_git_sha256,
        _target_spec,
    )
except ImportError:  # Top-level import used by unittest discovery below a directory.
    from fake_adapters import (
        FakeEventEmitter,
        FakePresentationAdapter,
        FakeProcessAdapter,
        FakeRuntimeAdapter,
    )
    from fake_clock import (
        FakeClock,
        MonotonicOriginMismatch,
        format_rfc3339_utc,
        parse_rfc3339_utc,
    )
    from helpers import (
        REGISTERED_FAILPOINT_BASE_NAMES,
        REGISTERED_FAILPOINT_NAMES,
        ConfiguredFailpointController,
        DisposableEnvironment,
        FailpointController,
        FailpointRaised,
        GitObservationError,
        GitSafetyError,
        NoOpFailpointController,
        assert_snapshot_unchanged,
        default_failpoint_controller,
        run_failpoint_child,
        run_failpoint_then_recover,
        run_fresh_process,
        safe_temporary_directory,
        sanitize_failpoint_context,
        sanitize_git_environment,
        git_run,
        snapshot_filesystem,
        snapshot_git_repository,
        snapshot_sqlite_rows,
        supports_git_sha256,
        _target_spec,
    )


# These targets are intentionally module-level so the child helper can import
# them in a fresh interpreter without serializing arbitrary callables.
def _child_side_effect(controller: FailpointController, payload: dict[str, str]) -> None:
    marker = Path(payload["marker"])
    marker.write_text("before\n", encoding="utf-8")
    controller.hit("manifest.write.before", {"marker": str(marker)})
    marker.write_text("after\n", encoding="utf-8")


def _child_reconcile(_controller: FailpointController, payload: dict[str, str]) -> dict[str, str]:
    marker = Path(payload["marker"])
    return {"marker": marker.read_text(encoding="utf-8").strip()}


def _child_large_output(_controller: FailpointController, payload: dict[str, int]) -> None:
    print("x" * int(payload["size"]))


def _child_environment_report(_controller: FailpointController, _payload: object) -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in ("HOME", "TMPDIR", "XDG_STATE_HOME", "XDG_RUNTIME_DIR")
    }


def _child_process_group_report(_controller: FailpointController, _payload: object) -> dict[str, int]:
    return {"pid": os.getpid(), "process_group": os.getpgrp()}


def _child_fork_descendant(_controller: FailpointController, payload: dict[str, str]) -> dict[str, int]:
    child_pid = os.fork()
    if child_pid == 0:
        time.sleep(3.0)
        Path(payload["marker"]).write_text("descendant-survived\n", encoding="utf-8")
        os._exit(0)
    return {"child_pid": child_pid}


def _child_setsid_descendant(_controller: FailpointController, payload: dict[str, str]) -> dict[str, int]:
    child_pid = os.fork()
    if child_pid == 0:
        os.setsid()
        time.sleep(3.0)
        Path(payload["marker"]).write_text("session-descendant-survived\n", encoding="utf-8")
        os._exit(0)
    time.sleep(0.5)
    return {"child_pid": child_pid}


def _child_failpoint_fork_descendant(
    controller: FailpointController,
    payload: dict[str, str],
) -> None:
    child_pid = os.fork()
    if child_pid == 0:
        os.setsid()
        time.sleep(3.0)
        Path(payload["marker"]).write_text("failpoint-descendant-survived\n", encoding="utf-8")
        os._exit(0)
    controller.hit("manifest.write.before", {})


def _child_fixture_git_status(_controller: FailpointController, payload: dict[str, str]) -> dict[str, int]:
    result = git_run(
        ["status", "--short"],
        cwd=Path(payload["cwd"]),
        fixture_root=Path(payload["root"]),
    )
    return {"returncode": result.returncode}


class FailpointContractTests(unittest.TestCase):
    def test_registered_names_have_before_after_boundaries(self):
        self.assertEqual(len(REGISTERED_FAILPOINT_NAMES), 24)
        self.assertEqual(
            set(REGISTERED_FAILPOINT_NAMES),
            {
                f"{base}.{phase}"
                for base in REGISTERED_FAILPOINT_BASE_NAMES
                for phase in ("before", "after")
            },
        )
        self.assertIn("integration.target_ref.before", REGISTERED_FAILPOINT_NAMES)
        self.assertIn("event.commit.after", REGISTERED_FAILPOINT_NAMES)

    def test_default_construction_is_noop_and_not_environment_controlled(self):
        controller = default_failpoint_controller()
        self.assertIs(controller, default_failpoint_controller())
        self.assertIsInstance(controller, FailpointController)
        controller.hit("operation.intent.before", {"secret": "must not matter"})
        self.assertIsInstance(NoOpFailpointController(), FailpointController)

        # A similarly named environment variable cannot enable a failpoint.
        old_value = os.environ.get("PI_CONTROL_FAILPOINT")
        try:
            os.environ["PI_CONTROL_FAILPOINT"] = "event.commit.before"
            default_failpoint_controller().hit("event.commit.before", {})
        finally:
            if old_value is None:
                os.environ.pop("PI_CONTROL_FAILPOINT", None)
            else:
                os.environ["PI_CONTROL_FAILPOINT"] = old_value

    def test_configured_failpoint_requires_exactly_one_action(self):
        with self.assertRaises(ValueError):
            ConfiguredFailpointController("event.commit.before")
        with self.assertRaises(ValueError):
            ConfiguredFailpointController(
                "event.commit.before",
                exit_code=91,
                callback=lambda *_: None,
            )
        with self.assertRaises(ValueError):
            ConfiguredFailpointController("event.commit.before", exit_code=256)

    def test_raise_failpoint_fires_once_and_sanitizes_bounded_context(self):
        controller = ConfiguredFailpointController(
            "operation.intent.before",
            raise_exception=FailpointRaised,
        )
        context = {
            "capability_token": "secret-value",
            "large": "x" * 1000,
            "operation": "fixture",
        }
        with self.assertRaises(FailpointRaised) as raised:
            controller.hit("operation.intent.before", context)
        self.assertEqual(raised.exception.name, "operation.intent.before")
        self.assertEqual(raised.exception.context["capability_token"], "[redacted]")
        self.assertLessEqual(len(raised.exception.context["large"]), 192)
        controller.hit("operation.intent.before", context)
        controller.hit("event.commit.before", {})
        self.assertTrue(controller.fired)
        self.assertEqual(controller.hit_count, 2)
        self.assertEqual(len(controller.hits), 3)
        self.assertEqual(context["large"], "x" * 1000)

    def test_callback_failpoint_fires_only_at_selected_name_once(self):
        calls: list[tuple[str, dict[str, str]]] = []

        def callback(name: str, context: dict[str, str]) -> None:
            calls.append((name, dict(context)))

        controller = ConfiguredFailpointController(
            "runtime.stop.after",
            callback=callback,
        )
        controller.hit("runtime.stop.before", {"state": "running"})
        controller.hit("runtime.stop.after", {"state": "stopped"})
        controller.hit("runtime.stop.after", {"state": "stopped-again"})
        self.assertEqual(calls, [("runtime.stop.after", {"state": "stopped"})])
        self.assertEqual(controller.hit_count, 2)

    def test_context_helper_is_bounded_and_does_not_mutate_input(self):
        source = {"z": "last", "a": "first", "private_key": "secret", "blob": b"abc"}
        result = sanitize_failpoint_context(source, max_items=3, max_value_length=4)
        self.assertEqual(list(result), ["a", "blob", "private_key"])
        self.assertEqual(result["private_key"], "[red")
        self.assertEqual(result["blob"], "YWJj")
        self.assertEqual(source["private_key"], "secret")


class GitEnvironmentTests(unittest.TestCase):
    def test_sanitizer_copies_mapping_and_blocks_path_config_hook_pager_editor_diff_prompt(self):
        source = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": "/tmp/attacker-home",
            "GIT_DIR": "/tmp/attacker-git",
            "GIT_WORK_TREE": "/tmp/attacker-tree",
            "GIT_COMMON_DIR": "/tmp/attacker-common",
            "GIT_INDEX_FILE": "/tmp/attacker-index",
            "GIT_OBJECT_DIRECTORY": "/tmp/attacker-objects",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/tmp/attacker-alternates",
            "GIT_TEMPLATE_DIR": "/tmp/attacker-template",
            "GIT_EXEC_PATH": "/tmp/attacker-exec",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/tmp/attacker-hooks",
            "GIT_CONFIG_GLOBAL": "/tmp/attacker-config",
            "GIT_CONFIG_PARAMETERS": "'core.pager=attacker'",
            "GIT_PAGER": "/tmp/attacker-pager",
            "PAGER": "/tmp/attacker-pager",
            "GIT_EDITOR": "/tmp/attacker-editor",
            "GIT_SEQUENCE_EDITOR": "/tmp/attacker-editor",
            "EDITOR": "/tmp/attacker-editor",
            "GIT_EXTERNAL_DIFF": "/tmp/attacker-diff",
            "GIT_DIFF_OPTS": "--no-index",
            "GIT_TERMINAL_PROMPT": "1",
            "GIT_ASKPASS": "/tmp/attacker-askpass",
            "GIT_SSH_COMMAND": "attacker ssh",
            "LD_PRELOAD": "/tmp/attacker.so",
            "LD_LIBRARY_PATH": "/tmp/attacker-libs",
            "DYLD_INSERT_LIBRARIES": "/tmp/attacker.dylib",
            "PYTHONINSPECT": "1",
            "XDG_CONFIG_HOME": "/tmp/attacker-config-home",
            "ATTACKER_RANDOM": "must be dropped",
        }
        original = dict(source)
        clean = sanitize_git_environment(source)
        self.assertEqual(source, original)
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_TEMPLATE_DIR",
            "GIT_EXEC_PATH",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_EXTERNAL_DIFF",
            "GIT_SSH_COMMAND",
        ):
            self.assertNotIn(key, clean)
        self.assertEqual(clean["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(clean["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(clean["GIT_PAGER"], "cat")
        self.assertEqual(clean["GIT_EDITOR"], "true")
        self.assertEqual(clean["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(clean["PATH"], os.defpath)
        self.assertEqual(clean["LC_ALL"], "C")
        for key in (
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONINSPECT",
            "XDG_CONFIG_HOME",
            "ATTACKER_RANDOM",
        ):
            self.assertNotIn(key, clean)

    def test_fixture_git_commands_ignore_attacker_global_config_and_hooks(self):
        with safe_temporary_directory(prefix="pi-control-git-injection-") as raw_root:
            root = Path(raw_root)
            attacker_home = root / "attacker-home"
            attacker_home.mkdir()
            attacker_config = attacker_home / ".gitconfig"
            attacker_config.write_text(
                "[core]\n\thooksPath = /tmp/attacker-hooks\n\tpager = /tmp/attacker-pager\n",
                encoding="utf-8",
            )
            source = {
                **os.environ,
                "HOME": str(attacker_home),
                "GIT_CONFIG_GLOBAL": str(attacker_config),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.hooksPath",
                "GIT_CONFIG_VALUE_0": "/tmp/attacker-hooks",
            }
            with DisposableEnvironment(repository_under_test=Path.cwd(), capture_host_state=True) as fixture:
                result = git_run(
                    ["config", "--get", "core.hooksPath"],
                    cwd=fixture.repo,
                    environ={**source, "HOME": str(attacker_home)},
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), os.devnull)
                fixture.assert_untouched()

    def test_git_run_rejects_local_execution_surfaces_before_git_runs(self):
        with DisposableEnvironment(repository_under_test=Path.cwd(), capture_host_state=True) as fixture:
            marker = fixture.root / "must-not-run"
            hook_dir = fixture.root / "hooks"
            hook_dir.mkdir()
            hook = hook_dir / "pre-commit"
            hook.write_text(
                f"#!/bin/sh\nprintf hook > {marker}\n",
                encoding="utf-8",
            )
            hook.chmod(0o700)
            external_diff = fixture.root / "external-diff"
            external_diff.write_text(
                f"#!/bin/sh\nprintf diff > {marker}\n",
                encoding="utf-8",
            )
            external_diff.chmod(0o700)
            config = fixture.repo / ".git" / "config"
            with config.open("a", encoding="utf-8") as stream:
                stream.write(
                    "\n[core]\n"
                    f"\thooksPath = {hook_dir}\n"
                    f"\tfsmonitor = {external_diff}\n"
                    f"\teditor = {external_diff}\n"
                    "[diff]\n"
                    f"\texternal = {external_diff}\n"
                    "[diff \"foo\"]\n"
                    f"\ttextconv = {external_diff}\n"
                    "[filter \"foo\"]\n"
                    f"\tprocess = {external_diff}\n"
                    "[credential \"foo\"]\n"
                    f"\thelper = {external_diff}\n"
                )

            with self.assertRaises(GitSafetyError):
                git_run(["status", "--short"], cwd=fixture.repo)
            self.assertFalse(marker.exists())
            fixture.assert_untouched()

    def test_git_rejects_symlink_worktree_and_worktree_config_redirects(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            config = fixture.repo / ".git" / "config"
            original_config = config.read_text(encoding="utf-8")
            with config.open("a", encoding="utf-8") as stream:
                stream.write("\n[core]\n\tworktree = /etc\n")
            with self.assertRaises(GitSafetyError):
                git_run(["status", "--short"], cwd=fixture.repo, fixture_root=fixture.root)
            config.write_text(original_config, encoding="utf-8")

            linked_git_file = fixture.linked_worktree / ".git"
            original_linked_git_file = linked_git_file.read_text(encoding="utf-8")
            redirect = fixture.root / "gitdir-link"
            redirect.symlink_to("/tmp", target_is_directory=True)
            linked_git_file.write_text(f"gitdir: {redirect}\n", encoding="utf-8")
            with self.assertRaises(GitSafetyError):
                git_run(
                    ["status", "--short"],
                    cwd=fixture.linked_worktree,
                    fixture_root=fixture.root,
                )
            linked_git_file.write_text(original_linked_git_file, encoding="utf-8")
            linked_gitdir = Path(
                next(
                    line.split(":", 1)[1].strip()
                    for line in linked_git_file.read_text(encoding="utf-8").splitlines()
                    if line.lower().startswith("gitdir:")
                )
            )
            commondir_file = linked_gitdir / "commondir"
            original_commondir = commondir_file.read_text(encoding="utf-8")
            commondir_file.write_text("/tmp\n", encoding="utf-8")
            with self.assertRaises(GitSafetyError):
                git_run(
                    ["status", "--short"],
                    cwd=fixture.linked_worktree,
                    fixture_root=fixture.root,
                )
            commondir_file.write_text(original_commondir, encoding="utf-8")

            worktree_config = linked_gitdir / "config.worktree"
            worktree_config.write_text("[core]\n\tworktree = /etc\n", encoding="utf-8")
            with self.assertRaises(GitSafetyError):
                git_run(
                    ["status", "--short"],
                    cwd=fixture.linked_worktree,
                    fixture_root=fixture.root,
                )

            bare_config = fixture.remote / "config"
            with bare_config.open("a", encoding="utf-8") as stream:
                stream.write("\n[core]\n\tworktree = /etc\n")
            with self.assertRaises(GitSafetyError):
                git_run(
                    ["status", "--short"],
                    cwd=fixture.remote,
                    fixture_root=fixture.root,
                )

            real_git = fixture.repo / ".git"
            moved_git = fixture.repo / ".git-real"
            real_git.rename(moved_git)
            external_git = fixture.root / "external-git"
            external_git.mkdir()
            real_git.symlink_to(external_git, target_is_directory=True)
            with self.assertRaises(GitSafetyError):
                git_run(["status", "--short"], cwd=fixture.repo, fixture_root=fixture.root)

    def test_nested_git_cwd_inherits_parent_config_safety(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            nested = fixture.repo / "nested"
            nested.mkdir()
            config = fixture.repo / ".git" / "config"
            original = config.read_text(encoding="utf-8")
            config.write_text(original + "\n[core]\n\tworktree = /etc\n", encoding="utf-8")
            try:
                with self.assertRaises(GitSafetyError):
                    git_run(
                        ["status", "--short"],
                        cwd=nested,
                        fixture_root=fixture.root,
                    )
            finally:
                config.write_text(original, encoding="utf-8")

    def test_git_mutations_require_a_disposable_fixture_root(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            for args in (
                ["config", "--local", "user.name", "must-not-write"],
                ["commit", "--allow-empty", "-m", "must-not-write"],
                ["init"],
                ["symbolic-ref", "HEAD", "refs/heads/main"],
            ):
                with self.assertRaises(GitSafetyError):
                    git_run(args, cwd=fixture.repo)

    def test_git_wrapper_rejects_shell_remote_alias_and_unbounded_calls(self):
        with DisposableEnvironment(repository_under_test=Path.cwd(), capture_host_state=True) as fixture:
            with self.assertRaises(TypeError):
                git_run(["status"], cwd=fixture.repo, shell=True)  # type: ignore[call-arg]
            with self.assertRaises(ValueError):
                git_run(["push"], cwd=fixture.repo)
            with self.assertRaises(ValueError):
                git_run(["local-alias"], cwd=fixture.repo)
            with self.assertRaises(GitSafetyError):
                git_run(["config", "--file", "/etc/gitconfig", "user.name"], cwd=fixture.repo)
            with self.assertRaises(GitSafetyError):
                git_run(["add", "../outside"], cwd=fixture.repo)
            with self.assertRaises(GitSafetyError):
                git_run(
                    ["init", "/etc/pi-control-fixture"],
                    cwd=fixture.repo,
                    fixture_root=fixture.root,
                )
            with self.assertRaises(GitSafetyError):
                git_run(
                    ["worktree", "add", "-b", "escape", "--force", "/etc", "main"],
                    cwd=fixture.repo,
                    fixture_root=fixture.root,
                )
            with self.assertRaises(ValueError):
                git_run(["status", "--short"], cwd=fixture.repo, timeout=None)  # type: ignore[arg-type]
            fixture.assert_untouched()

    def test_git_snapshot_failure_is_explicit(self):
        with safe_temporary_directory(prefix="pi-control-not-git-") as raw_root:
            with self.assertRaises(GitObservationError):
                snapshot_git_repository(Path(raw_root))

    def test_safe_temp_prefix_cannot_escape_fixed_parent(self):
        with self.assertRaises(ValueError):
            safe_temporary_directory(prefix="../escape-")
        with self.assertRaises(ValueError):
            safe_temporary_directory(prefix="/tmp/escape-")


class FixtureAndSnapshotTests(unittest.TestCase):
    def test_disposable_environment_has_isolated_home_xdg_git_worktree_sessions_and_adapters(self):
        with DisposableEnvironment(repository_under_test=Path.cwd(), capture_host_state=True) as fixture:
            fixture.assert_isolated()
            self.assertNotEqual(fixture.home.resolve(), Path.home().resolve())
            self.assertEqual(fixture.environment["HOME"], str(fixture.home))
            self.assertEqual(fixture.environment["XDG_STATE_HOME"], str(fixture.state_home))
            self.assertEqual(fixture.environment["XDG_RUNTIME_DIR"], str(fixture.runtime_dir))
            self.assertEqual(fixture.environment["TMPDIR"], str(fixture.temp_dir))
            self.assertEqual(fixture.environment["TMP"], str(fixture.temp_dir))
            self.assertEqual(fixture.environment["TEMP"], str(fixture.temp_dir))
            self.assertTrue((fixture.repo / ".git").is_dir())
            self.assertTrue(fixture.remote.is_dir())
            self.assertTrue(fixture.linked_worktree.is_dir())
            self.assertEqual(
                snapshot_git_repository(fixture.repo)["object_format"], "sha1"
            )
            worktrees = snapshot_git_repository(fixture.repo)["worktrees"]
            self.assertEqual(len(worktrees), 2)
            self.assertEqual(
                {Path(str(item["worktree"])).resolve() for item in worktrees},
                {fixture.repo.resolve(), fixture.linked_worktree.resolve()},
            )
            self.assertEqual(len(fixture.session_path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertTrue(stat.S_IMODE(fixture.runtime_dir.stat().st_mode) & 0o077 == 0)
            self.assertIsNotNone(fixture.process_adapter)
            self.assertIsNotNone(fixture.runtime_adapter)
            self.assertIsNotNone(fixture.presentation_adapter)
            self.assertIsNotNone(fixture.event_emitter)
            fixture.assert_untouched()

    def test_optional_sha256_setup_is_capability_detected(self):
        if not supports_git_sha256():
            self.skipTest("installed Git has no SHA-256 object format support")
        with DisposableEnvironment(
            repository_under_test=Path.cwd(),
            include_sha256=True,
            capture_host_state=True,
        ) as fixture:
            self.assertIsNotNone(fixture.sha256_repo)
            assert fixture.sha256_repo is not None
            self.assertEqual(
                snapshot_git_repository(fixture.sha256_repo)["object_format"], "sha256"
            )
            fixture.assert_untouched()

    def test_filesystem_snapshot_records_permissions_inodes_and_content(self):
        with safe_temporary_directory(prefix="pi-control-filesystem-") as raw_root:
            root = Path(raw_root)
            target = root / "target.txt"
            target.write_text("one\n", encoding="utf-8")
            before = snapshot_filesystem(root)
            original_inode = before["target.txt"]["inode"]
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
            changed_permissions = snapshot_filesystem(root)
            self.assertNotEqual(
                before["target.txt"]["mode"], changed_permissions["target.txt"]["mode"]
            )
            self.assertEqual(original_inode, changed_permissions["target.txt"]["inode"])
            replacement = root / "replacement.txt"
            replacement.write_text("two\n", encoding="utf-8")
            os.replace(replacement, target)
            replaced = snapshot_filesystem(root)
            self.assertNotEqual(original_inode, replaced["target.txt"]["inode"])
            self.assertNotEqual(before["target.txt"]["sha256"], replaced["target.txt"]["sha256"])

    def test_sqlite_rows_and_git_snapshots_are_deterministic(self):
        with safe_temporary_directory(prefix="pi-control-snapshots-") as raw_root:
            root = Path(raw_root)
            database = root / "state.db"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE rows (name TEXT, value BLOB)")
            connection.executemany(
                "INSERT INTO rows VALUES (?, ?)",
                [("b", b"two"), ("a", b"one")],
            )
            connection.commit()
            self.assertEqual(
                snapshot_sqlite_rows(connection),
                {
                    "rows": [
                        {"name": "a", "value": {"__bytes__": "b25l"}},
                        {"name": "b", "value": {"__bytes__": "dHdv"}},
                    ]
                },
            )
            connection.close()
            self.assertEqual(snapshot_sqlite_rows(database)["rows"][0]["name"], "a")

        with DisposableEnvironment(repository_under_test=Path.cwd(), capture_host_state=True) as fixture:
            git_before = snapshot_git_repository(fixture.repo)
            (fixture.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            git_after = snapshot_git_repository(fixture.repo)
            self.assertEqual(git_before["head_oid"], git_after["head_oid"])
            self.assertNotEqual(git_before["status"], git_after["status"])
            self.assertGreaterEqual(len(git_after["refs"]), 2)
            self.assertNotIn("refs/remotes/origin/main", git_after["refs"])
            self.assertIn("refs/heads/main", git_after["refs"])
            self.assertIn("refs/heads/fixture-linked", git_after["refs"])
            fixture.assert_untouched()

    def test_fake_runtime_and_events_have_observable_snapshots(self):
        runtime_controller = ConfiguredFailpointController(
            "runtime.create.after",
            callback=lambda _name, _context: None,
        )
        runtime = FakeRuntimeAdapter(failpoints=runtime_controller)
        created = runtime.create(runtime_id="runtime-1", writable=True)
        self.assertEqual(created.state, "running")
        self.assertTrue(runtime.snapshot()["runtime-1"]["writable"])
        runtime.stop("runtime-1")
        self.assertEqual(runtime.snapshot()["runtime-1"]["state"], "stopped")

        event_controller = ConfiguredFailpointController(
            "event.commit.after",
            callback=lambda _name, _context: None,
        )
        events = FakeEventEmitter(failpoints=event_controller)
        events.emit("operation.completed", {"resource": "fixture", "nested": {"value": 1}})
        snapshot = events.snapshot()
        snapshot[0]["payload"]["resource"] = "caller mutation"
        self.assertEqual(events.snapshot()[0]["payload"]["resource"], "fixture")
        returned_event = events.events[0]
        returned_event.payload["nested"]["value"] = 2
        self.assertEqual(events.snapshot()[0]["payload"]["nested"]["value"], 1)

        process = FakeProcessAdapter().start(["fake-tool"], metadata={"owner": "test"})
        with self.assertRaises(TypeError):
            process.metadata["owner"] = "mutated"  # type: ignore[index]
        presentation = FakePresentationAdapter().create(metadata={"owner": "test"})
        with self.assertRaises(TypeError):
            presentation.metadata["owner"] = "mutated"  # type: ignore[index]


class ClockTests(unittest.TestCase):
    def test_rfc3339_utc_is_deterministic(self):
        clock = FakeClock(start="2024-02-03T04:05:06Z")
        self.assertEqual(clock.rfc3339(), "2024-02-03T04:05:06.000000Z")
        clock.advance(1.25)
        self.assertEqual(clock.rfc3339(), "2024-02-03T04:05:07.250000Z")
        self.assertEqual(format_rfc3339_utc(clock.now()), clock.rfc3339())
        self.assertEqual(parse_rfc3339_utc("2024-02-03T05:05:07.25+01:00").tzinfo, clock.now().tzinfo)

    def test_monotonic_origin_reset_rejects_cross_process_comparisons(self):
        clock = FakeClock()
        with self.assertRaises(ValueError):
            FakeClock(origin=clock.origin)
        first = clock.monotonic()
        clock.advance(2)
        second = clock.monotonic()
        self.assertLess(first, second)
        self.assertEqual(second - first, 2_000_000_000)

        fresh = clock.new_process(origin="fresh-process")
        other = fresh.monotonic()
        second_fresh = clock.new_process()
        second_other = second_fresh.monotonic()
        self.assertNotEqual(fresh.origin, second_fresh.origin)
        with self.assertRaises(ValueError):
            clock.new_process(origin="fresh-process")
        with self.assertRaises(MonotonicOriginMismatch):
            _ = first < other
        with self.assertRaises(MonotonicOriginMismatch):
            _ = first == other
        with self.assertRaises(MonotonicOriginMismatch):
            _ = first - other
        with self.assertRaises(MonotonicOriginMismatch):
            _ = other == second_other
        clock.reset_monotonic(origin="after-reset")
        with self.assertRaises(MonotonicOriginMismatch):
            _ = second > clock.monotonic()

    def test_invalid_clock_inputs_do_not_reserve_or_mutate_origins(self):
        with self.assertRaises(ValueError):
            FakeClock(origin="constructor-retry", monotonic_start=-1)
        retry = FakeClock(origin="constructor-retry")
        self.assertEqual(retry.origin, "constructor-retry")

        clock = FakeClock()
        original_origin = clock.origin
        with self.assertRaises(ValueError):
            clock.reset_monotonic(origin="reset-retry", seconds=-1)
        self.assertEqual(clock.origin, original_origin)
        clock.reset_monotonic(origin="reset-retry")
        self.assertEqual(clock.origin, "reset-retry")

        generated = FakeClock()
        generated_base = generated.origin
        with self.assertRaises(ValueError):
            generated.reset_monotonic(seconds=-1)
        generated.reset_monotonic()
        self.assertEqual(generated.origin, f"{generated_base}#1")


class ChildProcessTests(unittest.TestCase):
    def test_child_failpoint_exit_and_fresh_process_recovery(self):
        with safe_temporary_directory(prefix="pi-control-child-") as raw_root:
            marker = Path(raw_root) / "marker.txt"
            payload = {"marker": str(marker)}
            result = run_failpoint_child(
                _child_side_effect,
                "manifest.write.before",
                exit_code=83,
                payload=payload,
                cwd=Path(raw_root),
                fixture_root=raw_root,
            )
            self.assertEqual(result.returncode, 83)
            self.assertEqual(marker.read_text(encoding="utf-8"), "before\n")
            recovery = run_fresh_process(
                _child_reconcile,
                payload=payload,
                cwd=Path(raw_root),
                fixture_root=raw_root,
            )
            self.assertEqual(recovery.returncode, 0)
            self.assertEqual(json.loads(recovery.stdout), {"marker": "before"})

    def test_combined_child_termination_and_recovery_has_no_pid_kill_surface(self):
        with safe_temporary_directory(prefix="pi-control-child-recover-") as raw_root:
            payload = {"marker": str(Path(raw_root) / "marker.txt")}
            result = run_failpoint_then_recover(
                _child_side_effect,
                _child_reconcile,
                "manifest.write.before",
                exit_code=84,
                payload=payload,
                recovery_payload=payload,
                cwd=Path(raw_root),
                fixture_root=raw_root,
            )
            self.assertEqual(result.terminated.returncode, 84)
            self.assertEqual(result.recovered.returncode, 0)
            self.assertEqual(json.loads(result.recovered.stdout)["marker"], "before")

    def test_string_child_target_is_not_imported_in_parent(self):
        with safe_temporary_directory(prefix="pi-control-import-") as raw_root:
            root = Path(raw_root)
            marker = root / "imported.txt"
            module_name = "phase1_import_side_effect"
            (root / f"{module_name}.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
                "def target(_controller, _payload):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            spec = f"{module_name}:target"
            self.assertEqual(_target_spec(spec), spec)
            self.assertFalse(marker.exists())
            result = run_fresh_process(
                spec,
                payload={},
                environ={"PYTHONPATH": str(root)},
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue(marker.exists())
            self.assertEqual(json.loads(result.stdout), {"ok": True})

    def test_child_output_has_transport_bound(self):
        result = run_fresh_process(
            _child_large_output,
            payload={"size": 1024 * 1024},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 256 * 1024)
        self.assertIn("transport bound", result.stderr)

    def test_child_defaults_to_disposable_home_and_tmp(self):
        result = run_fresh_process(
            _child_environment_report,
            payload={},
        )
        self.assertEqual(result.returncode, 0)
        values = json.loads(result.stdout)
        self.assertTrue(values["HOME"].startswith("/tmp/pi-control-child-sandbox-"))
        self.assertTrue(values["TMPDIR"].startswith("/tmp/pi-control-child-sandbox-"))
        self.assertTrue(values["XDG_STATE_HOME"].startswith("/tmp/pi-control-child-sandbox-"))
        self.assertTrue(values["XDG_RUNTIME_DIR"].startswith("/tmp/pi-control-child-sandbox-"))
        self.assertNotEqual(Path(values["HOME"]).resolve(), Path.home().resolve())

    def test_child_runs_in_a_private_process_group(self):
        result = run_fresh_process(
            _child_process_group_report,
            payload={},
        )
        self.assertEqual(result.returncode, 0)
        values = json.loads(result.stdout)
        self.assertEqual(values["pid"], values["process_group"])

    def test_child_rejects_host_cwd_without_disposable_fixture_root(self):
        with self.assertRaises(GitSafetyError):
            run_fresh_process(_child_environment_report, cwd=Path.cwd())

    def test_fresh_child_receives_registered_fixture_root_authorization(self):
        with DisposableEnvironment(repository_under_test=Path.cwd()) as fixture:
            result = run_fresh_process(
                _child_fixture_git_status,
                payload={"cwd": str(fixture.repo), "root": str(fixture.root)},
                cwd=fixture.repo,
                fixture_root=fixture.root,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout), {"returncode": 0})

    def test_descendants_cannot_survive_helper_process_exit(self):
        with safe_temporary_directory(prefix="pi-control-descendant-") as raw_root:
            marker = Path(raw_root) / "descendant.txt"
            result = run_fresh_process(
                _child_fork_descendant,
                payload={"marker": str(marker)},
            )
            self.assertEqual(result.returncode, 0)
            time.sleep(3.1)
            self.assertFalse(marker.exists())

    def test_session_escaping_descendants_are_terminated(self):
        with safe_temporary_directory(prefix="pi-control-session-descendant-") as raw_root:
            marker = Path(raw_root) / "descendant.txt"
            result = run_fresh_process(
                _child_setsid_descendant,
                payload={"marker": str(marker)},
            )
            self.assertEqual(result.returncode, 0)
            time.sleep(3.1)
            self.assertFalse(marker.exists())

    def test_failpoint_exit_still_tracks_immediate_session_descendants(self):
        with safe_temporary_directory(prefix="pi-control-failpoint-descendant-") as raw_root:
            marker = Path(raw_root) / "descendant.txt"
            result = run_failpoint_child(
                _child_failpoint_fork_descendant,
                "manifest.write.before",
                exit_code=88,
                payload={"marker": str(marker)},
            )
            self.assertEqual(result.returncode, 88)
            time.sleep(3.1)
            self.assertFalse(marker.exists())


class HostIsolationTests(unittest.TestCase):
    def test_fixture_and_real_home_repository_are_untouched(self):
        with DisposableEnvironment(repository_under_test=Path.cwd(), capture_host_state=True) as fixture:
            fixture.assert_isolated()
            fixture.assert_untouched()
            # Explicitly exercise the standalone assertion helper as well.
            before = snapshot_filesystem(fixture.real_home)
            assert_snapshot_unchanged(fixture.real_home, before, label="real HOME")


if __name__ == "__main__":
    unittest.main()
