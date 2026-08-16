from __future__ import annotations

import unittest

from scripts.pi_control.tmux_scope import pi_tmux_socket


class TmuxScopeTests(unittest.TestCase):
    def test_explicit_socket_wins(self) -> None:
        self.assertEqual(
            pi_tmux_socket(environ={"PI_TMUX_SOCKET": "/tmp/explicit.sock", "PI_SYSTEM_STATE_ROOT": "/tmp/state"}),
            "/tmp/explicit.sock",
        )

    def test_state_root_is_used_when_socket_is_unset(self) -> None:
        self.assertEqual(
            pi_tmux_socket(environ={"PI_SYSTEM_STATE_ROOT": "/tmp/state"}),
            "/tmp/state/tmux/pi.sock",
        )

    def test_default_is_under_home(self) -> None:
        self.assertEqual(
            pi_tmux_socket(environ={}, home="/tmp/home"),
            "/tmp/home/.local/state/pi-system/tmux/pi.sock",
        )


if __name__ == "__main__":
    unittest.main()
