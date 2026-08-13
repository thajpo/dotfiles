"""Presentation reconciler: locators, desired-vs-observed tmux reconciliation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.presentation import PresentationError, TmuxBackend, focus_presentation, reconcile_presentation
from scripts.pi_control.presentation_locator import build_locator, parse_locator
from tests.pi_test_build import allow_test_only_registered_build_rows
from tests.test_pi_core import _repo


class FakeTmux(TmuxBackend):
    """In-memory tmux model implementing the backend interface."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, list[dict]]] = {}
        self._pane_counter = 0
        self.home = "/nonexistent"
        self.tmux_tmpdir = None

    def _new_pane(self, session: str, window: str, *, dead: int = 0, command: str = "bash", pid: str = "0", options: dict | None = None) -> dict:
        self._pane_counter += 1
        pane = {
            "pane_id": f"%{self._pane_counter}", "session": session, "window": window,
            "pane_dead": str(dead), "command": command, "pid": pid, "title": "",
            "managed": "0", "conversation_id": "", "role": "", "argv_digest": "",
        }
        if options:
            for key, value in options.items():
                pane[key] = value
        self.sessions.setdefault(session, {}).setdefault(window, []).append(pane)
        return pane

    def has_server(self) -> bool:
        return bool(self.sessions)

    def has_session(self, session: str) -> bool:
        return session in self.sessions

    def windows(self, session: str) -> list[str]:
        return list(self.sessions.get(session, {}))

    def inventory(self) -> list[dict[str, str]]:
        rows = []
        for session, windows in self.sessions.items():
            for window, panes in windows.items():
                for pane in panes:
                    rows.append({key: str(pane[key]) for key in ("session", "window", "pane_id", "pane_dead", "command", "pid", "title", "managed", "conversation_id", "role", "argv_digest")})
        return rows

    def new_session(self, session: str, first_window: str, cwd: str) -> None:
        self.sessions.setdefault(session, {})
        self._new_pane(session, first_window)

    def new_window(self, session: str, window: str, cwd: str) -> str:
        pane = self._new_pane(session, window)
        return pane["pane_id"]

    def last_pane(self, session: str, window: str) -> str:
        panes = self.sessions.get(session, {}).get(window, [])
        return panes[-1]["pane_id"] if panes else ""

    def split_window(self, session: str, window: str, cwd: str) -> str:
        pane = self._new_pane(session, window)
        return pane["pane_id"]

    def move_pane(self, source: str, target_session: str, target_window: str) -> None:
        for session, windows in self.sessions.items():
            for window, panes in windows.items():
                for pane in panes:
                    if pane["pane_id"] == source:
                        panes.remove(pane)
                        pane["session"] = target_session
                        pane["window"] = target_window
                        self.sessions[target_session].setdefault(target_window, []).append(pane)
                        return

    def respawn_pane(self, pane: str) -> None:
        for panes in self._all_panes():
            for pane_row in panes:
                if pane_row["pane_id"] == pane:
                    pane_row["pane_dead"] = "0"
                    return

    def send_keys(self, target: str, argv: list[str]) -> None:
        pass

    def set_pane_options(self, pane: str, *, conversation_id: str, role: str, argv_digest: str) -> None:
        self._apply(pane, {"managed": "1", "conversation_id": conversation_id, "role": role, "argv_digest": argv_digest})

    def set_remain_on_exit(self, session: str, window: str) -> None:
        pass

    def set_pane_title(self, pane: str, title: str) -> None:
        self._apply(pane, {"title": title})

    def kill_pane(self, pane: str) -> None:
        for session, windows in self.sessions.items():
            for window, panes in list(windows.items()):
                for pane_row in list(panes):
                    if pane_row["pane_id"] == pane:
                        panes.remove(pane_row)
                if not panes:
                    del windows[window]

    def kill_window(self, session: str, window: str) -> None:
        self.sessions.get(session, {}).pop(window, None)

    def _all_panes(self):
        for windows in self.sessions.values():
            for panes in windows.values():
                yield panes

    def _apply(self, pane_id: str, updates: dict) -> None:
        for panes in self._all_panes():
            for pane in panes:
                if pane["pane_id"] == pane_id:
                    pane.update(updates)
                    return


class PresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = allow_test_only_registered_build_rows()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _project(self, root: Path, name: str) -> dict:
        return PiControllerClient(root / "state").register_project(str(_repo(root, name)), name)

    def _conversations(self, store: PiStore, project: dict, roles: list[str]) -> list[dict]:
        from scripts.pi_control.conversations import create_conversation
        result = []
        for index, role in enumerate(roles):
            if role == "secretary" and index == 0:
                conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=? AND role='secretary'", (project["project_id"],)).fetchone()
                result.append(dict(conversation))
                continue
            conversation = create_conversation(store, project_id=project["project_id"], role=role, display_name=f"{role}-{index}", idempotency_key=f"pres-{role}-{index}")
            result.append(dict(conversation))
        return result

    def _entry(self, conversation: dict, *, argv: list[str] | None = None, workstream_id: str | None = None, worktree_path: str | None = None) -> dict:
        return {
            "conversationId": conversation["conversation_id"],
            "role": conversation["role"],
            "title": conversation["display_name"],
            "argv": argv if argv is not None else ["pi-system-run", "--conversation-id", conversation["conversation_id"], "--interactive"],
            "workstreamId": workstream_id,
            "worktreePath": worktree_path,
        }

    def test_locator_roundtrip_and_legacy_rejection(self) -> None:
        locator = build_locator(surface="pisec", session="pisec", window="projects-1", pane="%1", project_id="prj_" + "a" * 32, conversation_id="conv_" + "b" * 32, role="secretary", layout="desktop", argv_digest="sha256:x")
        parsed = parse_locator(locator)
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["session"], "pisec")
        self.assertIsNone(parse_locator(json.dumps({"session": "pisec", "conversationId": "conv"})))
        self.assertIsNone(parse_locator({"version": 2}))
        self.assertIsNone(parse_locator(None))

    def test_fresh_desktop_grid_creates_pairs_and_persists_locators(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "grid")
            with PiStore(root / "state") as store:
                conversations = self._conversations(store, project, ["secretary", "secretary", "secretary"])
                entries = [self._entry(item) for item in conversations]
                backend = FakeTmux()
                report = reconcile_presentation(store, surface="pisec", layout="desktop", conversations=entries, backend=backend)
                self.assertEqual(report["present"], [item["conversation_id"] for item in conversations])
                self.assertEqual(sorted(backend.windows("pisec")), ["projects-1", "projects-2"])
                self.assertEqual(len(backend.sessions["pisec"]["projects-1"]), 2)
                self.assertEqual(len(backend.sessions["pisec"]["projects-2"]), 1)
                for conversation in conversations:
                    assignment = store.conn.execute("SELECT observed_state,locator_json FROM presentation_assignments WHERE conversation_id=?", (conversation["conversation_id"],)).fetchone()
                    self.assertEqual(assignment["observed_state"], "present")
                    locator = parse_locator(assignment["locator_json"])
                    self.assertIsNotNone(locator)
                    self.assertEqual(locator["conversationId"], conversation["conversation_id"])

    def test_mobile_layout_one_pane_per_window(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "mobile")
            with PiStore(root / "state") as store:
                conversations = self._conversations(store, project, ["secretary", "secretary"])
                entries = [self._entry(item) for item in conversations]
                backend = FakeTmux()
                reconcile_presentation(store, surface="pisec", layout="mobile", conversations=entries, backend=backend)
                self.assertEqual(len(backend.windows("pisec")), 2)
                for window in backend.windows("pisec"):
                    self.assertEqual(len(backend.sessions["pisec"][window]), 1)

    def test_dead_pane_is_repaired_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "repair")
            with PiStore(root / "state") as store:
                conversation = self._conversations(store, project, ["secretary"])[0]
                entry = self._entry(conversation)
                backend = FakeTmux()
                reconcile_presentation(store, surface="pisec", layout="mobile", conversations=[entry], backend=backend)
                self.assertEqual(len(backend.sessions["pisec"]["projects-1"]), 1)
                pane = backend.sessions["pisec"]["projects-1"][0]
                pane["pane_dead"] = "1"
                report = reconcile_presentation(store, surface="pisec", layout="mobile", conversations=[entry], backend=backend)
                self.assertIn(conversation["conversation_id"], report["repaired"])
                self.assertEqual(len(backend.sessions["pisec"]["projects-1"]), 1)
                self.assertEqual(backend.sessions["pisec"]["projects-1"][0]["pane_dead"], "0")

    def test_misplaced_proven_pane_is_moved(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "move")
            with PiStore(root / "state") as store:
                first, second = self._conversations(store, project, ["secretary", "secretary"])
                entries = [self._entry(first), self._entry(second)]
                backend = FakeTmux()
                reconcile_presentation(store, surface="pisec", layout="mobile", conversations=entries, backend=backend)
                second_pane = next(pane for pane in backend.sessions["pisec"]["projects-2"])
                report = reconcile_presentation(store, surface="pisec", layout="desktop", conversations=entries, backend=backend)
                self.assertIn(second["conversation_id"], report["moved"])
                self.assertEqual(second_pane["window"], "projects-1")

    def test_stale_live_pane_drifts_and_stale_dead_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "stale")
            with PiStore(root / "state") as store:
                conversation = self._conversations(store, project, ["secretary"])[0]
                entry = self._entry(conversation)
                backend = FakeTmux()
                backend.new_session("pisec", "projects-1", "/tmp")
                live = backend._new_pane("pisec", "projects-1", options={"managed": "1", "conversation_id": "conv_" + "9" * 32, "role": "secretary", "argv_digest": "sha256:live"})
                dead = backend._new_pane("pisec", "projects-1", dead=1, options={"managed": "1", "conversation_id": "conv_" + "8" * 32, "role": "secretary", "argv_digest": "sha256:dead"})
                report = reconcile_presentation(store, surface="pisec", layout="mobile", conversations=[entry], backend=backend)
                self.assertIn("conv_" + "9" * 32, report["drifted"])
                self.assertIn(live["pane_id"], [pane["pane_id"] for pane in backend.sessions["pisec"]["projects-1"]])
                self.assertNotIn(dead["pane_id"], [pane["pane_id"] for pane in backend.sessions["pisec"]["projects-1"]])

    def test_project_desktop_surface_creates_nvim_agent_windows(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "proj")
            with PiStore(root / "state") as store:
                from scripts.pi_control.pi_workstreams import create_workstream
                workstream = create_workstream(store, project_id=project["project_id"], title="feature work", idempotency_key="pres-ws")
                conversation = store.conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (workstream["conversation_id"],)).fetchone()
                entry = self._entry(dict(conversation), workstream_id=workstream["workstream_id"], worktree_path=workstream["worktree_path"])
                backend = FakeTmux()
                report = reconcile_presentation(store, surface="project", layout="desktop", conversations=[entry], backend=backend)
                session = next(iter(backend.sessions))
                self.assertIn("ws-", backend.windows(session)[0])
                self.assertEqual(len(backend.sessions[session][backend.windows(session)[0]]), 2)
                self.assertEqual(report["present"], [conversation["conversation_id"]])

    def test_focus_resolves_locator(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "focus")
            with PiStore(root / "state") as store:
                conversation = self._conversations(store, project, ["secretary"])[0]
                backend = FakeTmux()
                reconcile_presentation(store, surface="pisec", layout="mobile", conversations=[self._entry(conversation)], backend=backend)
                focus = focus_presentation(store, conversation_id=conversation["conversation_id"])
                self.assertEqual(focus["session"], "pisec")
                self.assertEqual(focus["window"], "projects-1")
                self.assertTrue(focus["pane"])

    def test_reconcile_rejects_wrong_role_and_missing_argv(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project = self._project(root, "reject")
            with PiStore(root / "state") as store:
                conversation = self._conversations(store, project, ["secretary"])[0]
                bad_role = self._entry(conversation)
                bad_role["role"] = "personal"
                with self.assertRaises(PresentationError):
                    reconcile_presentation(store, surface="pisec", layout="mobile", conversations=[bad_role], backend=FakeTmux())
                bad_argv = self._entry(conversation, argv=[])
                with self.assertRaises(PresentationError):
                    reconcile_presentation(store, surface="pisec", layout="mobile", conversations=[bad_argv], backend=FakeTmux())


if __name__ == "__main__":
    unittest.main()
