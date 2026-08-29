import hashlib
import unittest

from scripts.pisec.first_mate import FIRST_MATE_BRIEF, FIRST_MATE_RESPONSE_CONTRACT
from scripts.pisec.prompt_contract import (
    FIRST_MATE_RESPONSE_CONTRACT as SHARED_FIRST_MATE_RESPONSE_CONTRACT,
    IMMEDIATE_START_WORKER_CONTRACT,
    MEDIUM_DETAIL_REPORTING_CONTRACT,
    SECRETARY_RESPONSE_CONTRACT,
    SECRETARY_WORKER_TASK_CONTRACT,
)
from scripts.pisec.secretary import SECRETARY_RESPONSE_CONTRACT as SECRETARY_BRIEF_RESPONSE_CONTRACT


class PisecPromptContractTests(unittest.TestCase):
    def test_python_prompt_snapshots_are_stable(self):
        snapshots = {
            "first_mate_response": "8244887c51b486b34f3d3e9e679f70e2f0e4d95e9c00db01bf0bec81d34f166c",
            "secretary_response": "4bcab665533d9b7b1ed4436b715a9f67ac148df0a5c4689f36f39ecbd4b23aea",
            "reporting": "6dc8d42a281e182dc2145c53dfb1d8ea0706079c8ef2b97aaa569fe3d09f8b26",
            "immediate_start": "89be6ade422559aac60d2a5709e3c430a074dc3b2cb58bc1683c70f96dc417c4",
            "worker_task": "c76f87dca204569b413fc947f094a27801631c41084ff136dd4346d0da5d7e24",
        }
        values = {
            "first_mate_response": FIRST_MATE_RESPONSE_CONTRACT,
            "secretary_response": SECRETARY_RESPONSE_CONTRACT,
            "reporting": MEDIUM_DETAIL_REPORTING_CONTRACT,
            "immediate_start": IMMEDIATE_START_WORKER_CONTRACT,
            "worker_task": SECRETARY_WORKER_TASK_CONTRACT,
        }
        self.assertEqual(FIRST_MATE_RESPONSE_CONTRACT, SHARED_FIRST_MATE_RESPONSE_CONTRACT)
        self.assertEqual(SECRETARY_RESPONSE_CONTRACT, SECRETARY_BRIEF_RESPONSE_CONTRACT)
        self.assertEqual(
            {key: hashlib.sha256(values[key].encode()).hexdigest() for key in snapshots},
            snapshots,
        )

    def test_role_prompts_preserve_engineering_boundaries(self):
        self.assertIn(FIRST_MATE_RESPONSE_CONTRACT, FIRST_MATE_BRIEF)
        self.assertIn("original goal", SECRETARY_WORKER_TASK_CONTRACT)
        self.assertIn("starting state", SECRETARY_WORKER_TASK_CONTRACT)
        self.assertIn("required first action", SECRETARY_WORKER_TASK_CONTRACT)
        self.assertIn("acceptance criteria", SECRETARY_WORKER_TASK_CONTRACT)
        self.assertIn("verification", SECRETARY_WORKER_TASK_CONTRACT)
        self.assertIn("starts the assigned engineering task immediately", IMMEDIATE_START_WORKER_CONTRACT)
        for forbidden in (
            "create a worker",
            "prove that a tab exists",
            "complete the worker provisioning operation",
        ):
            self.assertIn(forbidden, SECRETARY_WORKER_TASK_CONTRACT)


if __name__ == "__main__":
    unittest.main()
