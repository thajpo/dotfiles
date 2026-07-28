import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "voxtype_mic_watchdog.py"
TMUX_STATUS_SCRIPT = Path(__file__).parents[1] / "scripts" / "tmux-voxtype-status.sh"
SPEC = importlib.util.spec_from_file_location("voxtype_mic_watchdog", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
WATCHDOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCHDOG)


class HealthTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tracker = WATCHDOG.HealthTracker(
            grace_seconds=3.0,
            min_dbfs=-55.0,
            ignore_seconds=1.0,
            required_signal_frames=2,
        )

    def test_silent_stream_fails_after_grace_period(self):
        self.assertEqual(self.tracker.update(10.0, True, -120.0)[0], "checking")
        state, reason = self.tracker.update(13.1, True, -120.0)
        self.assertEqual(state, "failed")
        self.assertEqual(
            reason,
            "No sustained microphone signal detected (maximum -120.0 dBFS)",
        )

    def test_missing_frames_are_distinguished_from_silence(self):
        self.tracker.update(10.0, True)
        state, reason = self.tracker.update(13.1, True)
        self.assertEqual(state, "failed")
        self.assertEqual(reason, "Voxtype produced no audio frames")

    def test_signal_marks_recording_healthy(self):
        self.tracker.update(10.0, True, -120.0)
        self.tracker.update(11.1, True, -30.0)
        state, _reason = self.tracker.update(11.2, True, -30.0)
        self.assertEqual(state, "ok")

    def test_failure_persists_until_next_recording(self):
        self.tracker.update(10.0, True, -120.0)
        self.tracker.update(13.1, True, -120.0)
        self.assertEqual(self.tracker.update(14.0, False)[0], "failed")
        self.assertEqual(self.tracker.update(20.0, True)[0], "checking")

    def test_late_signal_recovers_in_same_recording(self):
        self.tracker.update(10.0, True, -120.0)
        self.assertEqual(self.tracker.update(13.1, True, -120.0)[0], "failed")
        self.tracker.update(14.0, True, -20.0)
        self.assertEqual(self.tracker.update(14.1, True, -20.0)[0], "ok")

    def test_startup_beep_does_not_confirm_microphone(self):
        self.tracker.update(10.0, True, -10.0)
        self.tracker.update(10.5, True, -10.0)
        state, _reason = self.tracker.update(13.1, True, -120.0)
        self.assertEqual(state, "failed")

    def test_confirmed_signal_stays_healthy_after_grace_period(self):
        self.tracker.update(10.0, True)
        self.tracker.update(11.1, True, -20.0)
        self.assertEqual(self.tracker.update(11.2, True, -20.0)[0], "ok")
        self.assertEqual(self.tracker.update(14.0, True, -120.0)[0], "ok")


class WatchdogIntegrationTest(unittest.TestCase):
    def test_silent_socket_warns_and_later_signal_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            socket_path = root / "audio.sock"
            state_file = root / "state"
            health_file = root / "mic-health"
            state_file.write_text("idle\n", encoding="utf-8")

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(1)
            server.settimeout(2.0)

            process = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--socket",
                    str(socket_path),
                    "--state-file",
                    str(state_file),
                    "--health-file",
                    str(health_file),
                    "--grace-seconds",
                    "0.15",
                    "--ignore-seconds",
                    "0",
                    "--required-signal-frames",
                    "1",
                    "--no-alerts",
                ]
            )
            try:
                connection, _address = server.accept()
                with connection:
                    state_file.write_text("recording\n", encoding="utf-8")
                    silent_frame = WATCHDOG.FRAME.pack(1, 0.0, 0.0, -120.0)
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        connection.sendall(silent_frame)
                        if self._health_state(health_file) == "failed":
                            break
                        time.sleep(0.02)
                    self.assertEqual(self._health_state(health_file), "failed")

                    signal_frame = WATCHDOG.FRAME.pack(2, -0.2, 0.2, -14.0)
                    connection.sendall(signal_frame)
                    self.assertTrue(self._wait_for_state(health_file, "ok"))

                    state_file.write_text("idle\n", encoding="utf-8")
                    self.assertTrue(self._wait_for_state(health_file, "ok"))
            finally:
                process.terminate()
                process.wait(timeout=2.0)
                server.close()

    @staticmethod
    def _health_state(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").split("\t", 1)[0]
        except OSError:
            return ""

    def _wait_for_state(self, path: Path, expected: str) -> bool:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self._health_state(path) == expected:
                return True
            time.sleep(0.02)
        return False


class TmuxStatusIntegrationTest(unittest.TestCase):
    def test_status_distinguishes_checking_healthy_and_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            voxtype = runtime / "voxtype"
            voxtype.mkdir()
            (voxtype / "state").write_text("recording\n", encoding="utf-8")

            self.assertEqual(self._render(runtime, "checking"), "🎤 CHECK")
            self.assertEqual(self._render(runtime, "ok"), "🎤 REC")
            self.assertEqual(self._render(runtime, "failed"), "⚠ MIC")

    @staticmethod
    def _render(runtime: Path, health: str) -> str:
        (runtime / "voxtype" / "mic-health").write_text(
            f"{health}\ttest\n", encoding="utf-8"
        )
        environment = os.environ.copy()
        environment["XDG_RUNTIME_DIR"] = str(runtime)
        return subprocess.check_output(
            [str(TMUX_STATUS_SCRIPT)], env=environment, text=True
        )


if __name__ == "__main__":
    unittest.main()
