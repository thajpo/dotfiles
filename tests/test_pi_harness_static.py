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
        self.assertEqual(settings["defaultThinkingLevel"], "max")
        self.assertEqual(settings["enabledModels"], [
            "openai-codex/gpt-5.6-luna:max",
        ])
        self.assertEqual(settings["subagents"]["defaultModel"], "openai-codex/gpt-5.6-luna:max")
        self.assertEqual(settings["subagents"]["modelScope"]["allow"], [
            "openai-codex/gpt-5.6-luna",
        ])
        overrides = settings["subagents"]["agentOverrides"]
        self.assertEqual(overrides["scout"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["scout"]["thinking"], "max")
        self.assertEqual(overrides["worker"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["worker"]["thinking"], "max")
        self.assertEqual(overrides["reviewer"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["reviewer"]["thinking"], "max")
        self.assertEqual(overrides["oracle"]["model"], "openai-codex/gpt-5.6-luna")
        self.assertEqual(overrides["oracle"]["thinking"], "max")
        self.assertTrue(all(value["model"] == "openai-codex/gpt-5.6-luna" for value in overrides.values()))
        self.assertTrue(all(value["thinking"] == "max" for value in overrides.values()))
        review = json.loads((ROOT / "pi/pr-review.json").read_text())
        self.assertEqual(review["tiers"], {
            "light": "openai-codex/gpt-5.6-luna:max",
            "medium": "openai-codex/gpt-5.6-luna:max",
            "heavy": "openai-codex/gpt-5.6-luna:max",
        })
        self.assertNotIn("advisor", overrides)
        self.assertTrue(all(value["defaultContext"] == "fresh" for value in overrides.values()))
        worker = (ROOT / "pi/agents/worker.md").read_text()
        worker_frontmatter = worker.split("---", 2)[1]
        self.assertIn("model: openai-codex/gpt-5.6-luna", worker_frontmatter)
        self.assertIn("acceptanceRole: writer", worker_frontmatter)
        self.assertIn("tools: read, write, edit, bash, grep, find, ls, web_search, fetch_content, get_search_content, source_check, contact_supervisor, intercom, host_command, subagent", worker_frontmatter)
        config = json.loads((ROOT / "pi/extensions/subagent/config.json").read_text())
        self.assertTrue(config["asyncByDefault"])
        self.assertFalse(config["asyncWidget"])
        self.assertTrue(config["forceTopLevelAsync"])
        self.assertFalse(config["fleetView"])
        self.assertEqual(config["artifactDir"], "session")
        self.assertEqual(config["completionVisibility"], "hidden-success")
        self.assertEqual(config["waitTool"], {"enabled": False})
        self.assertNotIn("globalConcurrencyLimit", config)
        self.assertNotIn("timeoutMs", config)
        self.assertNotIn("maxRuntimeMs", config)
        self.assertNotIn("turnBudget", config)
        self.assertNotIn("toolBudget", config)
        self.assertEqual(config["parallel"]["maxTasks"], 0)
        self.assertEqual(config["maxSubagentSpawnsPerSession"], 0)
        self.assertEqual(config["maxSubagentDepth"], 2)
        self.assertIn("npm:@narumitw/pi-goal@0.43.0", settings["packages"])
        self.assertIn("npm:pi-image-tools@1.4.0", settings["packages"])
        self.assertEqual(json.loads((ROOT / "pi/keybindings.json").read_text())["app.clipboard.pasteImage"], [])
        package = json.loads((ROOT / "pi/npm/package.json").read_text())
        self.assertEqual(package["dependencies"]["@narumitw/pi-goal"], "0.43.0")
        self.assertEqual(package["dependencies"]["pi-image-tools"], "1.4.0")
        goal = json.loads((ROOT / "pi/pi-goal.json").read_text())
        self.assertEqual(goal["continuationLimits"], {"automaticTurns": None, "noProgressTurns": None})
        continuation = (ROOT / "pi/extensions/auto-continue/index.ts").read_text()
        self.assertIn('if (event.willRetry) return;', continuation)
        self.assertIn('entries.at(-1)?.type === "compaction"', continuation)
        self.assertIn('customType: "pi-auto-continue"', continuation)
        self.assertIn('display: false', continuation)
        self.assertIn('triggerTurn: true', continuation)
        self.assertIn('Context compaction has completed', continuation)
        harness_readme = (ROOT / "pi/README.md").read_text()
        self.assertIn("no automatic elapsed-time,\nassistant-turn", harness_readme)
        self.assertIn("Anti-slop means", harness_readme)
        self.assertIn("/observe", harness_readme)
        root_readme = (ROOT / "README.md").read_text()
        self.assertIn("no automatic turn,\n  elapsed-time, token, or tool-call limit", root_readme)
        self.assertNotIn("default is ten automatic responses", root_readme)
        host_command = (ROOT / "pi/extensions/host-command/index.ts").read_text()
        host_core = (ROOT / "pi/extensions/host-command/core.mjs").read_text()
        self.assertIn('name: "host_command"', host_command)
        self.assertIn("Reason:", host_command)
        self.assertIn("Description:", host_command)
        self.assertIn("ctx.ui.confirm", host_command)
        self.assertIn("parentRuntimeId", host_core)
        self.assertIn("expiresAt", host_core)
        for agent_path in (ROOT / "pi/agents").glob("*.md"):
            text = agent_path.read_text()
            frontmatter = text.split("---", 2)[1]
            self.assertIn("host_command", text, agent_path.name)
            self.assertIn("memory:\n  scope: user\n  path: pi-harness", frontmatter, agent_path.name)
            self.assertIn("agent-feedback.v1", text, agent_path.name)
            self.assertIn("AGENT_FEEDBACK", text, agent_path.name)
        harness_readme = (ROOT / "pi/README.md").read_text()
        self.assertIn("Agent feedback and intake", harness_readme)
        self.assertIn("~/.pi/agent/agent-memory/pi-harness/MEMORY.md", harness_readme)
        self.assertIn("secretary_record_idea", harness_readme)
        acceptance = (ROOT / "pi/WORKFLOW_ACCEPTANCE.md").read_text()
        self.assertIn("agent-feedback.v1", acceptance)
        self.assertIn("AGENT_FEEDBACK", acceptance)
        self.assertIn("can reply with a structured", acceptance)
        self.assertIn("pi-subagents-0.35.1-host-command-fanout.patch", (ROOT / "scripts/pi-patch-subagents").read_text())
        self.assertIn("pi-subagents-0.35.1-web-access-fanout.patch", (ROOT / "scripts/pi-patch-subagents").read_text())

    def test_observability_inspector_is_read_only_and_activated(self):
        extension = (ROOT / "pi/extensions/observability/index.ts").read_text()
        core = (ROOT / "pi/extensions/observability/core.mjs").read_text()
        self.assertIn('pi.registerCommand(OBSERVE_COMMAND', extension)
        self.assertIn('Key.ctrl("i")', extension)
        self.assertIn('overlay: true', extension)
        self.assertIn('SUBAGENT_ASYNC_STARTED_EVENT', extension)
        self.assertIn('SUBAGENT_FOREGROUND_STARTED_EVENT', extension)
        self.assertIn('SUBAGENT_FOREGROUND_COMPLETE_EVENT', extension)
        self.assertIn('extractActivePacket', extension)
        self.assertIn('read-only', extension)
        self.assertIn('validatePacket', core)
        self.assertIn('redacted', core)
        self.assertIn('MAX_MESSAGES', core)
        foreground_patch = "\n".join([
            (ROOT / "pi/patches/pi-subagents-0.35.1-observability-foreground-start-types.patch").read_text(),
            (ROOT / "pi/patches/pi-subagents-0.35.1-observability-foreground-start-executor.patch").read_text(),
        ])
        self.assertIn("SUBAGENT_FOREGROUND_STARTED_EVENT", foreground_patch)

    def test_secretary_investigators_are_mechanically_read_only(self):
        wrapper = (ROOT / "pi/extensions/secretary-subagents/index.ts").read_text()
        self.assertIn('const SAFE_ACTIONS = new Set<SafeAction>(["list", "doctor", "status", "interrupt", "stop", "resume", "steer"]);', wrapper)
        self.assertIn('params.action.toLowerCase()', wrapper)
        self.assertIn('requires explicit current-turn user intent', wrapper)
        self.assertIn('actionTargetIsAuthorized', wrapper)
        self.assertIn('must target the run selected by the current user turn', wrapper)
        lifecycle = (ROOT / "pi/extensions/secretary-subagents/lifecycle.ts").read_text()
        self.assertIn('const TARGETED_ACTIONS', lifecycle)
        self.assertIn('selected = candidates.find', lifecycle)
        self.assertIn('discoverAgents(cwd, scope).agents.filter(isReadOnlyAgent)', wrapper)
        self.assertIn('const INVESTIGATOR_TOOLS = ["read", "grep", "find", "ls", "web_search", "fetch_content", "get_search_content", "source_check", "secretary_git", "contact_supervisor", "intercom", "host_command"]', wrapper)
        self.assertIn('subagentOnlyExtensions: [gitExtension, autoContinueExtension, hostCommandExtension, webAccessExtension]', wrapper)
        self.assertIn('extensions: []', wrapper)
        self.assertIn('mcpDirectTools: []', wrapper)
        self.assertIn('memory: undefined', wrapper)
        self.assertIn('inheritProjectContext: false', wrapper)
        self.assertIn('params.worktree === true', wrapper)
        self.assertIn('asyncByDefault: true', wrapper)
        self.assertIn('forceTopLevelAsync: true', wrapper)
        self.assertIn('params.async === false', wrapper)
        self.assertIn('async: true', wrapper)
        self.assertIn('clarify: false', wrapper)
        self.assertIn('createResultWatcher', wrapper)
        self.assertIn('registerSubagentNotify', wrapper)
        self.assertIn('createAsyncJobTracker', wrapper)
        self.assertIn('registerWaitTool', wrapper)
        self.assertIn('if (waitToolEnabled) registerWaitTool(pi, state, true);', wrapper)
        self.assertIn('resolveWaitToolConfig', wrapper)
        self.assertIn('const waitToolEnabled = loaded.waitTool === undefined', wrapper)
        self.assertIn('resolveWaitToolConfig(loaded.waitTool).enabled', wrapper)
        self.assertIn('must not silently re-enable a blocking wait tool', wrapper)
        self.assertIn('rely on completion notifications', wrapper)
        self.assertIn('__pi_secretary_subagents_runtime_cleanup__', wrapper)
        self.assertIn('cleanupRuntime', wrapper)
        self.assertIn('artifacts: false', wrapper)
        self.assertIn('output: false', wrapper)
        self.assertIn('worktree: false', wrapper)
        self.assertIn('level: "none"', wrapper)
        self.assertIn('defaultTimeoutMs: undefined', wrapper)
        self.assertIn('defaultTurnBudget: undefined', wrapper)
        self.assertIn('turnBudget: undefined', wrapper)
        self.assertIn('timeoutMs: undefined', wrapper)
        self.assertIn('maxRuntimeMs: undefined', wrapper)
        self.assertIn('absoluteDeadlineAt: undefined', wrapper)
        self.assertIn('toolBudget: undefined', wrapper)
        self.assertIn('recordSecretarySubagentStats', wrapper)
        self.assertIn('recordSecretarySessionStats', wrapper)
        self.assertIn('defaultSessionDir: undefined', wrapper)
        self.assertIn('singleRunOutputBaseDir: undefined', wrapper)
        self.assertNotIn('maxTurns: 10,', wrapper)
        self.assertNotIn('hasExplicitOutput', wrapper)
        self.assertNotIn('cannot write output files', wrapper)

        secretary = (ROOT / "pi/extensions/secretary/index.ts").read_text()
        authorization = (ROOT / "pi/extensions/secretary/authorization.ts").read_text()
        self.assertIn('if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;', secretary)
        launcher = (ROOT / "bin/pi-secretary").read_text()
        self.assertIn('auto_continue_extension="$agent_dir/extensions/auto-continue/index.ts"', launcher)
        self.assertIn('-e "$auto_continue_extension"', launcher)
        self.assertIn('&& -r "$auto_continue_extension"', launcher)
        self.assertIn('name: "secretary_git_write"', secretary)
        self.assertIn('name: "secretary_git_cleanup"', secretary)
        self.assertIn('PI_SUBAGENT_CHILD', secretary)
        self.assertIn('git-commit-and-push', secretary)
        self.assertIn('gitWriteWasAuthorized', secretary)
        self.assertIn('const landingDenied', secretary)
        self.assertIn('const integrationDenied', secretary)
        self.assertIn('Never inherit a generic affirmation for landing or integration.', secretary)
        self.assertIn('reviewer receipt never authorizes automatic merge', secretary)
        self.assertIn('you can push now', authorization)
        self.assertIn('please commit and push', authorization)
        self.assertIn('Current user turn did not authorize secretary', secretary)
        self.assertIn('if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;', wrapper)
        git_tool = (ROOT / "pi/extensions/secretary-investigator-git/index.ts").read_text()
        self.assertIn('if (process.env.PI_SECRETARY_READ_ONLY !== "1") return;', git_tool)
        self.assertIn('name: "secretary_git"', git_tool)
        self.assertIn('PI_SECRETARY_READ_ONLY', git_tool)
        self.assertIn('"git-read"', git_tool)
        self.assertNotIn('name: "bash"', git_tool)
        control = (ROOT / "scripts/pi-secretary-control.py").read_text()
        self.assertIn('GIT_WRITE_OPERATIONS = {"commit", "push", "commit-and-push"}', control)
        self.assertIn('sub.add_parser("git-write")', control)
        self.assertIn('"origin"', control)
        self.assertIn('pi-secretary-git-write.lock', control)
        self.assertNotIn('argparse.REMAINDER', control[control.index('sub.add_parser("git-write")'):control.index('sub.add_parser("brief-create")')])

        agents = [
            path for path in (ROOT / "pi/agents").glob("*.md")
            if "acceptanceRole: read-only" in path.read_text()
        ]
        self.assertTrue(agents)
        for path in agents:
            tools = re.search(r"^tools: (.+)$", path.read_text(), re.MULTILINE).group(1).split(", ")
            self.assertNotIn("edit", tools, path.name)
            self.assertNotIn("write", tools, path.name)
            self.assertNotIn("subagent", tools, path.name)

    def test_only_isolated_publication_configuration_remains(self):
        config = json.loads((ROOT / "pi/extensions/pi-sandbox.json").read_text())
        self.assertNotIn("target", config)
        self.assertEqual(config["passEnv"], [])
        self.assertEqual(config["hostUntrackedFiles"], "ignore")
        self.assertEqual(config["checkpointFrequency"], "agent")
        self.assertEqual(config["lifecycle"], "remove")
        self.assertEqual(config["dockerPortMode"], "disabled")
        self.assertEqual(config["installDeps"], "auto")

    def test_platform_and_runtime_contracts_are_explicit(self):
        profile = (ROOT / "machines/macos-arm64.env").read_text()
        runtime = (ROOT / "scripts/pi-runtime.py").read_text()
        dockerfile = (ROOT / "pi/sandbox/Dockerfile").read_text()
        sandbox = (ROOT / "pi/patches/pi-sandbox-0.2.0-runtime-contract.patch").read_text()
        self.assertIn('PI_TRUSTED_PROJECT_ROOTS="${HOME}/projects"', profile)
        self.assertIn("manifest-only build context", runtime)
        self.assertIn("uv==${UV_VERSION}", dockerfile)
        self.assertIn("executionTarget", sandbox)
        self.assertIn("skill-resource", sandbox)
        self.assertIn("environment-key", sandbox)
        self.assertIn("version: 2", sandbox)
        workspace = (ROOT / "scripts/pi-workspace.py").read_text()
        self.assertIn('PI_CORE_PACKAGE_NAME = "@earendil-works/pi-coding-agent"', workspace)
        self.assertIn('PI_CORE_PACKAGE_VERSION = "0.83.0"', workspace)
        self.assertIn("refusing control-plane route preparation", workspace)

    def test_sandbox_patch_contains_required_boundaries(self):
        patch = (ROOT / "pi/patches/pi-sandbox-0.2.0-task-routing.patch").read_text()
        control_plane_patch = (ROOT / "pi/patches/pi-sandbox-0.2.0-control-plane-resources.patch").read_text()
        ownership_patch = (ROOT / "pi/patches/pi-sandbox-0.2.0-user-workspace.patch").read_text()
        child_lifecycle_patch = (ROOT / "pi/patches/pi-sandbox-0.2.0-child-lifecycle.patch").read_text()
        child_lease_patch = (ROOT / "pi/patches/pi-subagents-0.35.1-sandbox-child-lease.patch").read_text()
        child_runner_patch = (ROOT / "pi/patches/pi-subagents-0.35.1-sandbox-child-lease-runner.patch").read_text()
        child_foreground_patch = (ROOT / "pi/patches/pi-subagents-0.35.1-sandbox-child-lease-foreground.patch").read_text()
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
            '"--no-same-owner", "-xf", "-"',
            'source.stdout.on("error", reject)',
            'dest.stdin.on("error", reject)',
        ]:
            self.assertIn(evidence, patch)
        for evidence in [
            "controlPlaneResources",
            "controlPlanePackageRoot",
            "Control-plane routes must expose the pinned Pi docs and examples",
            "validateControlPlanePackageRoot",
            "CONTROL_PLANE_PI_PACKAGE_VERSION",
            "metadata.uid",
            "contains an escaping symlink",
            "readonly=true,bind-propagation=rprivate",
        ]:
            self.assertIn(evidence, control_plane_patch)
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
        for evidence in [
            'route.mode === "isolated" ? ["CAP_CHOWN"] : []',
            'route.mode === "isolated" ? ["--cap-add", "CHOWN"] : []',
            '"chown", identity || "0:0", state.repoRoot',
            'spawn(this.config.runtime, ["exec", "-i", this.containerName!',
            'chmod -R u+rwX .',
        ]:
            self.assertIn(evidence, ownership_patch)
        for evidence in [
            "subagent-sandbox-leases",
            "parent-transition.json",
            "processStartIdentity",
            "checkpoint or move the sandbox ref",
            "getChildLifecycleStatus",
            "FROZEN",
            "verify and export their artifacts",
            "recovery",
            "workspace must be clean",
            "child cannot rebind",
            "history must descend from the recorded host OID",
            "Sandbox child lease metadata is incomplete",
        ]:
            self.assertIn(evidence, child_lifecycle_patch)
        for evidence in [
            "ensureNoSandboxParentTransition",
            "PI_SUBAGENT_CHILD",
            "writePrivateAtomicJson",
            "processStartIdentity",
            "child start is blocked until it finishes",
            "!path.isAbsolute(value.routePath)",
            "Number.isFinite(value.acquiredAtMs)",
            "processStartIdentity !== undefined && typeof value.processStartIdentity !== \"string\"",
        ]:
            self.assertIn(evidence, child_lease_patch)
        self.assertIn("acquireSandboxChildLease", child_runner_patch)
        self.assertIn("acquireSandboxChildLease", child_foreground_patch)
        added_lines = "\n".join(
            line[1:]
            for line in ownership_patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertNotIn('"chown", "-R"', added_lines)
        self.assertNotIn('state.repoRoot, "/tmp/pi-home"', added_lines)
        installer = (ROOT / "scripts/pi-patch-subagents").read_text()
        self.assertIn("pi-subagents-0.35.1-hidden-success.patch", installer)
        self.assertIn("pi-subagents-0.35.1-hidden-success-extension.patch", installer)
        self.assertIn("pi-subagents-0.35.1-worker-read-only-fanout.patch", installer)
        self.assertIn("pi-subagents-0.35.1-host-command-fanout.patch", installer)
        self.assertIn("pi-subagents-0.35.1-failure-events.patch", installer)
        self.assertIn("pi-subagents-0.35.1-observability-foreground-start-types.patch", installer)
        self.assertIn("pi-subagents-0.35.1-observability-foreground-start-executor.patch", installer)
        self.assertIn("pi-subagents-0.35.1-sandbox-child-lease.patch", installer)
        self.assertIn("pi-subagents-0.35.1-sandbox-child-lease-runner.patch", installer)
        self.assertIn("pi-subagents-0.35.1-sandbox-child-lease-foreground.patch", installer)
        self.assertIn("pi-sandbox-0.2.0-child-lifecycle.patch", installer)
        self.assertIn("pi-sandbox-0.2.0-user-workspace.patch", installer)
        self.assertIn("pi-sandbox-0.2.0-runtime-contract.patch", installer)
        self.assertIn("pi-sandbox-0.2.0-control-plane-resources.patch", installer)
        install_script = (ROOT / "install.sh").read_text()
        self.assertIn("pi-runtime.py", install_script)
        self.assertIn("pi-sandbox-gc.py", install_script)
        self.assertIn("PI_TRUSTED_PROJECT_ROOTS", install_script)
        integration = (ROOT / "tests/pi-docker-isolated-ownership.sh").read_text()
        self.assertIn("--cap-add CHOWN", integration)
        self.assertIn("repeated tool-call execution", integration)
        runtime_integration = (ROOT / "tests/pi-docker-runtime-cache.sh").read_text()
        self.assertIn("environmentKey", runtime_integration)
        self.assertIn("native-host-sentinel", runtime_integration)
        gc = (ROOT / "scripts/pi-sandbox-gc.py").read_text()
        self.assertIn('docker system prune', (ROOT / "pi/README.md").read_text())
        self.assertIn("--apply", gc)
        self.assertIn("pi.container-sandbox.managed", gc)
        self.assertIn("owner_status", gc)
        self.assertIn("owner state unproven", gc)

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
        self.assertIn("pi-restart to activate the new generation", installer)
        self.assertNotIn("tmux source-file ~/.tmux.conf", installer)
        self.assertIn("uname -s", installer)
        self.assertIn("brew install gitmux", installer)
        self.assertIn("__PI_AGENT_DIR__", installer)

    def test_machine_profiles_are_selected_and_installed(self):
        installer = (ROOT / "install.sh").read_text()
        mac_profile = (ROOT / "machines/macos-arm64.env").read_text()
        linux_profile = (ROOT / "machines/linux-x86_64.env").read_text()
        self.assertIn("Darwin:arm64|Darwin:aarch64", installer)
        self.assertIn("Linux:x86_64|Linux:amd64", installer)
        self.assertIn('DOTFILES_MACHINE_ID=macos-arm64', installer)
        self.assertIn('DOTFILES_MACHINE_ID=linux-x86_64', linux_profile)
        self.assertIn('MACHINE_CONFIG_PATH="$MACHINE_CONFIG_DIR/machine.env"', installer)
        self.assertIn('activate_path "$STAGING_DIR/control/machine.env" "$MACHINE_CONFIG_PATH"', installer)
        for profile in (mac_profile, linux_profile):
            self.assertIn("PI_PERSONAL_MLRE_DIR", profile)
            self.assertIn("PI_PERSONAL_FINANCIALS_DIR", profile)
            self.assertIn("PI_PERSONAL_DOTFILES_DIR", profile)
        self.assertIn('PI_TRUSTED_PROJECT_ROOTS="${HOME}/Projects"', linux_profile)

    def test_secretary_slice_is_installed_and_mechanically_read_only(self):
        installer = (ROOT / "install.sh").read_text()
        launcher = (ROOT / "bin/pi-secretary").read_text()
        extension = (ROOT / "pi/extensions/secretary/index.ts").read_text()
        self.assertIn("pi-secretary-control.py", installer)
        self.assertIn("pi-root-session.py", installer)
        self.assertIn("pi-secretary-stats.py", installer)
        self.assertIn('skill_rollback_dir="${XDG_STATE_HOME:-$HOME/.local/state}/pi/rollback/skills"', installer)
        self.assertIn('activate_path "$STAGING_DIR/control/project-status-skill" "$PI_CONFIG_DIR/skills/project-status" "$skill_rollback_dir"', installer)
        self.assertIn('local source=$1 target=$2 rollback_dir backup=""', installer)
        self.assertIn('rollback_dir=${3:-$(dirname "$target")}', installer)
        self.assertIn("pi-sandbox-gc", installer)
        for flag in ["--no-extensions", "--no-skills", "--no-context-files", "--no-prompt-templates", "--tools", "read,grep,find,ls,web_search,fetch_content,get_search_content,source_check,host_command,subagent,secretary_git,secretary_git_write,secretary_git_cleanup,secretary_record_idea", "--session", "--name"]:
            self.assertIn(flag, launcher)
        self.assertNotIn("subagent_wait", launcher)
        self.assertEqual(launcher.count("-e \"$"), 7)
        self.assertIn('fast_mode_extension="$agent_dir/extensions/fast-mode/index.ts"', launcher)
        self.assertIn('root_session_extension="$agent_dir/extensions/root-session/index.ts"', launcher)
        self.assertIn('auto_continue_extension="$agent_dir/extensions/auto-continue/index.ts"', launcher)
        self.assertIn('host_command_extension="$agent_dir/extensions/host-command/index.ts"', launcher)
        root_extension = (ROOT / "pi/extensions/root-session/index.ts").read_text()
        self.assertIn('register-existing', root_extension)
        self.assertIn('PI_SUBAGENT_CHILD', root_extension)
        self.assertIn('sessions", "root', root_extension)
        self.assertNotIn("--tools read,grep,find,ls,bash", launcher)
        for forbidden in ["task_packet", "--edit", "--write"]:
            self.assertNotIn(forbidden, launcher)
        self.assertEqual(extension.count("pi.registerTool"), 14)
        self.assertIn("Current user turn did not authorize", extension)
        self.assertIn("Natural-language requests to log", extension)
        self.assertIn("log|note|capture|document", extension)
        self.assertIn("fast-forward-only landing", extension)
        self.assertIn("Refuses dirty, live, moved, uncertain, or unlanded", extension)
        self.assertIn("project-status", extension)
        secretary_subagents = (ROOT / "pi/extensions/secretary-subagents/index.ts").read_text()
        self.assertIn('name: "subagent"', secretary_subagents)
        self.assertIn("Number.MAX_SAFE_INTEGER", secretary_subagents)
        self.assertIn("requestsWorktree", secretary_subagents)
        self.assertIn("isReadOnlyAgent", secretary_subagents)
        self.assertIn("no elapsed-time, assistant-turn, or tool-call budgets", secretary_subagents)
        self.assertIn("secretary-stats.jsonl", (ROOT / "pi/README.md").read_text())
        self.assertIn("bind-key -T prefix g", (ROOT / "tmux.conf").read_text())
        self.assertIn("pi-personal", (ROOT / "tmux.conf").read_text())
        brief_extension = (ROOT / "pi/extensions/workstream-brief/index.ts").read_text()
        self.assertIn("getBranch", brief_extension)
        self.assertIn("pi.appendEntry", brief_extension)
        self.assertIn("pi.sendUserMessage", brief_extension)
        channel_extension = (ROOT / "pi/extensions/workstream-channel/index.ts").read_text()
        self.assertIn('name: "notify_secretary"', channel_extension)
        self.assertNotIn("sendUserMessage", channel_extension)
        self.assertNotIn("registerCommand", channel_extension)
        reviewer = (ROOT / "bin/pi-review-agent").read_text()
        self.assertIn("--tools read,grep,find,ls,web_search,fetch_content,get_search_content,source_check,submit_review_receipt", reviewer)
        self.assertNotIn("host_command", reviewer)
        self.assertIn("--no-extensions", reviewer)
        self.assertIn('--model "openai-codex/gpt-5.6-luna" --thinking high', reviewer)
        self.assertNotIn("bash,", reviewer)
        control = (ROOT / "scripts/pi-secretary-control.py").read_text()
        submit = control[control.index("def submit_review"):control.index("def review_status")]
        self.assertIn("with _project_lock(project):", submit)
        self.assertIn("_validate_review_workspace(current, require_clean=True)", submit[submit.index("with _project_lock(project):"):])
        receipt_extension = (ROOT / "pi/extensions/review-receipt/index.ts").read_text()
        self.assertEqual(receipt_extension.count("pi.registerTool"), 1)
        self.assertIn("PI_REVIEW_CANDIDATE_OID", receipt_extension)
        self.assertIn("PI_REVIEW_CAPABILITY", receipt_extension)
        self.assertIn("reviewAssignment", receipt_extension)
        self.assertIn("if (!assigned) return;", receipt_extension)
        self.assertLess(brief_extension.index("pi.appendEntry"), brief_extension.index("pi.sendUserMessage"))

    def test_pidev_is_installed_as_a_managed_pi_wrapper(self):
        installer = (ROOT / "install.sh").read_text()
        pidev = (ROOT / "bin/pidev").read_text()
        tmux_helper = (ROOT / "bin/pi-tmux-session").read_text()
        self.assertTrue((ROOT / "bin/pidev").stat().st_mode & 0o111)
        self.assertTrue((ROOT / "bin/pi-tmux-session").stat().st_mode & 0o111)
        self.assertTrue((ROOT / "bin/pi-root-session").stat().st_mode & 0o111)
        self.assertTrue((ROOT / "bin/pi-start").stat().st_mode & 0o111)
        self.assertTrue((ROOT / "bin/pi-help-custom").stat().st_mode & 0o111)
        self.assertIn("for launcher in pi pi-start pi-help-custom pi-host pidev pi-tmux-session", installer)
        self.assertIn("--session-id", pidev)
        self.assertIn("exact durable root JSONL", (ROOT / "bin/pi-tmux-session").read_text())
        self.assertIn("tmux new-session", pidev)
        self.assertIn("-F $'#{window_id}\\t#{window_name}'", pidev)
        self.assertIn("-F $'#{pane_id}\\t#{pane_index}\\t#{pane_pid}\\t#{pane_current_command}'", pidev)
        self.assertIn('"$self_dir/pi-tmux-session" "$@"', pidev)
        self.assertIn('"$self_dir/pi" "$@"', tmux_helper)
        self.assertNotIn('exec "$self_dir/pi" "$@"', tmux_helper)

    def test_pi_restart_rebuilds_all_tmux_workspaces(self):
        installer = (ROOT / "install.sh").read_text()
        restart = (ROOT / "bin/pi-restart").read_text()
        self.assertTrue((ROOT / "bin/pi-restart").stat().st_mode & 0o111)
        self.assertIn("pi-restart", installer)
        self.assertIn("tmux kill-server", restart)
        self.assertIn('exec "$pi_start" all', restart)
        self.assertIn("client_tty", restart)
        self.assertIn("PI_RESTARTING=1", restart)
        self.assertIn("set-environment -gu PI_RESTARTING", (ROOT / "bin/pi-start").read_text())
        self.assertIn('PI_RESTARTING:-0', (ROOT / "tmux.conf").read_text())

    def test_custom_help_covers_common_pi_commands(self):
        help_text = (ROOT / "bin/pi-help-custom").read_text()
        for command in [
            "pi --help-custom", "pi-start help", "pi-restart", "pi-start all", "pi-root-session migrate",
            "/goal <objective>", "/goal status|pause|resume", "/goal --tokens 100k",
            "/fast on|off|status", "/subagents-doctor",
        ]:
            self.assertIn(command, help_text)

    def test_tmux_workspace_clients_release_repair_locks_before_attach(self):
        personal = (ROOT / "bin/pi-personal").read_text()
        secretary = (ROOT / "bin/pisec").read_text()
        personal_release = 'eval "exec ${personal_lock_fd}>&-"'
        secretary_release = 'eval "exec ${lock_fd}>&-"'
        self.assertIn(personal_release, personal)
        self.assertIn(secretary_release, secretary)
        self.assertLess(personal.index(personal_release), personal.index("exec tmux switch-client"))
        self.assertLess(secretary.index(secretary_release), secretary.index("exec tmux switch-client"))

    def test_fast_mode_uses_openai_priority_service_tier(self):
        extension = (ROOT / "pi/extensions/fast-mode/index.ts").read_text()
        self.assertIn('pi.registerCommand("fast"', extension)
        self.assertIn('service_tier: "priority"', extension)
        self.assertIn('OPENAI_PROVIDERS', extension)
        self.assertIn('Usage: /fast [on|off|status]', extension)

    def test_tmux_resurrect_and_continuum_ordering_is_conservative(self):
        config = (ROOT / "tmux.conf").read_text()
        self.assertNotIn(":all:", config)
        self.assertIn("~^[[:space:]]*", config)
        self.assertIn(r"[^[:space:]]*/\.local/bin/pidev[[:space:]]+--launch[[:space:]]+--session-id", config)
        self.assertIn(r"[^[:space:]]*/\.local/bin/pi-tmux-session[[:space:]]+--session-id", config)
        self.assertNotIn('~pi.*--session-id', config)
        self.assertIn("set -g @continuum-restore 'on'", config)
        self.assertIn("set -g @continuum-save-interval '15'", config)
        self.assertLess(config.index("catppuccin/tmux"), config.index("tmux-continuum"))
        self.assertLess(config.index('set -g status-right "#{E:@catppuccin_status_directory}"'), config.index("run '~/.tmux/plugins/tpm/tpm'"))
        self.assertLess(config.index("run '~/.tmux/plugins/tpm/tpm'"), config.index("tmux-voxtype-status"))

    def test_resurrect_patterns_match_only_known_commands_and_argument_order(self):
        config = (ROOT / "tmux.conf").read_text()
        line = next(line for line in config.splitlines() if "@resurrect-processes" in line)
        value = shlex.split(line)[3]
        elements = shlex.split(value)
        patterns = [element[1:] for element in elements if element.startswith("~")]
        self.assertEqual(len(patterns), 4)
        self.assertEqual(len([element for element in elements if element.startswith("~")]), 4)

        # tmux-resurrect feeds each ~ entry as one ERE against the complete
        # command line. Python's equivalent needs POSIX space classes lowered.
        def matches(command, pattern):
            lowered = pattern.replace("[^[:space:]]", r"\S").replace("[[:space:]]", r"\s")
            return re.search(lowered, command) is not None

        known = [
            ("/home/j/.local/bin/pidev --launch --session-id stable", "pidev"),
            ("bash /home/j/.local/bin/pi-tmux-session --session-id stable", "pi-tmux-session"),
            ("bash /home/j/.local/bin/pi-host --session-dir /tmp/pi-host-sessions --session-id stable", "pi-host"),
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
            "/home/j/.local/bin/pi-host --session-dir /tmp/pi-host-sessions",
            "/home/j/.local/bin/pi-host --session-id stable --session-dir /tmp/pi-host-sessions",
            "/home/j/.local/bin/pi-host --session-dir /tmp/pi-host-sessions --name stable",
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
for (const model of ["deepseek/deepseek-v4-flash:high", "deepseek/deepseek-v4-flash-0731:high", "DeepSeek/DeepSeek_V4_Flash:MAX", "deepseek-v4-flash", "deepseek/deepseek-v4-flash-20260730:off", "deepseek/deepseek-v4-flash-2026-07-30:high", "DeepSeekV4Flash", "deepseek/deepseekv4flash:high", "DeepSeekV4Flash20260730:max"]) {
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
        shared_types = (package_root / "src/shared/types.ts").read_text()
        self.assertIn("return Number.POSITIVE_INFINITY", shared_types)

    def test_global_agents_hash_and_removed_legacy_orchestrators(self):
        agents = (ROOT / "agent/AGENTS.md").read_bytes()
        self.assertEqual(hashlib.sha256(agents).hexdigest(), "5f1209abcdc5f44c571bb7fd18c63be99565f986bea7fbb30a7bd4f45408d8e7")
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
        fanout = (ROOT / "pi/npm/node_modules/pi-subagents/src/extension/fanout-child.ts").read_text()
        failure_events = (ROOT / "pi/npm/node_modules/pi-subagents/src/runs/background/subagent-runner.ts").read_text()
        self.assertIn("function acceptanceEventFields", failure_events)
        self.assertIn("acceptanceFailedChecks", failure_events)
        self.assertIn('process.env[SUBAGENT_CHILD_AGENT_ENV]?.trim() === "worker"', fanout)
        self.assertIn('WORKER_READ_ONLY_TOOLS = ["read", "grep", "find", "ls", "web_search", "fetch_content", "get_search_content", "source_check", "contact_supervisor", "intercom", "host_command"]', fanout)
        self.assertIn('async: true', fanout)
        self.assertIn('acceptance: WORKER_READ_ONLY_ACCEPTANCE', fanout)
        for path in agent_dir.glob("*.md"):
            text = path.read_text()
            frontmatter = text.split("---", 2)[1]
            self.assertIn("defaultContext: fresh", frontmatter, path.name)
            self.assertIn("inheritProjectContext: false", frontmatter, path.name)
            self.assertIn("inheritSkills: false", frontmatter, path.name)
            self.assertIn("subagentOnlyExtensions: __PI_AGENT_DIR__/extensions/workflow-state/index.ts", frontmatter, path.name)
            self.assertIn("__PI_AGENT_DIR__/extensions/auto-continue/index.ts", frontmatter, path.name)
            self.assertIn("__PI_AGENT_DIR__/npm/node_modules/pi-web-access/index.ts", frontmatter, path.name)
            tools = re.search(r"^tools: (.+)$", frontmatter, re.MULTILINE).group(1).split(", ")
            for web_tool in ("web_search", "fetch_content", "get_search_content", "source_check"):
                self.assertIn(web_tool, tools, path.name)
            if path.stem == "worker":
                self.assertIn("write", tools)
                self.assertIn("edit", tools)
                self.assertIn("subagent", tools)
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
        self.assertIn("Configured nesting depth is 2", added)
        self.assertNotIn("Fable mode is the default", added)
        patch_installer = (ROOT / "scripts/pi-patch-subagents").read_text()
        self.assertIn("pi-subagents-0.35.1-skill.patch", patch_installer)
        self.assertIn("39a97d5b66806fbb85fe73aac2cd96608ee70c4adf69dc17d39a6cf3fdcc32bd", patch_installer)
        installer = (ROOT / "install.sh").read_text()
        self.assertIn("for tree in extensions agents prompts themes", installer)
        self.assertIn('activate_path "$STAGING_DIR/control/$tree" "$PI_CONFIG_DIR/$tree"', installer)


if __name__ == "__main__":
    unittest.main()
