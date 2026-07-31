import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/project-status"


class ProjectStatusSkillTests(unittest.TestCase):
    def test_skill_metadata_and_progressive_references_are_complete(self):
        skill = (SKILL / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: project-status\n"))
        self.assertNotIn("TODO", skill)
        for phrase in [
            "what the project or repository state is",
            "what worktrees or branches can be resumed",
            "what recent work has been moving toward",
            "suggestions based on recent work",
            "using Git history to look for gaps",
        ]:
            self.assertIn(phrase, skill)
        for reference in ["investigation.md", "output.md"]:
            self.assertIn(f"references/{reference}", skill)
            self.assertTrue((SKILL / "references" / reference).is_file())

    def test_skill_preserves_read_only_and_uncertainty_contract(self):
        text = " ".join(" ".join(path.read_text().split()) for path in SKILL.rglob("*.md"))
        for phrase in [
            "read-only project lens",
            "recent history and intent",
            "attempts and worktrees",
            "gaps and directions",
            "Observed fact",
            "Explicit statement",
            "Inference",
            "Never mutate Git or runtime state",
            "Never invent percentages",
            "inaccessible sibling worktrees",
            "unrelated session transcripts",
            "explicit target",
            "candidate to inspect or resume",
        ]:
            self.assertIn(phrase, text)
        self.assertIn("at most three", text)
        self.assertIn("Children must not write files", text)

    def test_ui_metadata_matches_skill_invocation(self):
        metadata = (SKILL / "agents/openai.yaml").read_text()
        self.assertIn('display_name: "Project Status"', metadata)
        self.assertIn("$project-status", metadata)
        self.assertNotIn("TODO", metadata)

    def test_installer_keeps_shared_skills_out_of_pi_agents_tree(self):
        installer = (ROOT / "scripts/agent-workflow-install.sh").read_text()
        self.assertIn('link_file "$DOTFILES_DIR/skills" "$HOME/.skills"', installer)
        self.assertNotIn('mkdir -p "$HOME/.agents/skills"', installer)
        self.assertNotIn('link_file "$DOTFILES_DIR/skills/project-status" "$HOME/.agents/skills/project-status"', installer)
        self.assertNotIn('rm -rf "$HOME/.agents/skills"', installer)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            sibling = home / ".agents/skills/other"
            sibling.mkdir(parents=True)
            marker = sibling / "SKILL.md"
            marker.write_text("keep\n")
            env = os.environ.copy()
            env.update({"HOME": str(home), "DOTFILES_DIR": str(ROOT)})
            result = subprocess.run(
                [str(ROOT / "scripts/agent-workflow-install.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            target = home / ".agents/skills/project-status"
            self.assertFalse(target.exists())
            self.assertEqual(marker.read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
