from __future__ import annotations
import unittest
from scripts.pi_control.migration_adapters.herdr import observe

class HerdrAdapterTests(unittest.TestCase):
    def test_fixed_json_command_and_redaction(self):
        calls=[]
        def runner(args):
            calls.append(tuple(args)); return (0, '{"space":"one","token":"secret"}', "")
        result=observe(runner)
        self.assertEqual(calls, [("herdr","status","--json")])
        self.assertEqual(result.state,"observed")
        self.assertNotIn("token", str(result.as_dict()))
    def test_missing_herdr_is_unavailable(self):
        result=observe(lambda args: (_ for _ in ()).throw(FileNotFoundError("herdr")))
        self.assertEqual(result.state,"unavailable")

if __name__ == "__main__": unittest.main()
