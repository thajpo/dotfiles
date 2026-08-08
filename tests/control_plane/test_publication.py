from __future__ import annotations
import unittest
from scripts.pi_control.errors import ConstraintError
from scripts.pi_control.publication import apply_publication, plan_publication

class PublicationTests(unittest.TestCase):
    def test_exact_nonforce_plan_and_apply(self):
        plan = plan_publication(project_id="prj_" + "1" * 32, source_ref="refs/heads/feature", remote="origin", target_ref="refs/heads/feature", expected_oid="a" * 40, current_oid="b" * 40)
        calls=[]
        result = apply_publication(plan, authorization={"kind":"publish","state":"active"}, observed={"remote":"origin","currentOid":"b" * 40,"sourceRef":"refs/heads/feature","sourceOid":"a" * 40}, runner=lambda args: calls.append(tuple(args)) or (0,"", ""))
        self.assertFalse(result["force"])
        self.assertEqual(calls[0][0:3], ("git","push","--porcelain"))
    def test_invalid_oid_and_ref_refuse_planning(self):
        with self.assertRaises(ConstraintError): plan_publication(project_id="prj_" + "1" * 32, source_ref="main", remote="origin", target_ref="refs/heads/f", expected_oid="bad", current_oid="b" * 40)

    def test_changed_observation_refuses(self):
        plan = plan_publication(project_id="prj_" + "1" * 32, source_ref="refs/heads/f", remote="origin", target_ref="refs/heads/f", expected_oid="a" * 40, current_oid="b" * 40)
        with self.assertRaises(ConstraintError): apply_publication(plan, authorization={"kind":"publish","state":"active"}, observed={"remote":"origin","currentOid":"c" * 40,"sourceRef":"refs/heads/f","sourceOid":"a" * 40}, runner=lambda args: (0,"", ""))
        with self.assertRaises(ConstraintError): apply_publication(plan, authorization={"kind":"publish","state":"active"}, observed={"remote":"origin","currentOid":"b" * 40,"sourceRef":"refs/heads/f","sourceOid":"c" * 40}, runner=lambda args: (0,"", ""))

    def test_changed_observation_ref_refuses(self):
        plan = plan_publication(project_id="prj_" + "1" * 32, source_ref="refs/heads/f", remote="origin", target_ref="refs/heads/f", expected_oid="a" * 40, current_oid="b" * 40)
        with self.assertRaises(ConstraintError):
            apply_publication(plan, authorization={"kind":"publish","state":"active"}, observed={"remote":"origin","currentOid":"b" * 40,"sourceRef":"refs/heads/other","sourceOid":"a" * 40}, runner=lambda args: (0,"", ""))

if __name__ == "__main__": unittest.main()
