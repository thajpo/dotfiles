#!/usr/bin/env python3
"""Shared resolver for the greenfield-backed Pi surface commands.

Resolves the active greenfield install, controller, and launchers; registers
the active build with the controller state; and ensures projects and role
conversations exist. Surface wrappers (pi-start, pi-restart, pisec,
pi-personal, pidev) call this helper and launch the pi-system-* binaries.

The daily launch model is one-shot and controller-bound: each prompt is one
run; conversation continuity comes from the durable session file, so a
follow-up turn is a relaunch of the same conversation id with a new prompt.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

DATA_ROOT = Path(os.environ.get("PI_SYSTEM_DATA_ROOT", "~/.local/share/pi-system")).expanduser()
STATE_ROOT = Path(os.environ.get("PI_SYSTEM_STATE_ROOT", "~/.local/state/pi-system")).expanduser()
MODEL = os.environ.get("PI_SYSTEM_MODEL", "deepseek/deepseek-v4-flash")
TOOL_IMAGE = os.environ.get(
    "PI_TOOL_IMAGE",
    "python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0",
)
CONTROL_HELPER = Path(os.environ.get("PI_SYSTEM_CONTROL_ROOT", str(DATA_ROOT))).expanduser()
SURFACE_STAGE = Path(os.environ.get("PI_SYSTEM_SURFACE_STAGE", "~/.local/share/pi-system/surface-stage")).expanduser()
SURFACE_MARKER = SURFACE_STAGE.with_name(SURFACE_STAGE.name + ".marker")
# The surface stage is built from the dotfiles repository. When run from the
# repo, resolve it structurally; the installed copy requires PI_SURFACE_REPO.
repo_candidate = Path(__file__).resolve().parents[1]
if not (repo_candidate / "bin" / "pi-install").is_file():
    for candidate in (Path.home() / "dotfiles", Path(os.environ.get("PI_PERSONAL_DOTFILES_DIR", ""))):
        if candidate.is_dir() and (candidate / "bin" / "pi-install").is_file():
            repo_candidate = candidate
            break
REPO_ROOT = Path(os.environ.get("PI_SURFACE_REPO", str(repo_candidate))).expanduser()


class SurfaceError(RuntimeError):
    pass


def _run(argv: list[str]) -> dict | list | str:
    result = subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise SurfaceError(f"{argv[0]} failed: {result.stderr.strip()[-1024:]}")
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def _staged_repo_digest() -> str:
    """Digest of the repo source the surface stage was built from.

    The stage manifest's own sourceDigest is computed over the staged tree and
    cannot be compared against the working tree. Instead we record a marker
    file with the working-tree digest at build time and compare against that.

    The full-tree digest computation is expensive (it hashes every release
    file). For the freshness check the repository HEAD plus the porcelain
    status fingerprint is a fast, sound proxy: the stage is rebuilt from a
    committed tree, so any change to HEAD or the working tree invalidates it.
    """
    if _staged_repo_digest.cache is not None:
        return _staged_repo_digest.cache
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=False, capture_output=True, text=True,
    )
    if head.returncode != 0:
        raise SurfaceError("the surface repository is not a Git checkout")
    status = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        check=False, capture_output=True, text=True,
    )
    if status.returncode != 0:
        raise SurfaceError("cannot inspect the surface repository status")
    _staged_repo_digest.cache = "head:" + head.stdout.strip() + "|dirty:" + status.stdout.strip()
    return _staged_repo_digest.cache


_staged_repo_digest.cache: str | None = None


def _registered_build(stage_root: str) -> str | None:
    """Return the registered build id for a stage when its row already exists.

    The stage was fully verified at build and registration time; the marker
    plus this row lookup is enough to reuse it without re-hashing the tree.
    """
    installed = Path(__file__).resolve().parent / "pi_control"
    if installed.is_dir():
        sys.path.insert(0, str(installed.parent))
        from pi_control.greenfield_store import GreenfieldStore
    else:
        sys.path.insert(0, str(REPO_ROOT))
        from scripts.pi_control.greenfield_store import GreenfieldStore
    try:
        with GreenfieldStore(STATE_ROOT) as store:
            row = store.conn.execute(
                "SELECT build_id, build_manifest_path FROM installed_builds WHERE status='staged' AND build_manifest_path=?",
                (str(Path(stage_root) / "build-manifest.json"),),
            ).fetchone()
    except Exception:
        return None
    return row["build_id"] if row is not None else None


def ensure_surface_stage() -> dict:
    """Build and register one persistent surface stage from the current repo.

    Staging is deterministic: the same source produces the same build. The
    stage is refreshed only when the repo source moved, and registered with the
    controller so launches bind a verified generation.
    """
    marker_path = SURFACE_MARKER
    if SURFACE_STAGE.exists() and marker_path.is_file():
        try:
            marker = marker_path.read_text(encoding="utf-8").strip()
        except OSError:
            marker = ""
        if marker == _staged_repo_digest():
            build_id = _registered_build(str(SURFACE_STAGE))
            if build_id is not None:
                return {"stageRoot": str(SURFACE_STAGE), "buildId": build_id, "reused": True}
            registered = build_register(str(SURFACE_STAGE))
            return {"stageRoot": str(SURFACE_STAGE), "buildId": registered.get("build_id"), "reused": True}
    parent = SURFACE_STAGE.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if SURFACE_STAGE.exists():
        import shutil
        shutil.rmtree(SURFACE_STAGE, ignore_errors=True)
    temporary = Path(tempfile.mkdtemp(prefix=".surface-stage-", dir=str(parent)))
    temporary.rmdir()
    try:
        result = subprocess.run(
            [str(REPO_ROOT / "bin/pi-install"), "stage", "--source-root", str(REPO_ROOT), "--staging-root", str(temporary)],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            raise SurfaceError(f"surface staging failed: {result.stderr.strip()[-1024:]}")
        staged = json.loads(result.stdout)
        manifest = json.loads((temporary / "build-manifest.json").read_text(encoding="utf-8"))
        build_id = manifest.get("buildId")
        if not isinstance(build_id, str):
            raise SurfaceError("surface stage produced no build id")
        build_register(str(temporary))
        temporary.rename(SURFACE_STAGE)
        marker_path = SURFACE_MARKER
        marker_path.write_text(_staged_repo_digest(), encoding="utf-8")
        return {"stageRoot": str(SURFACE_STAGE), "buildId": build_id, "reused": False}
    except BaseException:
        import shutil
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _launcher_root() -> str:
    return str(ensure_surface_stage()["stageRoot"])


def env() -> dict:
    build_id: str | None = None
    launcher_dir: Path
    marker_path = SURFACE_MARKER
    if SURFACE_STAGE.is_dir() and marker_path.is_file():
        try:
            marker = marker_path.read_text(encoding="utf-8").strip()
        except OSError:
            marker = ""
        if marker == _staged_repo_digest():
            try:
                manifest = json.loads((SURFACE_STAGE / "build-manifest.json").read_text(encoding="utf-8"))
                build_id = manifest.get("buildId")
                launcher_dir = SURFACE_STAGE / "bin"
            except (OSError, json.JSONDecodeError):
                pass
    if build_id is None:
        # Fall back to the activated install; launchers may be stale but the
        # controller and session layout remain the authoritative daily state.
        activation = DATA_ROOT / "activation.json"
        if not activation.is_file():
            raise SurfaceError(f"the greenfield install is not activated: {activation}")
        try:
            marker = json.loads(activation.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SurfaceError(f"activation marker is unreadable: {activation}") from error
        candidate = marker.get("buildId")
        if not isinstance(candidate, str) or not candidate:
            raise SurfaceError("activation marker has no buildId")
        build_id = candidate
        launcher_dir = DATA_ROOT / "bin"
    controller = launcher_dir / "pi-control"
    if not controller.is_file():
        controller = CONTROL_HELPER / "bin" / "pi-control"
    if not controller.is_file():
        raise SurfaceError(f"controller is missing: {controller}")
    launchers = {
        "secretary": launcher_dir / "pi-system-secretary",
        "personal": launcher_dir / "pi-system-container-run",
        "workstream": launcher_dir / "pi-system-workstream-run",
        "reviewer": launcher_dir / "pi-system-reviewer",
        "investigator": launcher_dir / "pi-system-investigator",
    }
    for name, path in launchers.items():
        if not path.is_file():
            raise SurfaceError(f"launcher is missing: {path}")
    return {
        "dataRoot": str(DATA_ROOT),
        "stateRoot": str(STATE_ROOT),
        "buildId": build_id,
        "model": MODEL,
        "toolImage": TOOL_IMAGE,
        "controller": str(controller),
        "launchers": {name: str(path) for name, path in launchers.items()},
    }


def _controller() -> str:
    return env()["controller"]


def build_register(stage_root: str) -> dict:
    # Use the staged controller itself: the activated install may predate the
    # current repo code, and build registration must run the same generation
    # that owns the staged manifests.
    staged_controller = Path(stage_root) / "bin" / "pi-control"
    controller = str(staged_controller) if staged_controller.is_file() else _controller()
    return _run([controller, "--state-root", str(STATE_ROOT), "build", "register", "--staged-root", stage_root])


def project_register(repository: str, name: str | None = None) -> dict:
    argv = [_controller(), "--state-root", str(STATE_ROOT), "project", "register", "--repository", repository]
    if name:
        argv += ["--name", name]
    return _run(argv)


def project_list() -> list:
    value = _run([_controller(), "--state-root", str(STATE_ROOT), "project", "list"])
    return value if isinstance(value, list) else []


def project_status(project_id: str) -> dict:
    value = _run([_controller(), "--state-root", str(STATE_ROOT), "project", "status", project_id])
    if not isinstance(value, dict):
        raise SurfaceError("project status is not an object")
    return value


def secretary_conversation(project_id: str) -> dict:
    status = project_status(project_id)
    for conversation in status.get("conversations", []):
        if conversation.get("role") == "secretary":
            return conversation
    raise SurfaceError(f"project {project_id} has no secretary conversation")


def personal_conversation(project_id: str, display_name: str = "personal") -> dict:
    status = project_status(project_id)
    for conversation in status.get("conversations", []):
        if conversation.get("role") == "personal":
            return conversation
    primary = next((item for item in status.get("workingCopies", []) if item.get("kind") == "primary"), None)
    if primary is None:
        raise SurfaceError(f"project {project_id} has no primary working copy")
    request = {
        "projectId": project_id,
        "role": "personal",
        "displayName": display_name,
        "workingCopyId": primary["working_copy_id"],
        "idempotencyKey": f"surface-personal:{project_id}",
    }
    value = _run([_controller(), "--state-root", str(STATE_ROOT), "conversation", "create", "--request-json", json.dumps(request)])
    if not isinstance(value, dict) or "conversation_id" not in value:
        raise SurfaceError("personal conversation creation failed")
    return value


def workstream_create(project_id: str, title: str, idempotency_key: str) -> dict:
    request = {"projectId": project_id, "title": title, "idempotencyKey": idempotency_key}
    value = _run([_controller(), "--state-root", str(STATE_ROOT), "workstream", "create", "--request-json", json.dumps(request)])
    if not isinstance(value, dict) or "conversation_id" not in value:
        raise SurfaceError("workstream creation failed")
    return value


def workstream_conversation(project_id: str, display_name: str = "personal") -> dict:
    """Return the project's active workstream conversation, creating one when
    missing. Writers require a controller-created worktree, so the personal
    surface is backed by a durable workstream rather than the primary checkout.
    """
    status = project_status(project_id)
    for conversation in status.get("conversations", []):
        if conversation.get("role") == "workstream" and conversation.get("desired_state") == "active":
            return conversation
    return workstream_create(project_id, display_name, f"surface-workstream:{project_id}")


_LAUNCHER_NAMES = {
    "secretary": "pi-system-secretary",
    "personal": "pi-system-container-run",
    "workstream": "pi-system-workstream-run",
    "reviewer": "pi-system-reviewer",
    "investigator": "pi-system-investigator",
}


def launch_argv(role: str, conversation_id: str, prompt: str, **extra: str) -> list[str]:
    stage = ensure_surface_stage()
    launcher_name = _LAUNCHER_NAMES.get(role)
    if launcher_name is None:
        raise SurfaceError(f"unknown surface role: {role}")
    launcher = Path(stage["stageRoot"]) / "bin" / launcher_name
    argv = [
        str(launcher),
        "--state-root", str(STATE_ROOT),
        "--conversation-id", conversation_id,
        "--build-id", stage["buildId"],
        "--prompt", prompt,
        "--model", MODEL,
    ]
    if role in {"personal", "workstream"}:
        argv += ["--tool-image", TOOL_IMAGE]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-surface")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("env")
    sub.add_parser("ensure-stage")
    build = sub.add_parser("build-register")
    build.add_argument("--staged-root", required=True)
    project = sub.add_parser("project-register")
    project.add_argument("--repository", required=True)
    project.add_argument("--name")
    sub.add_parser("project-list")
    status = sub.add_parser("project-status")
    status.add_argument("project_id")
    secretary = sub.add_parser("secretary-conversation")
    secretary.add_argument("project_id")
    personal = sub.add_parser("personal-conversation")
    personal.add_argument("project_id")
    personal.add_argument("--name", default="personal")
    workstream = sub.add_parser("workstream-create")
    workstream.add_argument("project_id")
    workstream.add_argument("title")
    workstream.add_argument("idempotency_key")
    ws_conv = sub.add_parser("workstream-conversation")
    ws_conv.add_argument("project_id")
    ws_conv.add_argument("--name", default="personal")
    launch = sub.add_parser("launch-argv")
    launch.add_argument("role", choices=("secretary", "personal", "workstream", "reviewer", "investigator"))
    launch.add_argument("conversation_id")
    launch.add_argument("prompt")
    launch_shell = sub.add_parser("launch-argv-shell")
    launch_shell.add_argument("role", choices=("secretary", "personal", "workstream", "reviewer", "investigator"))
    launch_shell.add_argument("conversation_id")
    launch_shell.add_argument("prompt")
    args = parser.parse_args(argv)
    try:
        if args.command == "env":
            value: object = env()
        elif args.command == "ensure-stage":
            value = ensure_surface_stage()
        elif args.command == "build-register":
            value = build_register(args.staged_root)
        elif args.command == "project-register":
            value = project_register(args.repository, args.name)
        elif args.command == "project-list":
            value = project_list()
        elif args.command == "project-status":
            value = project_status(args.project_id)
        elif args.command == "secretary-conversation":
            value = secretary_conversation(args.project_id)
        elif args.command == "personal-conversation":
            value = personal_conversation(args.project_id, args.name)
        elif args.command == "workstream-create":
            value = workstream_create(args.project_id, args.title, args.idempotency_key)
        elif args.command == "workstream-conversation":
            value = workstream_conversation(args.project_id, args.name)
        elif args.command == "launch-argv-shell":
            value = " ".join(shlex.quote(part) for part in launch_argv(args.role, args.conversation_id, args.prompt))
        else:
            value = launch_argv(args.role, args.conversation_id, args.prompt)
        if args.command == "launch-argv-shell":
            print(value)
        else:
            print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return 0
    except SurfaceError as error:
        print(f"pi-surface: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
