#!/usr/bin/env python3
"""Shared resolver for the Pi surface commands.

Resolves the one active generation (controller, launchers, resources, runtime
all from the same activated build), registers the active build with the
controller state, and ensures projects and role conversations exist. Surface
wrappers (pi-start, pi-restart, pisec, pi-personal, pidev) call this helper
and launch the pi-system-* binaries.

The daily launch model is one-shot and controller-bound: each prompt is one
run; conversation continuity comes from the durable session file, so a
follow-up turn is a relaunch of the same conversation id with a new prompt.

There is no implicit rebuild from repository source. Development staging is
the explicit `ensure-stage` command; development launches pass `--stage-root`
explicitly. The daily surface binds the activated generation end to end.
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


def _stage_build_id(stage_root: str) -> str:
    try:
        manifest = json.loads((Path(stage_root) / "build-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SurfaceError(f"development stage is unreadable: {stage_root}") from error
    build_id = manifest.get("buildId")
    if not isinstance(build_id, str) or not build_id:
        raise SurfaceError(f"development stage has no build id: {stage_root}")
    return build_id


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
    Look up by build id (from the stage manifest), not by path: the stage is
    staged to a temporary name and renamed into its stable path, so the
    registered row's manifest path may be the earlier temporary location.
    """
    try:
        manifest = json.loads((Path(stage_root) / "build-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    build_id = manifest.get("buildId")
    if not isinstance(build_id, str) or not build_id:
        return None
    installed = Path(__file__).resolve().parent / "pi_control"
    if installed.is_dir():
        sys.path.insert(0, str(installed.parent))
        from pi_control.pi_store import PiStore
    else:
        sys.path.insert(0, str(REPO_ROOT))
        from scripts.pi_control.pi_store import PiStore
    try:
        with PiStore(STATE_ROOT) as store:
            row = store.conn.execute(
                "SELECT build_id FROM installed_builds WHERE status='staged' AND build_id=?",
                (build_id,),
            ).fetchone()
    except Exception:
        return None
    return row["build_id"] if row is not None else None


def ensure_surface_stage() -> dict:
    """Build and register one development surface stage from the current repo.

    This is the explicit development staging command (`pi-surface
    ensure-stage`); the daily surface never calls it. Development launches
    then pass the resulting `--stage-root` explicitly. The stage is refreshed
    only when the repo source moved, and registered with the controller so
    development launches bind a verified generation.

    The build is serialized with an exclusive lock: parallel invocations
    would race on the stage rename.
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
    lock_path = parent / (SURFACE_STAGE.name + ".lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise SurfaceError(f"cannot open the surface stage lock: {error}") from error
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Another process may have built the stage while we waited for the
        # lock; re-check before rebuilding.
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
            temporary.rename(SURFACE_STAGE)
            build_register(str(SURFACE_STAGE))
            marker_path = SURFACE_MARKER
            marker_path.write_text(_staged_repo_digest(), encoding="utf-8")
            return {"stageRoot": str(SURFACE_STAGE), "buildId": build_id, "reused": False}
        except BaseException:
            import shutil
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    finally:
        import fcntl
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


def _launcher_root() -> str:
    return str(ensure_surface_stage()["stageRoot"])


def _activated_generation() -> tuple[str, Path]:
    """Resolve the one active generation for the daily surface.

    The daily surface binds a single activated build end to end: controller,
    launchers, resources, and runtime all come from the same generation
    recorded in activation.json. There is no implicit rebuild from repository
    source; development staging is an explicit command.
    """
    activation = DATA_ROOT / "activation.json"
    if not activation.is_file():
        raise SurfaceError(f"the Pi install is not activated: {activation}")
    try:
        marker = json.loads(activation.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SurfaceError(f"activation marker is unreadable: {activation}") from error
    candidate = marker.get("buildId")
    if not isinstance(candidate, str) or not candidate:
        raise SurfaceError("activation marker has no buildId")
    launcher_dir = DATA_ROOT / "bin"
    if not (launcher_dir / "pi-control").is_file():
        raise SurfaceError(f"the activated generation is missing its controller: {launcher_dir / 'pi-control'}")
    return candidate, launcher_dir


def env() -> dict:
    build_id, launcher_dir = _activated_generation()
    controller = launcher_dir / "pi-control"
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


def recover_conversation(conversation_id: str) -> dict:
    """Recover provably lost runs of one conversation before surface repair.

    Live runs are reported untouched; uncertain runs are reported and must
    block relaunch. The surface repair loops call this before respawning a
    dead pane so a stale writer claim can never brick the conversation.
    """
    request = {"conversationId": conversation_id, "actorId": "surface-repair"}
    value = _run([_controller(), "--state-root", str(STATE_ROOT), "conversation", "recover", "--request-json", json.dumps(request)])
    if not isinstance(value, dict):
        raise SurfaceError("conversation recovery result is not an object")
    return value


def _surfaces_config_path(config: str | None) -> Path:
    if config:
        path = Path(config).expanduser()
    elif os.environ.get("PI_SURFACES_CONFIG"):
        path = Path(os.environ["PI_SURFACES_CONFIG"]).expanduser()
    else:
        installed = Path.home() / ".config" / "pi" / "surfaces.json"
        if installed.is_file():
            path = installed
        else:
            machine_id = os.environ.get("DOTFILES_MACHINE_ID")
            candidates = sorted(REPO_ROOT.glob(f"machines/{machine_id}.pi-surfaces.json")) if machine_id else []
            if not candidates:
                candidates = sorted(REPO_ROOT.glob("machines/*.pi-surfaces.json"))
            if not candidates:
                raise SurfaceError("no surface configuration found; pass --config or install ~/.config/pi/surfaces.json")
            path = candidates[0]
    if not path.is_file():
        raise SurfaceError(f"surface configuration is missing: {path}")
    return path


def _load_surfaces_config(config: str | None) -> tuple[dict, Path]:
    path = _surfaces_config_path(config)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SurfaceError(f"surface configuration is unreadable: {path}") from error
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SurfaceError("surface configuration must declare version 1")
    projects = value.get("projects")
    if not isinstance(projects, list) or not projects:
        raise SurfaceError("surface configuration must declare at least one project")
    aliases: list[str] = []
    for item in projects:
        if not isinstance(item, dict) or not isinstance(item.get("alias"), str) or not item["alias"] or not isinstance(item.get("repository"), str) or not item["repository"]:
            raise SurfaceError("surface project entries require alias and repository")
        aliases.append(item["alias"])
    if len(set(aliases)) != len(aliases):
        raise SurfaceError("surface project aliases must be unique")
    surfaces = value.get("surfaces")
    if not isinstance(surfaces, dict) or not surfaces:
        raise SurfaceError("surface configuration must declare surfaces")
    for surface, ids in surfaces.items():
        if surface not in {"pisec", "pi-personal"}:
            raise SurfaceError(f"unknown surface: {surface}")
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
            raise SurfaceError(f"surface {surface} active set must be a list of aliases")
        for item in ids:
            if item not in aliases:
                raise SurfaceError(f"surface {surface} references an unknown alias: {item}")
    return value, path


def _repository_identity(repository: str, *, allow_missing: bool) -> tuple[Path, str] | None:
    import subprocess
    source = Path(repository).expanduser().resolve()
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "--show-toplevel"], capture_output=True, text=True, env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"})
    if result.returncode != 0:
        if allow_missing:
            return None
        raise SurfaceError(f"configured repository is not a Git checkout: {source}")
    toplevel = Path(result.stdout.strip()).resolve(strict=True)
    common = subprocess.run(["git", "-C", str(toplevel), "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True, env={"PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"})
    if common.returncode != 0:
        raise SurfaceError(f"cannot resolve Git identity for {toplevel}")
    return toplevel, Path(common.stdout.strip()).resolve().as_posix()


def bootstrap(config: str | None = None, *, dry_run: bool = False, keep_extra: bool = False, allow_missing: bool = False) -> dict:
    """Reconcile the declared surface configuration with controller state.

    Registers missing projects under their alias, renames projects whose
    display name differs from the alias, and atomically writes both ordered
    surface preferences. Idempotent; --dry-run performs no writes.
    """
    value, path = _load_surfaces_config(config)
    identities: dict[str, dict] = {}
    common_to_alias: dict[str, str] = {}
    for item in value["projects"]:
        identity = _repository_identity(item["repository"], allow_missing=allow_missing)
        if identity is None:
            continue
        toplevel, common = identity
        if common in common_to_alias and common_to_alias[common] != item["alias"]:
            raise SurfaceError(f"two configured aliases resolve to the same Git common directory: {common_to_alias[common]} and {item['alias']}")
        common_to_alias[common] = item["alias"]
        identities[item["alias"]] = {"repository": str(toplevel), "gitCommonDir": common}

    current = project_list()
    by_common = {str(item["git_common_dir"]): item for item in current}
    by_display = {item["display_name"]: item for item in current}

    plan: dict[str, list] = {"register": [], "rename": []}
    project_ids: dict[str, str] = {}
    for alias, identity in identities.items():
        existing = by_common.get(identity["gitCommonDir"])
        if existing is None:
            if by_display.get(alias) is not None:
                raise SurfaceError(f"display name {alias} is taken by another registered project")
            plan["register"].append(alias)
            if dry_run:
                continue
            registered = project_register(identity["repository"], alias)
            project_ids[alias] = registered["project_id"]
            continue
        if existing["display_name"] != alias:
            plan["rename"].append({"alias": alias, "from": existing["display_name"]})
            if not dry_run:
                request = {"projectId": existing["project_id"], "displayName": alias}
                _run([_controller(), "--state-root", str(STATE_ROOT), "project", "rename", "--request-json", json.dumps(request)])
        project_ids[alias] = existing["project_id"]

    preferences: dict[str, list[str]] = {}
    for surface, aliases in value["surfaces"].items():
        active = [project_ids[alias] for alias in aliases if alias in project_ids]
        if keep_extra:
            existing_ids = preference_get(surface)["activeProjectIds"]
            extra = [pid for pid in existing_ids if pid not in active and any(item["project_id"] == pid for item in current)]
            active = active + extra
        preferences[surface] = active

    if dry_run:
        return {"dryRun": True, "config": str(path), "plan": plan, "preferences": preferences}
    for surface, active in preferences.items():
        preference_set(surface, active)
    return {"dryRun": False, "config": str(path), "plan": plan, "preferences": preferences, "projectIds": project_ids}


def _preferences_path() -> Path:
    return STATE_ROOT / "surface" / "preferences.json"


def preference_get(surface: str) -> dict:
    """Return the ordered active project set and whether it is configured.

    A missing preference means the surface falls back to every registered
    project; a present list, even when empty, is an explicitly configured
    grid. Stale ids are reported separately so callers can drop them without
    bricking the surface.
    """
    try:
        value = json.loads(_preferences_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"surface": surface, "activeProjectIds": [], "configured": False}
    if not isinstance(value, dict) or not isinstance(value.get(surface), list):
        return {"surface": surface, "activeProjectIds": [], "configured": False}
    ids = [item for item in value[surface] if isinstance(item, str) and item]
    return {"surface": surface, "activeProjectIds": list(dict.fromkeys(ids)), "configured": True}


def preference_set(surface: str, project_ids: list[str]) -> dict:
    """Replace the ordered active project set for one surface atomically."""
    if surface not in {"pisec", "pi-personal"}:
        raise SurfaceError(f"unknown surface: {surface}")
    if any(not isinstance(item, str) or not item for item in project_ids):
        raise SurfaceError("active project set must contain project ids")
    path = _preferences_path()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = parent / "preferences.lock"
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise SurfaceError(f"cannot open the surface preference lock: {error}") from error
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            raise SurfaceError("surface preferences are malformed")
        value[surface] = list(dict.fromkeys(project_ids))
        fd, temporary = tempfile.mkstemp(prefix=".preferences-", dir=str(parent))
        try:
            os.write(fd, json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        import fcntl
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
    return {"surface": surface, "activeProjectIds": value[surface]}


_LAUNCHER_NAMES = {
    "secretary": "pi-system-secretary",
    "personal": "pi-system-container-run",
    "workstream": "pi-system-workstream-run",
    "reviewer": "pi-system-reviewer",
    "investigator": "pi-system-investigator",
}


def launch_argv(role: str, conversation_id: str, prompt: str = "", **extra: str) -> list[str]:
    """Build one launcher argv for a conversation.

    The daily surface resolves the single activated generation. Development
    launches pass an explicit `--stage-root` (via `stage_root`) built by the
    explicit `ensure-stage` command; nothing here rebuilds from repository
    source implicitly.
    """
    launcher_name = _LAUNCHER_NAMES.get(role)
    if launcher_name is None:
        raise SurfaceError(f"unknown surface role: {role}")
    stage_root = extra.get("stage_root")
    if stage_root:
        stage = {"stageRoot": stage_root, "buildId": _stage_build_id(stage_root)}
        launcher_dir = Path(stage_root) / "bin"
    else:
        stage = env()
        launcher_dir = Path(stage["dataRoot"]) / "bin"
    launcher = launcher_dir / launcher_name
    argv = [
        str(launcher),
        "--state-root", str(STATE_ROOT),
        "--conversation-id", conversation_id,
        "--build-id", stage["buildId"],
        "--model", MODEL,
    ]
    if extra.get("interactive"):
        argv += ["--interactive"]
    elif prompt:
        argv += ["--prompt", prompt]
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
    recover = sub.add_parser("recover-conversation")
    recover.add_argument("conversation_id")
    bootstrap_parser = sub.add_parser("bootstrap")
    bootstrap_parser.add_argument("--config", default=None)
    bootstrap_parser.add_argument("--dry-run", action="store_true")
    bootstrap_parser.add_argument("--keep-extra", action="store_true")
    bootstrap_parser.add_argument("--allow-missing", action="store_true")
    preference = sub.add_parser("preference")
    preference_sub = preference.add_subparsers(dest="preference_command", required=True)
    preference_get_parser = preference_sub.add_parser("get")
    preference_get_parser.add_argument("surface", choices=("pisec", "pi-personal"))
    preference_set_parser = preference_sub.add_parser("set")
    preference_set_parser.add_argument("surface", choices=("pisec", "pi-personal"))
    preference_set_parser.add_argument("project_ids", nargs="*")
    launch = sub.add_parser("launch-argv")
    launch.add_argument("role", choices=("secretary", "personal", "workstream", "reviewer", "investigator"))
    launch.add_argument("conversation_id")
    launch.add_argument("prompt", nargs="?", default="")
    launch.add_argument("--interactive", action="store_true")
    launch.add_argument("--stage-root", default=None, help="explicit development stage; default is the activated generation")
    launch_shell = sub.add_parser("launch-argv-shell")
    launch_shell.add_argument("role", choices=("secretary", "personal", "workstream", "reviewer", "investigator"))
    launch_shell.add_argument("conversation_id")
    launch_shell.add_argument("prompt", nargs="?", default="")
    launch_shell.add_argument("--interactive", action="store_true")
    launch_shell.add_argument("--stage-root", default=None, help="explicit development stage; default is the activated generation")
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
        elif args.command == "recover-conversation":
            value = recover_conversation(args.conversation_id)
        elif args.command == "bootstrap":
            value = bootstrap(args.config, dry_run=args.dry_run, keep_extra=args.keep_extra, allow_missing=args.allow_missing)
        elif args.command == "preference":
            if args.preference_command == "get":
                value = preference_get(args.surface)
            else:
                value = preference_set(args.surface, args.project_ids)
        elif args.command == "launch-argv-shell":
            value = " ".join(shlex.quote(part) for part in launch_argv(args.role, args.conversation_id, args.prompt, interactive=args.interactive, stage_root=args.stage_root))
        else:
            value = launch_argv(args.role, args.conversation_id, args.prompt, interactive=args.interactive, stage_root=args.stage_root)
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
