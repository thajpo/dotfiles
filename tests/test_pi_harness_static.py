import hashlib
import json
from pathlib import Path
import re
import shlex
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HarnessStaticTests(unittest.TestCase):
    def test_models_and_agent_limits(self):
        settings = json.loads((ROOT / "pi/settings.json").read_text())
        self.assertEqual(settings["defaultModel"], "gpt-5.6-luna")
        self.assertEqual(settings["defaultThinkingLevel"], "xhigh")
        overrides = settings["subagents"]["agentOverrides"]
        self.assertEqual(overrides["scout"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["scout"]["thinking"], "high")
        self.assertEqual(overrides["worker"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["worker"]["thinking"], "xhigh")
        self.assertEqual(overrides["reviewer"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["reviewer"]["thinking"], "xhigh")
        self.assertEqual(overrides["oracle"]["model"], "openai-codex/gpt-5.6-sol")
        review = json.loads((ROOT / "pi/pr-review.json").read_text())
        self.assertEqual(review["tiers"], {
            "light": "openai-codex/gpt-5.6-luna:xhigh",
            "medium": "openai-codex/gpt-5.6-luna:max",
            "heavy": "openai-codex/gpt-5.6-sol:xhigh",
        })
        self.assertNotIn("advisor", overrides)
        self.assertTrue(all(value["defaultContext"] == "fresh" for value in overrides.values()))
        worker = (ROOT / "pi/agents/worker.md").read_text()
        worker_frontmatter = worker.split("---", 2)[1]
        self.assertIn("model: openai-codex/gpt-5.6-luna", worker_frontmatter)
        self.assertIn("acceptanceRole: writer", worker_frontmatter)
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

    def test_secretary_slice_is_installed_and_mechanically_read_only(self):
        installer = (ROOT / "install.sh").read_text()
        launcher = (ROOT / "bin/pi-secretary").read_text()
        extension = (ROOT / "pi/extensions/secretary/index.ts").read_text()
        self.assertIn("pi-secretary-control.py", installer)
        self.assertIn('activate_path "$STAGING_DIR/control/project-status-skill" "$PI_CONFIG_DIR/skills/project-status"', installer)
        self.assertIn("for launcher in pi pi-host pidev pi-tmux-session pisec pi-secretary", installer)
        for flag in ["--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--tools", "read,grep,find,ls,secretary_git,secretary_record_idea", "--session-id", "--name"]:
            self.assertIn(flag, launcher)
        self.assertEqual(launcher.count("-e \"$"), 1)
        self.assertNotIn("--tools read,grep,find,ls,bash", launcher)
        for forbidden in ["task_packet", "--edit", "--write"]:
            self.assertNotIn(forbidden, launcher)
        self.assertEqual(extension.count("pi.registerTool"), 11)
        self.assertIn("Current user turn did not authorize", extension)
        self.assertIn("fast-forward-only landing", extension)
        self.assertIn("Refuses dirty, live, moved, uncertain, or unlanded", extension)
        self.assertIn("project-status", extension)
        self.assertIn("bind-key -T prefix g", (ROOT / "tmux.conf").read_text())
        brief_extension = (ROOT / "pi/extensions/workstream-brief/index.ts").read_text()
        self.assertIn("getBranch", brief_extension)
        self.assertIn("pi.appendEntry", brief_extension)
        self.assertIn("pi.sendUserMessage", brief_extension)
        channel_extension = (ROOT / "pi/extensions/workstream-channel/index.ts").read_text()
        self.assertIn('name: "notify_secretary"', channel_extension)
        self.assertNotIn("sendUserMessage", channel_extension)
        self.assertNotIn("registerCommand", channel_extension)
        reviewer = (ROOT / "bin/pi-review-agent").read_text()
        self.assertIn("--tools read,grep,find,ls,submit_review_receipt", reviewer)
        self.assertIn("--no-extensions", reviewer)
        self.assertNotIn("bash,", reviewer)
        receipt_extension = (ROOT / "pi/extensions/review-receipt/index.ts").read_text()
        self.assertEqual(receipt_extension.count("pi.registerTool"), 1)
        self.assertIn("PI_REVIEW_CANDIDATE_OID", receipt_extension)
        self.assertLess(brief_extension.index("pi.appendEntry"), brief_extension.index("pi.sendUserMessage"))

    def test_pidev_is_installed_as_a_managed_pi_wrapper(self):
        installer = (ROOT / "install.sh").read_text()
        pidev = (ROOT / "bin/pidev").read_text()
        tmux_helper = (ROOT / "bin/pi-tmux-session").read_text()
        self.assertTrue((ROOT / "bin/pidev").stat().st_mode & 0o111)
        self.assertTrue((ROOT / "bin/pi-tmux-session").stat().st_mode & 0o111)
        self.assertIn("for launcher in pi pi-host pidev pi-tmux-session", installer)
        self.assertIn("--session-id", pidev)
        self.assertIn("tmux new-session", pidev)
        self.assertIn("-F $'#{window_id}\\t#{window_name}'", pidev)
        self.assertIn("-F $'#{pane_id}\\t#{pane_index}\\t#{pane_pid}\\t#{pane_current_command}'", pidev)
        self.assertIn('"$self_dir/pi-tmux-session" "$@"', pidev)
        self.assertIn('exec "$self_dir/pi" "$@"', tmux_helper)

    def test_tmux_resurrect_and_continuum_ordering_is_conservative(self):
        config = (ROOT / "tmux.conf").read_text()
        self.assertNotIn(":all:", config)
        self.assertIn("~^[[:space:]]*", config)
        self.assertIn("/home/j/.local/bin/pidev[[:space:]]+--launch[[:space:]]+--session-id", config)
        self.assertIn("/home/j/.local/bin/pi-tmux-session[[:space:]]+--session-id", config)
        self.assertNotIn('~pi.*--session-id', config)
        self.assertIn("set -g @continuum-restore 'on'", config)
        self.assertIn("set -g @continuum-save-interval '15'", config)
        self.assertLess(config.index("catppuccin/tmux"), config.index("tmux-continuum"))
        self.assertLess(config.index('set -g status-right "#{E:@catppuccin_status_directory}"'), config.index("run '~/.tmux/plugins/tpm/tpm'"))
        self.assertLess(config.index("run '~/.tmux/plugins/tpm/tpm'"), config.index("tmux-voxtype-status.sh"))

    def test_resurrect_patterns_match_only_known_commands_and_argument_order(self):
        config = (ROOT / "tmux.conf").read_text()
        line = next(line for line in config.splitlines() if "@resurrect-processes" in line)
        value = shlex.split(line)[3]
        elements = shlex.split(value)
        patterns = [element[1:] for element in elements if element.startswith("~")]
        self.assertEqual(len(patterns), 3)
        self.assertEqual(len([element for element in elements if element.startswith("~")]), 3)

        # tmux-resurrect feeds each ~ entry as one ERE against the complete
        # command line. Python's equivalent needs POSIX space classes lowered.
        def matches(command, pattern):
            lowered = pattern.replace("[^[:space:]]", r"\S").replace("[[:space:]]", r"\s")
            return re.search(lowered, command) is not None

        known = [
            ("/home/j/.local/bin/pidev --launch --session-id stable", "pidev"),
            ("/usr/bin/bash /home/j/.local/bin/pi-tmux-session --session-id stable", "pi-tmux-session"),
            ("/usr/bin/node /home/j/.local/bin/pi --session-id stable", "pi"),
        ]
        for index, (command, _name) in enumerate(known):
            self.assertTrue(matches(command, patterns[index]), command)
        unrelated = [
            "/home/j/bin/compile --launch --session-id stable",
            "/home/j/bin/rapid --session-id stable",
            "/home/j/bin/mypidev --launch --session-id stable",
            "/home/j/bin/pidev --session-id stable",
            "/home/j/bin/pidev --session-id stable --launch",
            "/home/j/bin/pidev --launch",
            "/home/j/bin/pi-tmux-session",
            "/home/j/bin/pi-tmux-session --name stable",
            "/home/j/bin/pi --name stable",
            "/usr/bin/echo /home/j/bin/pidev --launch --session-id stable",
            "/usr/bin/echo /home/j/bin/pi-tmux-session --session-id stable",
            "/usr/bin/echo /home/j/bin/pi --session-id stable",
            "/tmp/pidev --launch --session-id stable",
            "/tmp/pi-tmux-session --session-id stable",
            "/tmp/pi --session-id stable",
        ]
        for command in unrelated:
            self.assertFalse(any(matches(command, pattern) for pattern in patterns), command)

    def test_btw_and_subagents_task_routes_are_pinned(self):
        btw = (ROOT / "pi/patches/pi-btw-0.4.1-task-routing.patch").read_text()
        self.assertIn("PI_TASK_ROUTE_CAPABILITY", btw)
        worktrees = (ROOT / "pi/patches/pi-subagents-0.35.1-task-worktrees.patch").read_text()
        self.assertIn("must commit all changes before comparison", worktrees)
        self.assertIn("durable comparison", worktrees)
        cwd = (ROOT / "pi/patches/pi-subagents-0.35.1-task-cwd.patch").read_text()
        self.assertIn("outside the parent task execution plane", cwd)

    def test_writer_defaults_and_model_policy_are_fail_closed(self):
        settings = json.loads((ROOT / "pi/settings.json").read_text())
        for name, override in settings["subagents"]["agentOverrides"].items():
            agent_path = ROOT / "pi/agents" / f"{name}.md"
            frontmatter = agent_path.read_text().split("---", 2)[1]
            if "acceptanceRole: writer" in frontmatter:
                self.assertNotRegex(override["model"].lower(), r"deepseek[./:_-]*v4[./_-]*flash")
        package_root = ROOT / "pi/npm/node_modules/pi-subagents"
        fallback = (package_root / "src/runs/shared/model-fallback.ts").read_text()
        self.assertIn("enforceSubagentModelPolicy", fallback)
        self.assertIn("Writer agents cannot use DeepSeek V4 Flash", fallback)
        for relative in [
            "src/runs/foreground/execution.ts",
            "src/runs/foreground/chain-execution.ts",
            "src/runs/foreground/subagent-executor.ts",
            "src/runs/background/async-execution.ts",
            "src/runs/background/subagent-runner.ts",
            "src/runs/background/async-resume.ts",
        ]:
            text = (package_root / relative).read_text()
            self.assertIn("acceptanceRole", text, relative)
        script = """
import { createJiti } from %r;
const jiti = createJiti(import.meta.url);
const policy = await jiti.import(%r);
const reject = (model) => {
  try { policy.enforceSubagentModelPolicy(model, "writer"); return false; }
  catch (error) { return /Writer agents cannot use DeepSeek V4 Flash/.test(String(error)); }
};
for (const model of ["deepseek/deepseek-v4-flash:high", "DeepSeek/DeepSeek_V4_Flash:MAX", "deepseek-v4-flash", "deepseek/deepseek-v4-flash-20260730:off", "deepseek/deepseek-v4-flash-2026-07-30:high", "DeepSeekV4Flash", "deepseek/deepseekv4flash:high", "DeepSeekV4Flash20260730:max"]) {
  if (!reject(model)) process.exit(1);
}
if (policy.enforceSubagentModelPolicy("deepseek/deepseek-v4-flash:high", "read-only") !== "deepseek/deepseek-v4-flash:high") process.exit(1);
try { policy.resolveSubagentModelOverride("deepseek/deepseek-v4-flash:high", undefined, [], undefined, { acceptanceRole: "writer" }); process.exit(1); } catch (error) { if (!/Writer agents cannot use DeepSeek V4 Flash/.test(String(error))) process.exit(1); }
try { policy.buildModelCandidates("deepseek/deepseek-v4-flash:high", undefined, [], undefined, { acceptanceRole: "writer" }); process.exit(1); } catch (error) { if (!/Writer agents cannot use DeepSeek V4 Flash/.test(String(error))) process.exit(1); }
""" % (str(ROOT / "pi/npm/node_modules/jiti/lib/jiti.mjs"), str(ROOT / "pi/npm/node_modules/pi-subagents/src/runs/shared/model-fallback.ts"))
        result = __import__("subprocess").run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        executor = (package_root / "src/runs/foreground/subagent-executor.ts").read_text()
        self.assertNotIn("{ scope: data.modelScope });", executor)
        recovery = (package_root / "src/runs/background/async-resume.ts").read_text()
        self.assertIn('agentConfig.acceptanceRole === "writer" || descriptor.acceptanceRole === "writer"', recovery)

    def test_global_agents_hash_and_removed_legacy_orchestrators(self):
        agents = (ROOT / "agent/AGENTS.md").read_bytes()
        self.assertEqual(hashlib.sha256(agents).hexdigest(), "5fdf1b474c242ad391424c68d27a3291e07c501363b19b8a486be1470ac7d839")
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
