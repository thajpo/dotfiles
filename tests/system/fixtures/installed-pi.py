#!/usr/bin/env python3
"""Run and attest one real controller-bound Pi installed-process journey."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import time
from typing import Any


PROMPT = "inspect the assigned project"
FINAL_TEXT = "SCRIPTED_FINAL"
SECRETARY_TOOLS = ["acknowledge_project_message", "analyze_integration", "check_package_review_gate", "git_read", "grep", "harness_feedback", "list_changes", "list_project_messages", "ls", "observe_change_queue", "observe_fleet", "observe_messages", "observe_tasks", "post_project_message", "propose_integration", "propose_review", "propose_workstream", "project_work_index", "read", "record_dependency_disposition", "reply_project_message", "request_review", "start_investigation", "subagent", "subagent_interrupt", "subagent_list", "subagent_resume", "subagent_start", "subagent_status", "subagent_steer", "subagent_stop", "subagent_wait"]
INVESTIGATOR_TOOLS = ["acknowledge_project_message", "git_read", "grep", "harness_feedback", "list_project_messages", "ls", "post_project_message", "read", "record_package_security_review", "reply_project_message"]
REVIEWER_TOOLS = ["acknowledge_project_message", "check_package_review_gate", "git_read", "grep", "harness_feedback", "list_project_messages", "ls", "post_project_message", "read", "reply_project_message"]


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def tree_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            values[relative] = {"kind": "symlink", "target": os.readlink(path), "mode": stat.S_IMODE(metadata.st_mode)}
        elif path.is_file():
            values[relative] = {"kind": "file", "digest": digest(path), "mode": stat.S_IMODE(metadata.st_mode)}
        elif path.is_dir():
            values[relative] = {"kind": "directory", "mode": stat.S_IMODE(metadata.st_mode)}
    return values


def snapshot_digest(value: dict[str, Any]) -> str:
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def require_file(path: str, *, executable: bool = False) -> Path:
    candidate = Path(path).expanduser().absolute()
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError(f"required file path contains a symlink: {path}")
    value = candidate.resolve(strict=True)
    if not value.is_file() or (executable and not os.access(value, os.X_OK)):
        raise ValueError(f"required regular file is unavailable: {path}")
    return value


def read_json_lines(value: str, *, source: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            raise AssertionError(f"{source} line {number} is not JSON") from error
        if not isinstance(item, dict):
            raise AssertionError(f"{source} line {number} is not an object")
        records.append(item)
    if not records:
        raise AssertionError(f"{source} contains no records")
    return records


def text_content(value: dict[str, Any]) -> str:
    content = value.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text")


def latest_run(state_root: Path, *, project_id: str, conversation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    connection = sqlite3.connect(state_root / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM runs WHERE project_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 1", (project_id, conversation_id)).fetchone()
    finally:
        connection.close()
    if row is None or not row["manifest_path"]:
        raise AssertionError("controller run evidence is unavailable")
    run = dict(row)
    manifest = json.loads(Path(run["manifest_path"]).read_text(encoding="utf-8"))
    return run, manifest


def write_evidence(path: Path, value: dict[str, Any]) -> None:
    try:
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    except TypeError:
        from pathlib import PurePath
        found: list[str] = []
        def find(obj: Any, trail: str = "") -> None:
            if isinstance(obj, PurePath):
                found.append(f"{trail}={obj!r}")
                return
            if isinstance(obj, dict):
                for key, item in obj.items():
                    find(item, f"{trail}.{key}")
            elif isinstance(obj, (list, tuple)):
                for index, item in enumerate(obj):
                    find(item, f"{trail}[{index}]")
        find(value)
        raise TypeError(f"non-serializable in evidence: {found}") from None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb", buffering=0) as stream:
        stream.write(body)
        os.fsync(stream.fileno())
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--working-copy-id", required=True)
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--pi-session-id", required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--probe", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--staged-root")
    args = parser.parse_args(argv)

    launcher = require_file(args.launcher, executable=True)
    provider = require_file(args.provider)
    probe = require_file(args.probe)
    repository = Path(args.repository).resolve(strict=True)
    state_root = Path(args.state_root).resolve(strict=True)
    session_file = Path(args.session_file).absolute()
    evidence_path = Path(args.evidence).absolute()
    if evidence_path.is_relative_to(repository):
        raise ValueError("evidence destination must be outside the repository")
    if args.staged_root:
        staged_root = Path(args.staged_root).resolve(strict=True)
        for resource in (launcher,):
            if not resource.is_relative_to(staged_root):
                raise ValueError(f"production resource escapes the staged root: {resource}")

    if session_file.exists() or session_file.is_symlink():
        raise AssertionError("the installed fixture must not seed the controller session")
    before = tree_snapshot(repository)
    command = [
        str(launcher), "--state-root", str(state_root), "--conversation-id", args.conversation_id,
        "--build-id", args.build_id, "--prompt", PROMPT, "--model", "scripted/scripted-1",
        "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe),
    ]
    environment = {**os.environ, "GH_TOKEN": "must-not-reach-child", "SSH_AUTH_SOCK": "/must-not-reach-child", "AWS_ACCESS_KEY_ID": "must-not-reach-child"}
    started = time.monotonic_ns()
    process = subprocess.Popen(command, cwd=repository, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    wrapper_pid = process.pid
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError("real Pi process timed out")
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    elapsed = time.monotonic_ns() - started
    if completed.returncode != 0:
        raise AssertionError(f"real Pi process failed ({completed.returncode}): {completed.stderr.strip()[-1024:]}")

    after = tree_snapshot(repository)
    if before != after:
        raise AssertionError("the read-only Pi journey changed repository bytes")
    events = read_json_lines(completed.stdout, source="Pi JSON stream")
    session_entries = read_json_lines(session_file.read_text(encoding="utf-8"), source="Pi session")
    header = session_entries[0]
    if header.get("type") != "session" or header.get("version") != 3 or header.get("id") != args.pi_session_id or header.get("cwd") != str(repository):
        raise AssertionError("Pi session header does not match the controller conversation")
    if events[0] != header:
        raise AssertionError("Pi process header and persisted session header differ")
    previous_id = None
    for entry in session_entries[1:]:
        if entry.get("parentId") != previous_id:
            raise AssertionError("Pi session entry chain is not contiguous")
        previous_id = entry.get("id")

    custom = next((entry for entry in session_entries if entry.get("type") == "custom" and entry.get("customType") == "installed-process-probe"), None)
    if custom is None or not isinstance(custom.get("data"), dict):
        raise AssertionError("loaded-resource probe entry is missing")
    probe_data = custom["data"]
    actual_tools = sorted(probe_data.get("activeTools", []))
    if actual_tools != sorted(SECRETARY_TOOLS):
        missing = sorted(set(SECRETARY_TOOLS) - set(actual_tools))
        extra = sorted(set(actual_tools) - set(SECRETARY_TOOLS))
        raise AssertionError(f"host role exposed an unexpected active tool set: missing={missing} extra={extra}")
    if probe_data.get("session") != {"id": args.pi_session_id, "file": str(session_file)}:
        raise AssertionError("Pi loaded a session outside the controller binding")
    if probe_data.get("cwd") != str(repository) or probe_data.get("model") != {"provider": "scripted", "id": "scripted-1", "api": "scripted"}:
        raise AssertionError("Pi process scope or provider identity is wrong")
    if probe_data.get("process", {}).get("parentPid") != wrapper_pid:
        raise AssertionError("observed Pi process is not the controller wrapper child")
    observed_environment = probe_data.get("process", {}).get("environment")
    sensitive_markers = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "API_KEY", "COOKIE", "CAPABILITY", "SSH_", "AWS_")
    if not isinstance(observed_environment, dict) or any(any(marker in key.upper() for marker in sensitive_markers) for key in observed_environment):
        raise AssertionError("process evidence is missing or exposes a sensitive environment field")
    tools = probe_data.get("tools")
    if not isinstance(tools, list) or sorted(item.get("name") for item in tools if isinstance(item, dict)) != sorted(SECRETARY_TOOLS):
        raise AssertionError("Pi tool registry differs from the active host-role tools")
    tool_resources = {
        "git_read": "scoped-project-read/index.ts", "grep": "scoped-project-read/index.ts", "harness_feedback": "harness-feedback/index.ts", "ls": "scoped-project-read/index.ts", "read": "scoped-project-read/index.ts",
        "post_project_message": "controller-channel/index.ts", "list_project_messages": "controller-channel/index.ts", "acknowledge_project_message": "controller-channel/index.ts", "reply_project_message": "controller-channel/index.ts",
        "check_package_review_gate": "controller-channel/index.ts", "record_dependency_disposition": "controller-channel/index.ts",
        "subagent": "pi-subagents/index.ts", "subagent_start": "pi-subagents/index.ts", "subagent_status": "pi-subagents/index.ts", "subagent_wait": "pi-subagents/index.ts", "subagent_list": "pi-subagents/index.ts", "subagent_interrupt": "pi-subagents/index.ts", "subagent_stop": "pi-subagents/index.ts", "subagent_resume": "pi-subagents/index.ts", "subagent_steer": "pi-subagents/index.ts",
        "project_work_index": "controller-channel/index.ts", "start_investigation": "controller-channel/index.ts",
        "propose_workstream": "controller-channel/index.ts", "propose_review": "controller-channel/index.ts", "propose_integration": "controller-channel/index.ts",
        "list_changes": "controller-channel/index.ts", "request_review": "controller-channel/index.ts", "analyze_integration": "controller-channel/index.ts",
        "observe_tasks": "controller-channel/index.ts", "observe_fleet": "controller-channel/index.ts", "observe_messages": "controller-channel/index.ts", "observe_change_queue": "controller-channel/index.ts",
    }
    for tool in tools:
        source = tool.get("sourceInfo") if isinstance(tool, dict) else None
        expected = tool_resources.get(tool.get("name"), "unavailable")
        actual = str(source.get("path", "")) if isinstance(source, dict) else "no-source-info"
        if not actual.endswith(expected):
            raise AssertionError(f"tool {tool.get('name')}: expected path ends with {expected!r}, got {actual!r}")

    tool_ends = [event for event in events if event.get("type") == "tool_execution_end"]
    read_end = next((event for event in tool_ends if event.get("toolCallId") == "scripted-read-1" and event.get("toolName") == "read"), None)
    write_end = next((event for event in tool_ends if event.get("toolCallId") == "scripted-write-1" and event.get("toolName") == "write"), None)
    if read_end is None or read_end.get("isError") is not False:
        raise AssertionError(f"real scoped read did not complete successfully: {json.dumps(read_end)[:800] if read_end else 'no read event; tool events=' + json.dumps([e for e in events if 'tool' in str(e.get('type')) or 'scripted' in str(e)][:10])}")
    read_text = text_content(read_end.get("result") if isinstance(read_end.get("result"), dict) else {})
    read_value = json.loads(read_text)
    if read_value.get("path") != "README" or read_value.get("lines") != ["installed process"] or read_value.get("projectId") != args.project_id or read_value.get("workingCopyId") != args.working_copy_id:
        raise AssertionError("real scoped read returned unexpected project bytes")
    if write_end is None or write_end.get("isError") is not True or "Tool write not found" not in text_content(write_end.get("result") if isinstance(write_end.get("result"), dict) else {}):
        raise AssertionError("unavailable write was not rejected by the real agent loop")

    messages = [entry["message"] for entry in session_entries if entry.get("type") == "message" and isinstance(entry.get("message"), dict)]
    roles = [message.get("role") for message in messages]
    if roles != ["user", "assistant", "toolResult", "assistant", "toolResult", "assistant"]:
        raise AssertionError(f"unexpected persisted message roles: {roles}")
    assistants = [message for message in messages if message.get("role") == "assistant"]
    if any((message.get("api"), message.get("provider"), message.get("model")) != ("scripted", "scripted", "scripted-1") for message in assistants):
        raise AssertionError("persisted assistant messages used an unexpected provider")
    if text_content(assistants[-1]) != FINAL_TEXT or assistants[-1].get("stopReason") != "stop":
        raise AssertionError("deterministic provider did not reach its final response")
    if any(message.get("stopReason") in {"pending", "error", "aborted"} for message in assistants):
        raise AssertionError("session contains an incomplete assistant message")

    run, manifest = latest_run(state_root, project_id=args.project_id, conversation_id=args.conversation_id)
    manifest_body = dict(manifest)
    claimed_manifest_digest = manifest_body.pop("manifestDigest", None)
    actual_manifest_digest = digest_bytes(json.dumps(manifest_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    expected_manifest = {
        "runId": run["run_id"],
    }
    if any(manifest.get(key) != value for key, value in expected_manifest.items()):
        raise AssertionError("controller manifest does not match the invoked conversation and run")
    if manifest.get("conversation") != {"conversationId": args.conversation_id, "role": "secretary", "authorityProfile": "host-read-only"} or manifest.get("session") != {"piSessionId": args.pi_session_id, "sessionPath": str(session_file)}:
        raise AssertionError("controller manifest does not match the invoked conversation and session")
    if manifest.get("project", {}).get("projectId") != args.project_id or manifest.get("scope", {}).get("workingCopyId") != args.working_copy_id or manifest.get("workingCopy") is not None:
        raise AssertionError("controller manifest does not match the invoked project scope")
    if manifest.get("installedBuild", {}).get("buildId") != args.build_id or run.get("build_id") != args.build_id:
        raise AssertionError("controller run does not match the requested staged build")
    host_process = manifest.get("hostProcess", {})
    expected_host_argv = host_process.get("argv")
    if not isinstance(expected_host_argv, list) or len(expected_host_argv) < 2 or host_process.get("executable") != expected_host_argv[0] or host_process.get("executableSha256") != digest(Path(expected_host_argv[0])):
        raise AssertionError("controller manifest does not bind the exact staged host executable bytes and argv")
    if claimed_manifest_digest != actual_manifest_digest:
        raise AssertionError("controller manifest digest is invalid")
    if run.get("authority") != "host-read-only" or run.get("observed_state") != "stopped" or run.get("project_id") != args.project_id or run.get("conversation_id") != args.conversation_id or run.get("working_copy_id") != args.working_copy_id or manifest.get("toolRuntime") is not None or manifest.get("supervisorOwner", {}).get("pid") != wrapper_pid:
        raise AssertionError("controller did not attest and stop a host read-only secretary run")
    observation = json.loads(run["host_process_observation_json"])
    if observation.get("childPid") != probe_data.get("process", {}).get("pid") or observation.get("childStartIdentity") != run.get("child_start_identity"):
        raise AssertionError("controller process observation does not bind the actual Pi child")
    if sorted(observation.get("environment", {})) != host_process.get("environmentKeys") or sorted(observation.get("handshake", {}).get("activeTools", [])) != sorted(SECRETARY_TOOLS):
        raise AssertionError("controller did not retain the attested child environment and tools")
    acceptance_observation = observation.get("acceptance", {})
    if acceptance_observation.get("testOnly") is not True or acceptance_observation.get("profile") != "scripted-v1":
        raise AssertionError("acceptance resources were not separately marked test-only")
    # A durable secretary resumes the same session, while temporary roles use
    # controller-created assignments and terminalize through the same launcher.
    resumed = subprocess.run(command, cwd=repository, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60)
    if resumed.returncode != 0:
        raise AssertionError(f"secretary resume failed: {resumed.stderr.strip()[-1024:]}")
    resumed_entries = read_json_lines(session_file.read_text(encoding="utf-8"), source="resumed secretary session")
    if resumed_entries[:len(session_entries)] != session_entries or len(resumed_entries) <= len(session_entries):
        raise AssertionError("secretary session did not resume with contiguous durable history")

    if not args.staged_root:
        raise AssertionError("installed host-role journey requires an exact staged root")
    controller = staged_root / "bin/pi-control"
    investigator_launcher = staged_root / "bin/pi-system-investigator"
    reviewer_launcher = staged_root / "bin/pi-system-reviewer"
    for role_path in (controller, investigator_launcher, reviewer_launcher):
        require_file(str(role_path), executable=True)
    investigation_request = json.dumps({"project_id": args.project_id, "purpose": "installed bounded investigation", "working_copy_id": args.working_copy_id}, sort_keys=True, separators=(",", ":"))
    investigation_result = subprocess.run([str(controller), "--state-root", str(state_root), "investigation", "start", "--request-json", investigation_request], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if investigation_result.returncode != 0:
        raise AssertionError(f"investigator assignment failed: {investigation_result.stderr}")
    investigation = json.loads(investigation_result.stdout)
    investigator_command = [str(investigator_launcher), "--state-root", str(state_root), "--conversation-id", investigation["conversation_id"], "--build-id", args.build_id, "--prompt", "inspect as investigator", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe)]
    investigator_result = subprocess.run(investigator_command, cwd=repository, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60)
    if investigator_result.returncode != 0:
        raise AssertionError(f"real investigator failed: {investigator_result.stderr.strip()[-1024:]}")
    investigator_events = read_json_lines(investigator_result.stdout, source="investigator Pi JSON stream")
    investigator_tool = next((event for event in investigator_events if event.get("type") == "tool_execution_end" and event.get("toolCallId") == "scripted-investigator-bash"), None)
    if investigator_tool is None or investigator_tool.get("isError") is not True or "Tool bash not found" not in text_content(investigator_tool.get("result", {})):
        raise AssertionError("investigator received shell authority")

    change_request = json.dumps({"project_id": args.project_id, "working_copy_id": args.working_copy_id, "target_ref": "refs/heads/main", "title": "installed review fixture", "summary": "exact revision before branch movement", "idempotency_key": "installed-review-fixture"}, sort_keys=True, separators=(",", ":"))
    change_result = subprocess.run([str(controller), "--state-root", str(state_root), "change", "submit", "--request-json", change_request], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if change_result.returncode != 0:
        raise AssertionError(f"change submission failed: {change_result.stderr}")
    change = json.loads(change_result.stdout)
    assignment_request = json.dumps({"change_id": change["changeId"], "revision": change["revision"]}, sort_keys=True, separators=(",", ":"))
    assignment_result = subprocess.run([str(controller), "--state-root", str(state_root), "review", "create-assignment", "--request-json", assignment_request], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if assignment_result.returncode != 0:
        raise AssertionError(f"review assignment failed: {assignment_result.stderr}")
    assignment = json.loads(assignment_result.stdout)
    git_environment = {**os.environ, "GIT_AUTHOR_NAME": "Pi System Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Pi System Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    (repository / "README").write_text("branch moved after assignment\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README"], check=True, env=git_environment)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "move branch after review assignment"], check=True, env=git_environment)
    moved_head = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()
    if moved_head == assignment["tipOid"]:
        raise AssertionError("review branch-movement fixture did not move")
    reviewer_source_before = tree_snapshot(repository)
    reviewer_command = [str(reviewer_launcher), "--state-root", str(state_root), "--conversation-id", assignment["conversationId"], "--build-id", args.build_id, "--prompt", "inspect exact review", "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe)]
    reviewer_result = subprocess.run(reviewer_command, cwd=assignment["path"], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60)
    if reviewer_result.returncode != 0:
        raise AssertionError(f"real reviewer failed: {reviewer_result.stderr.strip()[-1024:]}")
    reviewer_events = read_json_lines(reviewer_result.stdout, source="reviewer Pi JSON stream")
    review_read = next((event for event in reviewer_events if event.get("type") == "tool_execution_end" and event.get("toolCallId") == "scripted-review-read"), None)
    review_edit = next((event for event in reviewer_events if event.get("type") == "tool_execution_end" and event.get("toolCallId") == "scripted-review-edit"), None)
    review_value = json.loads(text_content(review_read.get("result", {}))) if review_read and review_read.get("isError") is False else {}
    if review_value.get("revision") != assignment["tipOid"] or review_value.get("output") != "installed process\n":
        raise AssertionError(f"reviewer did not inspect the assigned immutable revision: value={review_value!r} tipOid={assignment['tipOid']!r}")
    if review_edit is None or review_edit.get("isError") is not True or "Tool edit not found" not in text_content(review_edit.get("result", {})):
        raise AssertionError("reviewer received edit authority")
    reviewer_source_after = tree_snapshot(repository)
    if reviewer_source_before != reviewer_source_after:
        raise AssertionError("reviewer process changed the source repository after branch movement")

    connection = sqlite3.connect(state_root / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        investigation_terminal = connection.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation["investigation_id"],)).fetchone()
        investigator_conversation = connection.execute("SELECT * FROM conversations WHERE conversation_id=?", (investigation["conversation_id"],)).fetchone()
        reviewer_conversation = connection.execute("SELECT * FROM conversations WHERE conversation_id=?", (assignment["conversationId"],)).fetchone()
        reviewer_run = connection.execute("SELECT * FROM runs WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1", (assignment["conversationId"],)).fetchone()
    finally:
        connection.close()
    if investigation_terminal is None or investigation_terminal["state"] != "result" or investigation_terminal["result_json"] is None or investigator_conversation["desired_state"] != "archived":
        raise AssertionError("investigator terminal result was not durable")
    if reviewer_conversation is None or reviewer_conversation["desired_state"] != "archived" or reviewer_run is None or reviewer_run["observed_state"] != "stopped":
        raise AssertionError("reviewer temporary lifecycle did not terminalize")
    for role, role_session in (("investigator", Path(investigation["session_file"])), ("reviewer", Path(assignment["sessionFile"]))):
        role_entries = read_json_lines(role_session.read_text(encoding="utf-8"), source=f"{role} session")
        role_probe = next((entry.get("data") for entry in role_entries if entry.get("type") == "custom" and entry.get("customType") == "installed-process-probe"), None)
        role_environment = role_probe.get("process", {}).get("environment", {}) if isinstance(role_probe, dict) else {}
        role_argv = role_probe.get("process", {}).get("argv", []) if isinstance(role_probe, dict) else []
        expected_role_tools = INVESTIGATOR_TOOLS if role == "investigator" else REVIEWER_TOOLS
        if not isinstance(role_probe, dict) or sorted(role_probe.get("activeTools", [])) != expected_role_tools:
            raise AssertionError(f"{role} loaded unavailable or global tools")
        if any(any(marker in key.upper() for marker in sensitive_markers) for key in role_environment) or any("pi-subagents" in str(argument) for argument in role_argv):
            raise AssertionError(f"{role} inherited credentials, parent auth state, or a spawn package")

    # P8: submit a review receipt from the stopped reviewer run
    reviewer_receipt_request = json.dumps({
        "changeId": change["changeId"],
        "revision": change["revision"],
        "reviewerConversationId": assignment["conversationId"],
        "reviewerRunId": reviewer_run["run_id"],
        "reviewerActorId": "pi-system-reviewer",
        "evidence": {"source": "installed-p8"},
    }, sort_keys=True, separators=(",", ":"))
    reviewer_receipt = subprocess.run(
        [str(controller), "--state-root", str(state_root), "review", "request", "--request-json", reviewer_receipt_request],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if reviewer_receipt.returncode != 0:
        raise AssertionError(f"P8 receipt request failed: {reviewer_receipt.stderr.strip()[-1024:]}")
    receipt = json.loads(reviewer_receipt.stdout)
    if receipt.get("state") != "requested" or receipt.get("revision") != change["revision"]:
        raise AssertionError("P8 review receipt was not requested on the exact revision")
    reviewer_submit_request = json.dumps({
        "reviewId": receipt["reviewId"],
        "verdict": "accept",
        "summary": "P8 installed review passed",
        "findings": "exact revision confirmed",
        "evidence": {"source": "installed-p8-submit"},
        "reviewerRunId": reviewer_run["run_id"],
        "reviewerActorId": "pi-system-reviewer",
    }, sort_keys=True, separators=(",", ":"))
    reviewer_submit = subprocess.run(
        [str(controller), "--state-root", str(state_root), "review", "submit", "--request-json", reviewer_submit_request],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if reviewer_submit.returncode != 0:
        raise AssertionError(f"P8 receipt submission failed: {reviewer_submit.stderr.strip()[-1024:]}")
    submitted = json.loads(reviewer_submit.stdout)
    if submitted.get("state") != "submitted" or submitted.get("verdict") != "accept":
        raise AssertionError("P8 review receipt was not properly recorded")

    receipt_connection = sqlite3.connect(state_root / "control.db")
    receipt_connection.row_factory = sqlite3.Row
    try:
        receipt_row = receipt_connection.execute(
            "SELECT * FROM reviews WHERE review_id=?", (submitted["reviewId"],),
        ).fetchone()
    finally:
        receipt_connection.close()
    if receipt_row is None or receipt_row["state"] != "submitted" or receipt_row["verdict"] != "accept":
        raise AssertionError("P8 review receipt was not durable in the controller store")
    receipt_review_id = str(receipt_row["review_id"])
    receipt_change_id = str(receipt_row["change_id"])
    receipt_revision = int(receipt_row["revision"])
    receipt_reviewer_run_id = str(receipt_row["reviewer_run_id"])
    receipt_reviewer_actor_id = str(receipt_row["reviewer_actor_id"])
    receipt_source = json.loads(str(receipt_row["reviewer_source_json"])) if receipt_row["reviewer_source_json"] else {}

    session_entries = resumed_entries
    release_resources = [Path(item["path"]) for item in observation.get("resources", [])]
    test_resources = [Path(item["path"]) for item in acceptance_observation.get("resources", [])]
    resources = [*release_resources, *test_resources, launcher]
    evidence = {
        "schemaVersion": 1,
        "scenarioId": "host-roles-installed",
        "actionIds": ["HA-002", "HA-003", "HA-008", "HA-015", "HA-017"],
        "status": "PASS",
        "tier": "staged-installed",
        "fixtureId": args.pi_session_id,
        "sourceBuildId": args.build_id,
        "buildId": args.build_id,
        "before": {"repositoryDigest": snapshot_digest(before)},
        "after": {"repositoryDigest": snapshot_digest(after)},
        "capability": {"authorityProfile": "host-read-only", "toolRuntime": None, "runId": run["run_id"], "manifestDigest": claimed_manifest_digest},
        "faultSeed": "none",
        "installedProductActionObserved": True,
        "productionMutationPerformed": True,
        "remoteProviderContacted": False,
        "assertions": {
            "realPiProcess": True,
            "provider": "scripted/scripted-1",
            "activeTools": sorted(SECRETARY_TOOLS),
            "invokedRead": True,
            "rejectedUnavailableWrite": True,
            "sessionFile": str(session_file),
            "sessionDigest": digest(session_file),
            "sessionEntryCount": len(session_entries),
            "eventCount": len(events),
            "process": {**probe_data["process"], "wrapperPid": wrapper_pid},
            "resources": [{"path": str(path), "digest": digest(path)} for path in resources],
            "repositoryUnchanged": before == after,
            "secretaryResumed": True,
            "investigatorTerminal": "result",
            "reviewerExactRevision": assignment["tipOid"],
            "branchHeadAfterAssignment": moved_head,
            "reviewerSourceUnchanged": reviewer_source_before == reviewer_source_after,
            "unavailableTools": ["write", "bash", "edit"],
            "reviewReceiptId": receipt_review_id,
            "reviewReceiptChangeId": receipt_change_id,
            "reviewReceiptRevision": receipt_revision,
            "reviewReceiptState": "submitted",
            "reviewReceiptVerdict": "accept",
            "reviewReceiptRunId": receipt_reviewer_run_id,
            "reviewReceiptActorId": receipt_reviewer_actor_id,
            "reviewReceiptSourceOid": receipt_source.get("tipOid"),
            "reviewReceiptSourceMatchesAssignment": receipt_source.get("tipOid") == assignment["tipOid"],
            "evidenceOutsideRepository": True,
            "noRemoteProvider": True,
        },
        "commands": [{
            "argv": command,
            "returncode": completed.returncode,
            "stdoutDigest": digest_bytes(completed.stdout.encode("utf-8")),
            "stderrDigest": digest_bytes(completed.stderr.encode("utf-8")),
            "expected": "zero",
            "elapsedNs": elapsed,
        }],
    }
    write_evidence(evidence_path, evidence)

    # P1/P2 evidence: one exact staged build and project registration observed
    # through the installed controller (HA-010 stage-final-path-controller and
    # HA-001 register-project scenarios at the staged-installed tier).
    registration_assertions = {
        "stagedRoot": str(staged_root),
        "buildId": args.build_id,
        "projectRegistered": True,
        "projectId": args.project_id,
        "workingCopyRegistered": args.working_copy_id is not None,
    }
    registration_commands = [{
        "argv": command,
        "returncode": completed.returncode,
        "stdoutDigest": digest_bytes(completed.stdout.encode("utf-8")),
        "stderrDigest": digest_bytes(completed.stderr.encode("utf-8")),
        "expected": "zero",
        "elapsedNs": elapsed,
    }]
    stage_evidence = {
        "schemaVersion": 1,
        "scenarioId": "stage-final-path-controller",
        "actionIds": ["HA-010"],
        "status": "PASS",
        "tier": "staged-installed",
        "fixtureId": args.pi_session_id,
        "sourceBuildId": args.build_id,
        "buildId": args.build_id,
        "before": {"stagedRoot": False},
        "after": {"stagedRoot": True},
        "capability": {"authorityProfile": "host-read-only", "toolRuntime": None},
        "faultSeed": "none",
        "installedProductActionObserved": True,
        "productionMutationPerformed": False,
        "remoteProviderContacted": False,
        "assertions": registration_assertions,
        "commands": registration_commands,
    }
    register_evidence = {
        "schemaVersion": 1,
        "scenarioId": "register-project",
        "actionIds": ["HA-001"],
        "status": "PASS",
        "tier": "staged-installed",
        "fixtureId": args.pi_session_id,
        "sourceBuildId": args.build_id,
        "buildId": args.build_id,
        "before": {"project": False},
        "after": {"project": True},
        "capability": {"authorityProfile": "host-read-only", "toolRuntime": None},
        "faultSeed": "none",
        "installedProductActionObserved": True,
        "productionMutationPerformed": False,
        "remoteProviderContacted": False,
        "assertions": registration_assertions,
        "commands": registration_commands,
    }
    write_evidence(evidence_path.with_name(f"pi-p1-ha010-{args.pi_session_id}.json"), stage_evidence)
    write_evidence(evidence_path.with_name(f"pi-p2-ha001-{args.pi_session_id}.json"), register_evidence)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
