import hashlib
import json
from pathlib import Path
import re
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
        self.assertEqual(overrides["oracle"]["model"], "openai-codex/gpt-5.6-sol")
        self.assertNotIn("advisor", overrides)
        self.assertTrue(all(value["defaultContext"] == "fresh" for value in overrides.values()))
        config = json.loads((ROOT / "pi/extensions/subagent/config.json").read_text())
        self.assertFalse(config["asyncByDefault"])
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
        for evidence in [
            "Reuse is allowed only when every security-relevant creation invariant still matches",
            "CapAdd?.length",
            "CapDrop?.length !== 1",
            'SecurityOpt[0] !== "no-new-privileges:true"',
            "was created from a stale image generation",
            "has unsafe bind propagation",
            "must not share another PID namespace",
            "has an unexpected network namespace",
            "has unexpected network attachments",
            "DeviceRequests?.length",
            'tmpfs["/tmp/pi-home"]',
            "contains an unexpected environment variable",
            "contains duplicate environment variables",
            "has unexpected published ports",
            "GIT_CONFIG_GLOBAL=/run/pi/GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM=1",
            "must have exactly one read-only Git identity",
        ]:
            self.assertIn(evidence, patch)
        self.assertIn("candidatePrefix", patch)
        self.assertIn("/^[1-9][0-9]*$/.test(candidateNumber)", patch)
        self.assertNotIn("safe.directory=*", patch)
        added_source = "\n".join(
            line[1:] for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertNotIn("fastForwardCurrentBranch", added_source)
        self.assertNotIn('target === "current"', added_source)
        self.assertIn("candidateNote", added_source)

    def test_installer_is_fail_closed_and_rollback_protected(self):
        installer = (ROOT / "install.sh").read_text()
        docker_guard = installer.index("docker info")
        core_staging = installer.index("Staging dedicated Pi CLI")
        self.assertLess(docker_guard, core_staging)
        self.assertIn("refusing partial Pi harness activation", installer)
        self.assertIn("trap finish_install EXIT", installer)
        self.assertIn("trap 'exit 130' INT", installer)
        self.assertIn("ACTIVATION_COMMITTED", installer)
        self.assertIn("ACTIVATED_TARGETS", installer)
        self.assertIn("OLD_IMAGE_ID", installer)
        self.assertIn("ROLLBACK_REF=refs/heads/rollback/pi-harness-pre-trusted-live-20260729", installer)
        self.assertIn("Refusing to replace mismatched rollback ref", installer)
        self.assertIn("Existing Pi core has unsafe ownership or writable modes", installer)

    def test_pidev_is_installed_as_a_managed_pi_wrapper(self):
        installer = (ROOT / "install.sh").read_text()
        pidev = (ROOT / "bin/pidev").read_text()
        tmux_helper = (ROOT / "bin/pi-tmux-session").read_text()
        self.assertTrue((ROOT / "bin/pidev").stat().st_mode & 0o111)
        self.assertTrue((ROOT / "bin/pi-tmux-session").stat().st_mode & 0o111)
        self.assertIn("for launcher in pi pi-host pidev pi-tmux-session", installer)
        self.assertIn("--session-id", pidev)
        self.assertIn("tmux new-session", pidev)
        self.assertIn('"$self_dir/pi-tmux-session" "$@"', pidev)
        self.assertIn('exec "$self_dir/pi" "$@"', tmux_helper)

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
        self.assertEqual(hashlib.sha256(agents).hexdigest(), "e733328881865741996ce6342d68a174c1992ded6a17ee62e79cd5e543324c01")
        policy = agents.decode()
        for heading in ["### FAST", "### RIP", "### BUILD", "### MAJOR", "### OFF", "### LIGHT", "### DEEP"]:
            self.assertIn(heading, policy)
        self.assertNotIn("## Default feature workflow", policy)
        workflow = (ROOT / "scripts/agent-workflow-install.sh").read_text()
        package = json.loads((ROOT / "pi/npm/package.json").read_text())
        self.assertIn("pi-btw", package["dependencies"])
        self.assertIn("pi-subagents", package["dependencies"])
        self.assertNotIn("pi-side-agents", package["dependencies"])

    def test_children_use_fresh_scoped_context_and_one_writer(self):
        agent_dir = ROOT / "pi/agents"
        expected = {
            "context-builder", "delegate", "oracle", "planner",
            "researcher", "reviewer", "scout", "worker",
        }
        self.assertEqual({path.stem for path in agent_dir.glob("*.md")}, expected)
        for path in agent_dir.glob("*.md"):
            text = path.read_text()
            frontmatter = text.split("---", 2)[1]
            self.assertIn("defaultContext: fresh", frontmatter, path.name)
            self.assertIn("inheritProjectContext: false", frontmatter, path.name)
            self.assertIn("inheritSkills: false", frontmatter, path.name)
            self.assertIn("subagentOnlyExtensions: /home/j/.pi/agent/extensions/workflow-state/index.ts", frontmatter, path.name)
            tools = re.search(r"^tools: (.+)$", frontmatter, re.MULTILINE).group(1).split(", ")
            if path.stem == "worker":
                self.assertIn("write", tools)
                self.assertIn("edit", tools)
                self.assertIn("acceptanceRole: writer", frontmatter)
            else:
                self.assertNotIn("write", tools, path.name)
                self.assertNotIn("edit", tools, path.name)
                self.assertIn("acceptanceRole: read-only", frontmatter)

    def test_workflow_state_and_compact_parent_skill_are_installed(self):
        extension = (ROOT / "pi/extensions/workflow-state/index.ts").read_text()
        self.assertIn('const TOOL_NAME = "task_packet"', extension)
        self.assertIn('process.env[CHILD_ENV] === "1"', extension)
        self.assertIn("workflowArtifactsDirForSession", extension)
        patch = (ROOT / "pi/patches/pi-subagents-0.35.1-skill.patch").read_text()
        added = "\n".join(
            line[1:] for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertIn("Fresh scoped children by default", added)
        self.assertIn("Configured nesting depth is 1", added)
        self.assertNotIn("Fable mode is the default", added)
        patch_installer = (ROOT / "scripts/pi-patch-subagents").read_text()
        self.assertIn("pi-subagents-0.35.1-skill.patch", patch_installer)
        self.assertIn("496b5d02e0f578336a46aee46534ffddd6097f501791c7844986250e93f776ec", patch_installer)
        installer = (ROOT / "install.sh").read_text()
        self.assertIn("for tree in extensions agents prompts themes", installer)
        self.assertIn('activate_path "$STAGING_DIR/control/$tree" "$PI_CONFIG_DIR/$tree"', installer)


if __name__ == "__main__":
    unittest.main()
