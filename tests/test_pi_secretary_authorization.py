import json
from pathlib import Path
import subprocess
import unittest
ROOT = Path(__file__).resolve().parents[1]
AUTH = (ROOT / "pi/extensions/secretary/authorization.ts").resolve()


class SecretaryAuthorizationTests(unittest.TestCase):
    def test_git_authorization_is_explicit_and_clause_scoped(self):
        cases = {
            "the commit is ready": [],
            "should we commit this?": [],
            "what about pushing?": [],
            "you can push now": ["git-push"],
            "please commit and push": ["git-commit-and-push"],
            "commit and do not push": ["git-commit"],
            "commit and don't push": ["git-commit"],
            "do not commit; push the branch": ["git-push"],
            "do not commit and push": [],
            "commit this, but don't push": ["git-commit"],
            "push this commit": ["git-push"],
        }
        script = f"""
import {{ gitWriteWasAuthorized }} from {json.dumps(AUTH.as_uri())};
const cases = {json.dumps(list(cases))};
process.stdout.write(JSON.stringify(cases.map((value) => gitWriteWasAuthorized(value))));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual, list(cases.values()))

    def test_git_cleanup_requires_explicit_destructive_plan_language(self):
        cases = {
            "inspect the benchmark and side-agent branches": False,
            "what would cleanup remove?": False,
            "please plan the cleanup of benchmark branches": False,
            "please apply that Git cleanup plan": True,
            "please apply no Git cleanup": False,
            "delete the benchmark branches and remove the side-agent worktrees": True,
            "do not delete or remove anything": False,
            "yes": False,
        }
        script = f"""
import {{ gitCleanupApplyWasAuthorized }} from {json.dumps(AUTH.as_uri())};
const cases = {json.dumps(list(cases))};
process.stdout.write(JSON.stringify(cases.map((value) => gitCleanupApplyWasAuthorized(value))));
"""
        result = subprocess.run(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), list(cases.values()))


if __name__ == "__main__":
    unittest.main()
