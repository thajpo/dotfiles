from __future__ import annotations
import unittest
from scripts.pi_control.migration_adapters.docker import observe

class DockerAdapterTests(unittest.TestCase):
    def test_fixed_command_and_unlabeled_observation(self):
        calls=[]
        def runner(args):
            calls.append(tuple(args)); return (0, '{"ID":"abc","Names":"x"}\n', "")
        result=observe(runner)
        self.assertEqual(calls, [("docker","ps","--no-trunc","--format","{{json .}}")])
        self.assertEqual(result.state,"observed")
        self.assertIn("observation", result.records[0].resource_kind)
    def test_missing_runtime_is_unavailable(self):
        result=observe(lambda args: (_ for _ in ()).throw(FileNotFoundError("docker")))
        self.assertEqual(result.state,"unavailable")

if __name__ == "__main__": unittest.main()
