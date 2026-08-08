from __future__ import annotations

import unittest

from scripts.pi_control.errors import PresentationUnknownError
from scripts.pi_control.presentation_adapter import observe_presentation, plan_restart, plan_swap


class PresentationLifecycleTests(unittest.TestCase):
    def test_restart_is_exact_and_never_broad_kill(self):
        plan = plan_restart(managed_session="pi-managed", observed_session="pi-managed", process_state="stopped", unrelated_sessions=["other"])
        self.assertFalse(plan["broadKill"])
        with self.assertRaises(PresentationUnknownError):
            plan_restart(managed_session="pi-managed", observed_session="other", process_state="present")

    def test_unknown_or_live_worker_refuses_swap(self):
        with self.assertRaises(PresentationUnknownError):
            observe_presentation(backend="tmux", locator={}, process_state="unknown")
        with self.assertRaises(PresentationUnknownError):
            plan_swap(exact_process_state="stopped", live_worker=True)


if __name__ == "__main__": unittest.main()
