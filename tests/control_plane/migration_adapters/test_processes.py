from __future__ import annotations
import unittest
from scripts.pi_control.migration_adapters.processes import observe

class ProcessAdapterTests(unittest.TestCase):
    def test_fixed_command_and_observation(self):
        calls=[]
        def runner(args):
            calls.append(tuple(args)); return (0, "1 0 10 pi\n", "")
        result=observe(runner)
        self.assertEqual(calls, [("ps","-eo","pid=,ppid=,etimes=,args=")])
        self.assertEqual(result.state,"observed")
        self.assertEqual(result.records[0].resource_kind,"process-observation")
    def test_unavailable_is_not_empty(self):
        result=observe(lambda args: (_ for _ in ()).throw(FileNotFoundError("ps")))
        self.assertEqual(result.state,"unavailable")

if __name__ == "__main__": unittest.main()
