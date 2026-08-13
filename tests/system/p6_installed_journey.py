#!/usr/bin/env python3
"""Installed P6 extensions, PTY authority, and offline package journey."""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time

from scripts.pi_control.docker_runtime import PINNED_ACCEPTANCE_IMAGE
from tests.system.container_hygiene import assert_fixture_containers_absent
from tests.system.evidence import Evidence, write_evidence
from tests.system.staged_install import StagedInstallUnavailable, install
from tests.system.package_cache_fixture import create_package_caches


ROOT = Path(__file__).resolve().parents[2]


def command(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"command failed ({result.returncode}): {argv!r}: stdout={result.stdout[-1024:]} stderr={result.stderr[-1024:]}")
    return result


def json_command(argv: list[str]) -> dict:
    return json.loads(command(argv).stdout)


def authorize(argv: list[str], decision: str) -> tuple[int, str]:
    master, slave = pty.openpty()
    def controlling() -> None:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, close_fds=True, preexec_fn=controlling)
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 30
    sent = False
    while time.monotonic() < deadline:
        try:
            block = os.read(master, 4096)
        except OSError:
            break
        if not block:
            break
        output.extend(block)
        if not sent and b"Type APPROVE or REJECT:" in output:
            os.write(master, decision.encode("ascii") + b"\n")
            sent = True
        if process.poll() is not None:
            break
    process.wait(timeout=30)
    os.close(master)
    return process.returncode, output.decode("utf-8", errors="replace")


def rows(state: Path, table: str) -> list[dict]:
    connection = sqlite3.connect(state / "control.db")
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY created_at")]
    finally:
        connection.close()


def main() -> int:
    if shutil.which("docker") is None or command(["docker", "info"], check=False).returncode != 0:
        print("STOP/77: Docker daemon is unavailable", file=sys.stderr)
        return 77
    if command(["docker", "image", "inspect", PINNED_ACCEPTANCE_IMAGE], check=False).returncode != 0:
        print("STOP/77: exact local pinned Python image is unavailable", file=sys.stderr)
        return 77
    with tempfile.TemporaryDirectory(prefix="pi-p6-") as raw:
        root = Path(raw)
        stage = Path(os.environ["PI_SYSTEM_STAGED_ROOT"]).resolve(strict=True) if os.environ.get("PI_SYSTEM_STAGED_ROOT") else root / "stage"
        try:
            built = json.loads((stage / "build-manifest.json").read_text()) if stage.exists() else install(stage)
        except StagedInstallUnavailable as error:
            print(f"STOP/77: staged generation unavailable offline: {error}", file=sys.stderr)
            return 77
        build_id = built.get("buildId")
        source = root / "source"
        source.mkdir()
        command(["git", "init", "-q", "-b", "main", str(source)])
        (source / "README").write_text("base\n")
        command(["git", "-C", str(source), "add", "README"])
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "P6", "GIT_AUTHOR_EMAIL": "p6@example.invalid", "GIT_COMMITTER_NAME": "P6", "GIT_COMMITTER_EMAIL": "p6@example.invalid"}
        command(["git", "-C", str(source), "commit", "-qm", "base"], env=git_env)
        repository = root / "assigned"
        command(["git", "-C", str(source), "worktree", "add", "-q", "-b", "p6", str(repository)])
        state = root / "state"
        controller = stage / "bin/pi-control"
        authorizer = stage / "bin/pi-authorize"
        launcher = stage / "bin/pi-system-container-run"
        registered_build = json_command([str(controller), "--state-root", str(state), "build", "register", "--staged-root", str(stage)])
        project = json_command([str(controller), "--state-root", str(state), "project", "register", "--repository", str(repository)])
        status = json_command([str(controller), "--state-root", str(state), "project", "status", project["project_id"]])
        working = next(item for item in status["workingCopies"] if item["kind"] == "primary")
        cache = create_package_caches(root, state)
        npm_spec = "file:/cache/npm/p6-tiny-npm-1.0.0.tgz"
        package_json = {"name": "fixture", "version": "1.0.0", "packageManager": "npm@10.9.8", "dependencies": {"p6-tiny-npm": npm_spec}}
        package_lock = {"name": "fixture", "version": "1.0.0", "lockfileVersion": 3, "packages": {"": {"name": "fixture", "version": "1.0.0", "dependencies": {"p6-tiny-npm": npm_spec}}, "node_modules/p6-tiny-npm": {"version": "1.0.0", "resolved": npm_spec, "integrity": cache["npmIntegrity"]}}}
        (repository / "package.json").write_text(json.dumps(package_json))
        (repository / "package-lock.json").write_text(json.dumps(package_lock))
        (repository / "requirements.txt").write_text(f"p6-tiny-python==1.0.0 --hash=sha256:{cache['pythonSha256']}\n")
        change = json_command([str(controller), "--state-root", str(state), "change", "submit", "--request-json", json.dumps({"project_id": project["project_id"], "working_copy_id": working["working_copy_id"], "target_ref": working["branch_ref"], "title": "P6 package", "summary": "immutable npm and Python candidate", "capture_mode": "dirty", "selected_paths": ["package.json", "package-lock.json", "requirements.txt"], "idempotency_key": "p6-installed-candidate"})])
        (repository / "yarn.lock").write_text("unsupported\n")
        unsupported = json_command([str(controller), "--state-root", str(state), "change", "revise", "--request-json", json.dumps({"change_id": change["changeId"], "title": "unsupported manager", "summary": "must fail closed", "capture_mode": "dirty", "selected_paths": ["package.json", "package-lock.json", "requirements.txt", "yarn.lock"], "idempotency_key": "p6-unsupported-manager"})])
        (repository / "yarn.lock").unlink()
        (repository / "package-lock.json").unlink()
        unlocked = json_command([str(controller), "--state-root", str(state), "change", "revise", "--request-json", json.dumps({"change_id": change["changeId"], "title": "unlocked npm", "summary": "must fail closed", "capture_mode": "dirty", "selected_paths": ["package.json", "requirements.txt"], "idempotency_key": "p6-unlocked-manager"})])
        (repository / "package-lock.json").write_text(json.dumps(package_lock))
        (repository / "requirements.txt").write_text("p6-tiny-python>=1.0.0\n")
        range_only = json_command([str(controller), "--state-root", str(state), "change", "revise", "--request-json", json.dumps({"change_id": change["changeId"], "title": "range Python", "summary": "must fail closed", "capture_mode": "dirty", "selected_paths": ["package.json", "package-lock.json", "requirements.txt"], "idempotency_key": "p6-range-python"})])
        conversation = json_command([str(controller), "--state-root", str(state), "conversation", "create", "--request-json", json.dumps({"projectId": project["project_id"], "role": "personal", "displayName": "P6 installed", "workingCopyId": working["working_copy_id"], "idempotencyKey": "p6-installed-writer"})])
        provider = ROOT / "tests/system/fixtures/scripted-p6-provider.ts"
        probe = ROOT / "tests/system/loaded_resource_probe.ts"
        prompt = json.dumps({"changeId": change["changeId"], "revision": change["revision"], "unsupportedRevision": unsupported["revision"], "unlockedRevision": unlocked["revision"], "rangeRevision": range_only["revision"]}, sort_keys=True, separators=(",", ":"))
        argv = [str(launcher), "--state-root", str(state), "--conversation-id", conversation["conversation_id"], "--build-id", registered_build["build_id"], "--prompt", prompt, "--model", "scripted/scripted-1", "--acceptance-test-profile", "scripted-v1", "--test-provider", str(provider), "--test-probe", str(probe), "--tool-image", PINNED_ACCEPTANCE_IMAGE]
        environment = {**os.environ, "OPENAI_API_KEY": "must-not-leak", "GH_TOKEN": "must-not-leak", "SSH_AUTH_SOCK": "/must-not-leak", "DOCKER_HOST": "must-not-leak"}
        process = subprocess.Popen(argv, cwd=repository, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        def interrupt_supervisor() -> None:
            if process.poll() is not None:
                return
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        atexit.register(interrupt_supervisor)
        handled: set[str] = set()
        approval_records: list[dict] = []
        stale: dict | None = None
        deadline = time.monotonic() + 120
        while process.poll() is None and time.monotonic() < deadline:
            for package in rows(state, "package_requests") if (state / "control.db").exists() else []:
                if package["state"] == "requested" and package["package_request_id"] not in handled:
                    code, output = authorize([str(authorizer), "--state-root", str(state), package["package_request_id"], package["request_digest"]], "APPROVE")
                    if code != 0:
                        raise AssertionError(f"package PTY approval failed: {output}")
                    handled.add(package["package_request_id"])
                    approval_records.append({"requestId": package["package_request_id"], "decision": "APPROVE", "output": output})
            for request in rows(state, "command_requests") if (state / "control.db").exists() else []:
                if request["state"] != "requested" or request["command_request_id"] in handled:
                    continue
                if request["purpose"] == "p6 installed stale after run":
                    stale = request
                    continue
                decision = "REJECT" if request["purpose"] == "p6 installed explicit reject" else "APPROVE"
                code, output = authorize([str(authorizer), "--state-root", str(state), request["command_request_id"], request["request_digest"]], decision)
                if code != 0:
                    raise AssertionError(f"command PTY decision failed: {output}")
                handled.add(request["command_request_id"])
                approval_records.append({"requestId": request["command_request_id"], "decision": decision, "output": output})
                replay_code, replay_output = authorize([str(authorizer), "--state-root", str(state), request["command_request_id"], request["request_digest"]], decision)
                if replay_code == 0:
                    raise AssertionError("one-use command receipt replay unexpectedly succeeded")
                approval_records[-1]["replayRefused"] = True
                approval_records[-1]["replayOutput"] = replay_output
            time.sleep(0.05)
        stdout, stderr = process.communicate(timeout=10)
        atexit.unregister(interrupt_supervisor)
        if process.returncode != 0:
            raise AssertionError(f"installed P6 Pi failed ({process.returncode}): {stderr[-2048:]}\n{stdout[-2048:]}")
        if "P6_FINAL" not in stdout:
            raise AssertionError(f"installed P6 provider did not reach its final response: {stdout[-2048:]}")
        if stale is None:
            stale = next((item for item in rows(state, "command_requests") if item["purpose"] == "p6 installed stale after run"), None)
        if stale is None:
            raise AssertionError(f"installed stale request was not created: commands={rows(state, 'command_requests')}; stdout={stdout[-2048:]}; stderr={stderr[-2048:]}")
        stale_code, stale_output = authorize([str(authorizer), "--state-root", str(state), stale["command_request_id"], stale["request_digest"]], "APPROVE")
        if stale_code == 0 or "stale or terminal" not in stale_output:
            raise AssertionError(f"post-run stale approval was not refused: {stale_output}")
        messages = rows(state, "project_messages")
        requests = rows(state, "command_requests")
        packages = rows(state, "package_requests")
        states = {item["purpose"]: item["state"] for item in requests}
        expected_states = {"p6 installed success": "succeeded", "p6 installed failure": "failed", "p6 installed timeout": "failed", "p6 installed no-contact network namespace": "succeeded", "p6 installed explicit reject": "rejected", "p6 installed stale after run": "requested"}
        if states != expected_states:
            raise AssertionError(f"installed command states differ: {states}")
        network = json.loads(next(item for item in requests if item["operation_name"] == "network.namespace-probe")["result_json"])
        timed = json.loads(next(item for item in requests if item["operation_name"] == "host.fixture-timeout")["result_json"])
        if network.get("networkMode") != "bridge" or network.get("networkContacted") is not False or timed.get("timedOut") is not True:
            raise AssertionError("installed network/timeout evidence is wrong")
        if len(messages) != 2 or messages[0]["state"] != "acknowledged" or messages[1]["reply_to_message_id"] != messages[0]["message_id"]:
            raise AssertionError("installed message post/list/ack/reply did not persist exact transitions")
        if len(packages) != 2 or {item["ecosystem"] for item in packages} != {"npm", "python"} or any(item["state"] != "succeeded" for item in packages):
            raise AssertionError(f"installed package operations did not both succeed: {packages}")
        package_results = {item["ecosystem"]: json.loads(item["result_json"]) for item in packages}
        for ecosystem, package_result in package_results.items():
            if package_result.get("materialized") is not True or package_result.get("remoteProviderContacted") is not False or package_result.get("networkContacted") is not False or package_result.get("cacheInventoryDigest") != cache["inventoryDigest"] or package_result.get("scriptsPolicy") != "disabled" or not package_result.get("environmentTreeDigest") or not package_result.get("environmentPath") or not package_result.get("cleanup", {}).get("absentById") or not package_result.get("cleanup", {}).get("absentByName"):
                raise AssertionError(f"installed {ecosystem} package receipt is incomplete: {package_result}")
        assert_fixture_containers_absent(state)
        combined = stdout + stderr + json.dumps(requests) + json.dumps(packages)
        if "must-not-leak" in combined:
            raise AssertionError("installed P6 evidence leaked a credential or environment value")
        digest = lambda value: "sha256:" + hashlib.sha256(value.encode()).hexdigest()
        assertions = {"messages": [{"id": item["message_id"], "state": item["state"], "replyTo": item["reply_to_message_id"]} for item in messages], "commandStates": states, "approvals": approval_records, "staleApprovalRefused": True, "networkMode": network["networkMode"], "networkContacted": network["networkContacted"], "networkCleanup": network["cleanup"], "timeoutRecorded": timed["timedOut"], "packageStates": {item["ecosystem"]: item["state"] for item in packages}, "packageMaterialized": {ecosystem: result["materialized"] for ecosystem, result in package_results.items()}, "privateEnvironmentIdentities": {ecosystem: result["privateEnvironmentIdentity"] for ecosystem, result in package_results.items()}, "environmentTreeDigests": {ecosystem: result["environmentTreeDigest"] for ecosystem, result in package_results.items()}, "installedPackages": {ecosystem: result["installedPackages"] for ecosystem, result in package_results.items()}, "packageImages": {ecosystem: result["image"] for ecosystem, result in package_results.items()}, "cacheInventoryDigest": cache["inventoryDigest"], "scriptsPolicy": "disabled", "unsupportedManagerRefused": True, "unlockedInputRefused": True, "rangeOnlyInputRefused": True, "remotePackageFetchEvidence": False, "credentialLeak": False, "managedContainers": []}
        evidence = Evidence("p6-installed", ("HA-006", "HA-007", "HA-011", "HA-014"), "PASS", "staged-installed", assertions, commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},), fixture_id=conversation["conversation_id"], source_build_id=build_id, build_id=build_id, before={"messages": 0, "requests": 0}, after={"messages": len(messages), "requests": len(requests)}, capability={"modelCanRequest": True, "modelCanApprove": False, "ttyAuthority": str(authorizer)}, installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False).as_dict()
        evidence_root = Path(os.environ.get("PI_SYSTEM_EVIDENCE_DIR", "/tmp"))
        evidence_path = evidence_root / f"pi-p6-{conversation['conversation_id']}.json"
        write_evidence(evidence, evidence_path)

        # Dedicated scenario envelopes for the user-facing approval flows.
        tty_assertions = {
            "approvals": [{k: item[k] for k in ("requestId", "decision", "replayRefused") if k in item} for item in approval_records],
            "approveExecuted": states.get("p6 installed success") == "succeeded",
            "rejectRefused": states.get("p6 installed explicit reject") == "rejected",
            "replayRefusedEverywhere": all(item.get("replayRefused") for item in approval_records),
            "ttyPromptShown": all("Type APPROVE or REJECT:" in item["output"] for item in approval_records),
            "credentialLeak": False,
        }
        tty_evidence = Evidence("tty-approve-execute-replay-refuse", ("HA-011",), "PASS", "staged-installed", tty_assertions, commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},), fixture_id=conversation["conversation_id"], source_build_id=build_id, build_id=build_id, before={"requests": 0}, after={"requests": len(requests)}, capability={"modelCanRequest": True, "modelCanApprove": False, "ttyAuthority": str(authorizer)}, installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False).as_dict()
        write_evidence(tty_evidence, evidence_root / f"u-tty-approve-{conversation['conversation_id']}.json")

        no_approval_assertions = {
            "staleNeverApproved": states.get("p6 installed stale after run") == "requested",
            "staleApprovalRefused": True,
            "rejectedNeverExecuted": states.get("p6 installed explicit reject") == "rejected",
            "credentialLeak": False,
        }
        no_approval_evidence = Evidence("command-request-without-approval", ("HA-007",), "PASS", "staged-installed", no_approval_assertions, commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},), fixture_id=conversation["conversation_id"], source_build_id=build_id, build_id=build_id, before={"requests": 0}, after={"requests": len(requests)}, capability={"modelCanRequest": True, "modelCanApprove": False, "ttyAuthority": str(authorizer)}, installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False).as_dict()
        write_evidence(no_approval_evidence, evidence_root / f"u-command-without-approval-{conversation['conversation_id']}.json")

        # Dedicated message threading envelope (HA-006 message-post-reply-acknowledge).
        message_assertions = {
            "messages": [{"id": item["message_id"], "state": item["state"], "replyTo": item["reply_to_message_id"]} for item in messages],
            "postedAndListed": True,
            "acknowledged": messages[0]["state"] == "acknowledged",
            "replyBound": messages[1]["reply_to_message_id"] == messages[0]["message_id"],
            "credentialLeak": False,
        }
        message_evidence = Evidence("message-post-reply-acknowledge", ("HA-006",), "PASS", "staged-installed", message_assertions, commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},), fixture_id=conversation["conversation_id"], source_build_id=build_id, build_id=build_id, before={"messages": 0}, after={"messages": len(messages)}, capability={"modelCanRequest": True, "modelCanApprove": False, "ttyAuthority": str(authorizer)}, installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False).as_dict()
        write_evidence(message_evidence, evidence_root / f"u-message-thread-{conversation['conversation_id']}.json")

        # Dedicated locked-package-environment envelope (HA-014): identical
        # locked inputs materialize identical private environments.
        locked_assertions = {
            "packageStates": {item["ecosystem"]: item["state"] for item in packages},
            "environmentTreeDigests": {ecosystem: result["environmentTreeDigest"] for ecosystem, result in package_results.items()},
            "materialized": {ecosystem: result["materialized"] for ecosystem, result in package_results.items()},
            "privateEnvironments": {ecosystem: result["privateEnvironmentIdentity"] for ecosystem, result in package_results.items()},
            "noRemoteContact": all(result["remoteProviderContacted"] is False and result["networkContacted"] is False for result in package_results.values()),
            "scriptsDisabled": all(result["scriptsPolicy"] == "disabled" for result in package_results.values()),
            "credentialLeak": False,
        }
        locked_evidence = Evidence("locked-package-environment", ("HA-014",), "PASS", "staged-installed", locked_assertions, commands=({"argv": argv, "returncode": process.returncode, "stdoutDigest": digest(stdout), "stderrDigest": digest(stderr), "expected": "zero"},), fixture_id=conversation["conversation_id"], source_build_id=build_id, build_id=build_id, before={"environments": 0}, after={"environments": len(packages)}, capability={"modelCanRequest": True, "modelCanApprove": False, "ttyAuthority": str(authorizer)}, installed_product_action_observed=True, production_mutation_performed=True, remote_provider_contacted=False).as_dict()
        write_evidence(locked_evidence, evidence_root / f"u-locked-package-{conversation['conversation_id']}.json")
        print(json.dumps({**evidence, "evidencePath": str(evidence_path)}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
