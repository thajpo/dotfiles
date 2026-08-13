"""Bounded, process-specific teardown for installed system fixtures."""

from __future__ import annotations

import signal
import subprocess


def terminate_process(process: subprocess.Popen[str], *, timeout: float = 30) -> None:
    """Interrupt one fixture-owned process, escalating only to that PID."""
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


__all__ = ["terminate_process"]
