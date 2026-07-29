import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HarnessStaticTests(unittest.TestCase):
    def test_models_and_agent_limits(self):
        settings = json.loads((ROOT / "pi/settings.json").read_text())
        self.assertEqual(settings["defaultModel"], "gpt-5.6-luna")
        self.assertEqual(settings["defaultThinkingLevel"], "high")
        overrides = settings["subagents"]["agentOverrides"]
        self.assertEqual(overrides["scout"]["model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(overrides["worker"]["model"], "deepseek/deepseek-v4-flash")
        self.assertEqual(overrides["reviewer"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["advisor"]["model"], "openai-codex/gpt-5.6-sol")
        config = json.loads((ROOT / "pi/extensions/subagent/config.json").read_text())
        self.assertEqual(config["globalConcurrencyLimit"], 3)
        self.assertEqual(config["parallel"]["maxTasks"], 3)
        self.assertEqual(config["maxSubagentDepth"], 1)

    def test_only_isolated_publication_configuration_remains(self):
        config = json.loads((ROOT / "pi/extensions/pi-sandbox.json").read_text())
        self.assertNotIn("target", config)
        self.assertEqual(config["passEnv"], [])
        self.assertEqual(config["hostUntrackedFiles"], "ignore")
        self.assertEqual(config["checkpointFrequency"], "agent")
        self.assertEqual(config["lifecycle"], "remove")

    def test_sandbox_patch_contains_required_boundaries(self):
        patch = (ROOT / "pi/patches/pi-sandbox-0.2.0-task-routing.patch").read_text()
        for evidence in [
            "Missing host-owned Pi task route",
            "Task route workspace mismatch",
            '"--cap-drop", "ALL"',
            '"--security-opt", "no-new-privileges:true"',
            "type=bind,src=${source},dst=${source},bind-propagation=rprivate",
            "HOST_CONTEXT.md,readonly=true",
            "type=volume",
            "Trusted-live changes are already visible",
            "Collaborating children cannot checkpoint",
            "removeOwnedTaskContainers",
            "BTW task route capability mismatch",
            'return { trusted: "no"',
        ]:
            self.assertIn(evidence, patch)
        self.assertNotIn("safe.directory=*", patch)
        installed = ROOT / "pi/npm/node_modules/@kjrjay/pi-sandbox/index.ts"
        if installed.exists():
            source = installed.read_text()
            self.assertNotIn("fastForwardCurrentBranch", source)
            self.assertNotIn('target === "current"', source)
            self.assertIn("candidateNote", source)

    def test_btw_and_subagents_task_routes_are_pinned(self):
        btw = (ROOT / "pi/patches/pi-btw-0.4.1-task-routing.patch").read_text()
        self.assertIn("PI_TASK_ROUTE_CAPABILITY", btw)
        worktrees = (ROOT / "pi/patches/pi-subagents-0.35.1-task-worktrees.patch").read_text()
        self.assertIn("must commit all changes before comparison", worktrees)
        self.assertIn("durable comparison", worktrees)
        cwd = (ROOT / "pi/patches/pi-subagents-0.35.1-task-cwd.patch").read_text()
        self.assertIn("outside the parent task execution plane", cwd)

    def test_global_agents_hash_and_removed_legacy_orchestrators(self):
        agents = (ROOT / "agent/AGENTS.md").read_bytes()
        self.assertEqual(hashlib.sha256(agents).hexdigest(), "3452e9a33b92a5c837f5b5aa7cbde68a66c00a70030d68d9d0000a5cdfc2a7c7")
        workflow = (ROOT / "scripts/agent-workflow-install.sh").read_text()
        self.assertNotIn("clone_firstmate", workflow)
        self.assertNotIn("treehouse/install", workflow)
        package = json.loads((ROOT / "pi/npm/package.json").read_text())
        self.assertIn("pi-btw", package["dependencies"])
        self.assertIn("pi-subagents", package["dependencies"])
        self.assertNotIn("pi-side-agents", package["dependencies"])


if __name__ == "__main__":
    unittest.main()
