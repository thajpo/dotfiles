from __future__ import annotations
import unittest
from scripts.pi_control.migration_adapters.tmux import observe

class TmuxAdapterTests(unittest.TestCase):
    def test_fixed_command_and_pane_observation(self):
        calls=[]
        def runner(args):
            calls.append(tuple(args)); return (0, "managed\t0\t1\t55\tPi\n", "")
        result=observe(runner)
        self.assertEqual(calls, [("tmux","list-panes","-a","-F","#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_pid}\t#{pane_title}")])
        self.assertEqual(result.state,"observed")
        self.assertEqual(result.records[0].resource_kind,"presentation-observation")

if __name__ == "__main__": unittest.main()
