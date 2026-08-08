from __future__ import annotations
import unittest
from scripts.pi_control.cleanup import apply_cleanup, plan_cleanup
from scripts.pi_control.errors import ConstraintError

class CleanupTests(unittest.TestCase):
    DIGEST = "sha256:" + "d" * 64

    def test_exact_owned_clean_resource(self):
        plan = plan_cleanup(project_id="prj_" + "1" * 32, resources=[{"resourceId":"wc_" + "2" * 32,"path":"/tmp/exact","controllerOwned":True,"live":False,"dirty":False,"digest":self.DIGEST}])
        removed=[]
        result=apply_cleanup(plan, authorization={"kind":"cleanup","state":"active"}, observed={"wc_" + "2" * 32:{"controllerOwned":True,"live":False,"dirty":False,"digest":self.DIGEST}}, remover=removed.append)
        self.assertEqual(result["removed"],["wc_" + "2" * 32])

    def test_missing_or_invalid_digest_refuses_plan(self):
        for digest in (None, "d", "sha256:" + "g" * 64):
            with self.subTest(digest=digest):
                with self.assertRaises(ConstraintError):
                    plan_cleanup(project_id="prj_" + "1" * 32, resources=[{"resourceId":"wc_" + "2" * 32,"path":"/tmp/x","controllerOwned":True,"live":False,"dirty":False,"digest":digest}])

    def test_missing_observed_digest_refuses_apply(self):
        plan = plan_cleanup(project_id="prj_" + "1" * 32, resources=[{"resourceId":"wc_" + "2" * 32,"path":"/tmp/exact","controllerOwned":True,"live":False,"dirty":False,"digest":self.DIGEST}])
        with self.assertRaises(ConstraintError):
            apply_cleanup(plan, authorization={"kind":"cleanup","state":"active"}, observed={"wc_" + "2" * 32:{"controllerOwned":True,"live":False,"dirty":False}}, remover=lambda path: None)

    def test_live_or_dirty_refuses_plan(self):
        with self.assertRaises(ConstraintError): plan_cleanup(project_id="prj_" + "1" * 32, resources=[{"resourceId":"wc_" + "2" * 32,"path":"/tmp/x","controllerOwned":True,"live":True,"dirty":False,"digest":self.DIGEST}])

if __name__ == "__main__": unittest.main()
