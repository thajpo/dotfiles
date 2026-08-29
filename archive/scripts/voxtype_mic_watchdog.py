#!/usr/bin/env python3
"""Warn when Voxtype is recording without a usable microphone signal."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import time


FRAME = struct.Struct("=Ifff")
RECONNECT_SECONDS = 0.25
NOTIFICATION_ID = "424201"


class HealthTracker:
    """Turn recording state and audio peaks into a small health state machine."""

    def __init__(
        self,
        grace_seconds: float,
        min_dbfs: float,
        ignore_seconds: float = 1.0,
        required_signal_frames: int = 15,
    ) -> None:
        self.grace_seconds = grace_seconds
        self.min_dbfs = min_dbfs
        self.ignore_seconds = ignore_seconds
        self.required_signal_frames = required_signal_frames
        self.recording = False
        self.started_at = 0.0
        self.frame_seen = False
        self.signal_frames = 0
        self.max_dbfs = -120.0
        self.state = "idle"
        self.reason = ""

    def update(
        self, now: float, recording: bool, peak_dbfs: float | None = None
    ) -> tuple[str, str]:
        was_recording = self.recording
        if recording and not was_recording:
            self.started_at = now
            self.frame_seen = False
            self.signal_frames = 0
            self.max_dbfs = -120.0
            self.state = "checking"
            self.reason = "Waiting for microphone signal"

        self.recording = recording

        if recording:
            elapsed = now - self.started_at
            if peak_dbfs is not None and elapsed >= self.ignore_seconds:
                self.frame_seen = True
                if math.isfinite(peak_dbfs):
                    self.max_dbfs = max(self.max_dbfs, peak_dbfs)
                    if peak_dbfs > self.min_dbfs:
                        self.signal_frames += 1

            if self.signal_frames >= self.required_signal_frames:
                if self.state != "ok":
                    self.state = "ok"
                    self.reason = (
                        f"Microphone signal confirmed ({self.max_dbfs:.1f} dBFS)"
                    )
            elif elapsed >= self.grace_seconds:
                self.state = "failed"
                if self.frame_seen:
                    self.reason = (
                        "No sustained microphone signal detected "
                        f"(maximum {self.max_dbfs:.1f} dBFS)"
                    )
                else:
                    self.reason = "Voxtype produced no audio frames"
        elif was_recording and self.state == "checking":
            self.state = "failed"
            self.reason = "Recording ended before microphone signal was confirmed"

        return self.state, self.reason


def runtime_paths() -> tuple[Path, Path, Path]:
    runtime_dir = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    ) / "voxtype"
    return (
        runtime_dir / "audio.sock",
        runtime_dir / "state",
        runtime_dir / "mic-health",
    )


def read_recording_state(state_file: Path) -> bool:
    try:
        return state_file.read_text(encoding="utf-8").strip() == "recording"
    except OSError:
        return False


def write_health(health_file: Path, state: str, reason: str) -> None:
    health_file.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{state}\t{reason}\n"
    temporary = health_file.with_name(f"{health_file.name}.tmp.{os.getpid()}")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, health_file)


def alert_failure(reason: str) -> None:
    subprocess.run(
        [
            "notify-send",
            "--app-name=Voxtype",
            "--urgency=critical",
            f"--replace-id={NOTIFICATION_ID}",
            "Microphone is silent",
            f"{reason}. Check the selected input or mute switch.",
        ],
        check=False,
    )
    subprocess.run(
        ["canberra-gtk-play", "--id=dialog-error", "--description=Microphone silent"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def alert_checking() -> None:
    subprocess.run(
        [
            "notify-send",
            "--app-name=Voxtype",
            "--urgency=low",
            f"--replace-id={NOTIFICATION_ID}",
            "Checking microphone…",
            "Speak now; recording is not trusted until signal is confirmed.",
        ],
        check=False,
    )


def alert_success(restored: bool) -> None:
    title = "Microphone signal restored" if restored else "Microphone confirmed"
    subprocess.run(
        [
            "notify-send",
            "--app-name=Voxtype",
            "--urgency=normal",
            f"--replace-id={NOTIFICATION_ID}",
            title,
            "Voxtype is receiving sustained microphone audio.",
        ],
        check=False,
    )


def parse_args() -> argparse.Namespace:
    default_socket, default_state, default_health = runtime_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=default_socket)
    parser.add_argument("--state-file", type=Path, default=default_state)
    parser.add_argument("--health-file", type=Path, default=default_health)
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=float(os.environ.get("VOXTYPE_MIC_GRACE_SECONDS", "3.0")),
        help="Seconds without signal before warning (default: 3.0)",
    )
    parser.add_argument(
        "--min-dbfs",
        type=float,
        default=float(os.environ.get("VOXTYPE_MIC_MIN_DBFS", "-55.0")),
        help="Minimum peak considered a usable signal (default: -55.0 dBFS)",
    )
    parser.add_argument(
        "--ignore-seconds",
        type=float,
        default=float(os.environ.get("VOXTYPE_MIC_IGNORE_SECONDS", "1.0")),
        help="Ignore startup audio feedback for this long (default: 1.0)",
    )
    parser.add_argument(
        "--required-signal-frames",
        type=int,
        default=int(os.environ.get("VOXTYPE_MIC_REQUIRED_SIGNAL_FRAMES", "15")),
        help="100 Hz frames required to confirm signal (default: 15)",
    )
    parser.add_argument("--no-alerts", action="store_true")
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    tracker = HealthTracker(
        args.grace_seconds,
        args.min_dbfs,
        args.ignore_seconds,
        args.required_signal_frames,
    )
    previous = (tracker.state, tracker.reason)
    write_health(args.health_file, *previous)

    stream: socket.socket | None = None
    buffer = bytearray()
    next_connect_at = 0.0

    while True:
        now = time.monotonic()
        recording = read_recording_state(args.state_file)
        peak: float | None = None

        if stream is None and now >= next_connect_at:
            candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            candidate.settimeout(0.2)
            try:
                candidate.connect(str(args.socket))
                stream = candidate
                buffer.clear()
            except OSError:
                candidate.close()
                next_connect_at = now + RECONNECT_SECONDS

        if stream is not None:
            try:
                chunk = stream.recv(4096)
                if not chunk:
                    stream.close()
                    stream = None
                    next_connect_at = now + RECONNECT_SECONDS
                else:
                    buffer.extend(chunk)
                    while len(buffer) >= FRAME.size:
                        frame = bytes(buffer[: FRAME.size])
                        del buffer[: FRAME.size]
                        _sequence, _minimum, _maximum, frame_peak = FRAME.unpack(frame)
                        peak = frame_peak if peak is None else max(peak, frame_peak)
            except socket.timeout:
                pass
            except OSError:
                stream.close()
                stream = None
                next_connect_at = now + RECONNECT_SECONDS

        current = tracker.update(time.monotonic(), recording, peak)
        if current != previous:
            old_state = previous[0]
            write_health(args.health_file, *current)
            print(f"microphone health: {current[0]}: {current[1]}", flush=True)
            if not args.no_alerts:
                if current[0] == "failed":
                    alert_failure(current[1])
                elif current[0] == "checking":
                    alert_checking()
                elif current[0] == "ok":
                    alert_success(restored=old_state == "failed")
            previous = current

        if stream is None:
            time.sleep(0.05)


def main() -> None:
    args = parse_args()
    if args.grace_seconds <= 0:
        raise SystemExit("--grace-seconds must be positive")
    if not 0 <= args.ignore_seconds < args.grace_seconds:
        raise SystemExit("--ignore-seconds must be non-negative and below grace time")
    if args.required_signal_frames <= 0:
        raise SystemExit("--required-signal-frames must be positive")
    if not -120.0 <= args.min_dbfs <= 0.0:
        raise SystemExit("--min-dbfs must be between -120 and 0")
    run(args)


if __name__ == "__main__":
    main()
