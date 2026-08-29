"""Independent worker repository creation, locking, and validation."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Iterator, Mapping

from .fsutil import _secure_tree
from .git_runner import git_text, run_git
from .models import ConflictError, InvalidRequestError, NeedsAttentionError, validate_git_oid, validate_id


WORKER_IDENTITY = ("Pisec Worker", "pisec-worker@invalid")
SECRETARY_IDENTITY = ("Pisec Secretary", "pisec-secretary@invalid")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,511}$")
_CONFIG_KEYS = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.hookspath", "core.fsmonitor", "commit.gpgsign", "tag.gpgsign", "gc.auto",
    "maintenance.auto",
})


def _oid(repository: Path, revision: str) -> str:
    value = validate_git_oid(git_text(repository, "rev-parse", "--verify", f"{revision}^{{commit}}"), "Git commit object id")
    if value != value.lower():
        raise NeedsAttentionError("Git returned an invalid commit object id")
    return value


def _owner_dir(path: Path, *, create: bool = False) -> None:
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NeedsAttentionError("managed Git directory is unsafe")
        if info.st_uid != os.geteuid():
            raise NeedsAttentionError("managed Git directory is not owner-controlled")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(path, 0o700)
        return
    if not create:
        raise NeedsAttentionError("managed Git directory is missing")
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _owner_parents(path: Path) -> None:
    """Validate each existing managed parent before creating a leaf."""
    path = path.absolute()
    if path.is_symlink():
        raise NeedsAttentionError("managed Git directory is unsafe")
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    _owner_dir(current)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)


def _canonical_branch(value: Any) -> tuple[str, str]:
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise InvalidRequestError("target branch is invalid")
    branch = value.removeprefix("refs/heads/")
    if not _BRANCH_RE.fullmatch(branch) or branch != value and value != f"refs/heads/{branch}":
        raise InvalidRequestError("target branch must be a canonical local branch")
    if branch in {"HEAD", "", ".", ".."} or ".." in branch or branch.endswith("/") or branch.endswith("."):
        raise InvalidRequestError("target branch is invalid")
    return branch, f"refs/heads/{branch}"


def project_target_state(primary: Path, target: Any) -> tuple[str, str, str]:
    """Resolve the exact local branch and commit used for worker creation."""
    if target in (None, "", "HEAD"):
        branch = git_text(primary, "symbolic-ref", "--quiet", "--short", "HEAD")
    else:
        branch, _ = _canonical_branch(target)
    branch, ref = _canonical_branch(branch)
    output = run_git(primary, ("show-ref", "--verify", "--quiet", ref), accepted=(0, 1))
    if output.returncode != 0:
        raise NeedsAttentionError("approved target is not a local branch")
    oid = validate_git_oid(git_text(primary, "rev-parse", "--verify", f"{ref}^{{commit}}"), "target base commit")
    return branch, ref, oid


@contextmanager
def project_git_lock(state_root: Path | str, project_id: str, *, timeout: float = 5.0) -> Iterator[None]:
    validate_id(project_id, prefix="prj")
    root = Path(state_root) / "locks" / "projects"
    _secure_tree(Path(state_root), root)
    path = root / f"{project_id}.git.lock"
    flags = os.O_RDWR | os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    try:
        deadline = __import__("time").monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if __import__("time").monotonic() >= deadline:
                    raise ConflictError("project Git lock is busy")
                __import__("time").sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def project_permissions_lock(state_root: Path | str, project_id: str, *, timeout: float = 5.0) -> Iterator[None]:
    """Serialize one project's permission replacement and runtime materialization."""
    validate_id(project_id, prefix="prj")
    root = Path(state_root) / "locks" / "projects"
    _secure_tree(Path(state_root), root)
    path = root / f"{project_id}.permissions.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = __import__("time").monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if __import__("time").monotonic() >= deadline:
                    raise ConflictError("project permission lock is busy")
                __import__("time").sleep(0.05)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _git_dir(repository: Path) -> Path:
    marker = repository / ".git"
    if marker.is_symlink() or not marker.is_dir():
        raise NeedsAttentionError("worker repository must contain a real .git directory")
    return marker


def _git_path(repository: Path, name: str) -> Path:
    path = Path(git_text(repository, "rev-parse", "--git-path", name))
    return path if path.is_absolute() else repository / path


def _verify_independent_objects(primary: Path, worker: Path) -> None:
    primary_objects = _git_path(primary, "objects")
    worker_objects = _git_path(worker, "objects")
    alternates = worker_objects / "info" / "alternates"
    if alternates.exists() or (_git_dir(worker) / "info" / "grafts").exists():
        raise NeedsAttentionError("worker repository contains an object indirection file")
    for child in worker_objects.rglob("*"):
        if not child.is_file():
            continue
        try:
            counterpart = primary_objects / child.relative_to(worker_objects)
            worker_stat = child.stat()
            primary_stat = counterpart.stat()
        except (FileNotFoundError, ValueError):
            continue
        if worker_stat.st_dev == primary_stat.st_dev and worker_stat.st_ino == primary_stat.st_ino:
            raise NeedsAttentionError("worker repository shares a hardlinked Git object with the primary")


def _check_no_nested_git(repository: Path) -> None:
    root_marker = (_git_dir(repository)).absolute()
    for marker in repository.rglob(".git"):
        if marker.absolute() != root_marker:
            raise InvalidRequestError("nested Git repositories are unsupported")


def _remove_refs(repository: Path, prefixes: tuple[str, ...]) -> None:
    refs = git_text(repository, "for-each-ref", "--format=%(refname)", *prefixes)
    for ref in refs.splitlines():
        if ref:
            run_git(repository, ("update-ref", "-d", ref))


def _rewrite_config(repository: Path) -> None:
    # Clone configuration is deliberately reduced to a fixed local allowlist.
    configured = [line.strip() for line in git_text(repository, "config", "--local", "--name-only", "--list").splitlines() if line.strip()]
    for key in sorted(set(configured)):
        if key.lower() not in _CONFIG_KEYS:
            run_git(repository, ("config", "--local", "--unset-all", key), accepted=(0, 1, 2, 5, 128))
    for key, value in (
        ("core.repositoryformatversion", "0"), ("core.filemode", "true"),
        ("core.bare", "false"), ("core.logallrefupdates", "true"),
        ("core.hooksPath", "/dev/null"), ("core.fsmonitor", "false"),
        ("commit.gpgSign", "false"), ("tag.gpgSign", "false"),
        ("gc.auto", "0"), ("maintenance.auto", "false"),
    ):
        run_git(repository, ("config", "--local", key, value))
    config = git_text(repository, "config", "--local", "--name-only", "--list")
    keys = {line.strip().lower() for line in config.splitlines() if line.strip()}
    if not keys.issubset(_CONFIG_KEYS):
        raise NeedsAttentionError("worker Git configuration contains an unsupported key")


def _check_no_gitlinks(repository: Path) -> None:
    tree = git_text(repository, "ls-tree", "-r", "-t", "HEAD")
    if any(line.startswith("160000 ") for line in tree.splitlines()):
        raise InvalidRequestError("Git submodules and gitlinks are unsupported")


def create_worker_repository(
    *,
    primary: Path,
    worker: Path,
    project_id: str,
    workstream_id: str,
    target_branch_ref: str,
    base_oid: str,
    target_branch: str | None = None,
) -> Path:
    """Create the exact self-contained repository described by the v1 contract."""
    validate_id(project_id, prefix="prj")
    validate_id(workstream_id, prefix="ws")
    branch, canonical_ref = _canonical_branch(target_branch_ref)
    base_oid = validate_git_oid(base_oid, "target base commit")
    current_branch, current_ref, current_oid = project_target_state(primary, canonical_ref)
    if current_branch != branch or current_ref != canonical_ref or current_oid != base_oid:
        raise ConflictError("approved target ref moved during worker preparation")
    _owner_parents(worker.parent)
    if worker.exists() or worker.is_symlink():
        if worker.is_symlink() or not worker.is_dir():
            raise NeedsAttentionError("approved worker repository path is unsafe")
        validate_worker_repository(worker, branch_name=f"pisec/{workstream_id}/work", base_oid=base_oid, target_branch=target_branch or branch, require_clean=True)
        return worker
    if worker.absolute().resolve(strict=False) != worker.absolute():
        raise NeedsAttentionError("approved worker repository path resolves through a symlink")
    result = run_git(
        None,
        ("clone", "--no-local", "--no-hardlinks", "--no-checkout", "--single-branch", "--no-tags", "--branch", branch, "--", str(primary), str(worker)),
        timeout=120,
    )
    del result
    _, _, after_oid = project_target_state(primary, canonical_ref)
    if after_oid != base_oid:
        shutil.rmtree(worker)
        raise ConflictError("approved target ref moved during worker preparation")
    _owner_dir(worker)
    git_dir = _git_dir(worker)
    _check_no_gitlinks(worker)
    _check_no_nested_git(worker)
    worker_branch = f"pisec/{workstream_id}/work"
    run_git(worker, ("checkout", "--force", "-B", worker_branch, base_oid))
    for ref_prefix in ("refs/heads", "refs/remotes", "refs/tags"):
        refs = git_text(worker, "for-each-ref", "--format=%(refname)", ref_prefix)
        for ref in refs.splitlines():
            if ref and ref != f"refs/heads/{worker_branch}":
                run_git(worker, ("update-ref", "-d", ref))
    run_git(worker, ("update-ref", f"refs/remotes/origin/{branch}", base_oid))
    run_git(worker, ("symbolic-ref", f"refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"))
    _rewrite_config(worker)
    hooks = git_dir / "hooks"
    if hooks.is_dir():
        for child in hooks.iterdir():
            if child.is_file() or child.is_symlink():
                child.unlink()
    os.chmod(worker, 0o700)
    _verify_independent_objects(primary, worker)
    validate_worker_repository(worker, branch_name=worker_branch, base_oid=base_oid, target_branch=target_branch or branch, require_clean=True)
    return worker


def validate_worker_repository(
    repository: Path | str,
    *,
    branch_name: str,
    base_oid: str,
    target_branch: str | None = None,
    require_clean: bool = True,
    role: str = "worker",
    allowed_private_ref: str | None = None,
    history_base_oid: str | None = None,
    review_base_oid: str | None = None,
) -> str:
    """Validate worker repository identity, refs, configuration, and candidate commits."""
    repo = Path(repository).absolute()
    _owner_dir(repo)
    _git_dir(repo)
    _check_no_nested_git(repo)
    objects = _git_path(repo, "objects")
    if (objects / "info" / "alternates").exists() or (_git_dir(repo) / "info" / "grafts").exists():
        raise NeedsAttentionError("worker repository contains an object indirection file")
    base_oid = validate_git_oid(base_oid, "worker base commit")
    branch = git_text(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != branch_name:
        raise NeedsAttentionError("worker repository is detached or on the wrong branch")
    head = validate_git_oid(git_text(repo, "rev-parse", "--verify", "HEAD^{commit}"), "worker HEAD")
    if role == "worker":
        identity = WORKER_IDENTITY
    elif role == "secretary":
        identity = SECRETARY_IDENTITY
    else:
        raise InvalidRequestError("worker repository role is invalid")
    if require_clean and git_text(repo, "status", "--porcelain", "--untracked-files=all"):
        raise NeedsAttentionError("worker repository is dirty")
    top_level = Path(git_text(repo, "rev-parse", "--show-toplevel")).absolute()
    if top_level != repo:
        raise NeedsAttentionError("worker repository top-level is not canonical")
    expected_values = {
        "core.repositoryformatversion": "0",
        "core.filemode": "true",
        "core.bare": "false",
        "core.logallrefupdates": "true",
        "core.hookspath": "/dev/null",
        "core.fsmonitor": "false",
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
        "gc.auto": "0",
        "maintenance.auto": "false",
    }
    for key, expected in expected_values.items():
        value = git_text(repo, "config", "--local", "--get", key)
        if value != expected:
            raise NeedsAttentionError("worker Git configuration has an unexpected value")
    hooks = _git_dir(repo) / "hooks"
    if hooks.exists() and any(hooks.iterdir()):
        raise NeedsAttentionError("worker repository contains a hook")
    ancestry = run_git(repo, ("merge-base", "--is-ancestor", base_oid, "HEAD"), accepted=(0, 1)).returncode
    if ancestry != 0:
        raise NeedsAttentionError("worker HEAD does not descend from the approved base")
    if git_text(repo, "remote"):
        raise NeedsAttentionError("worker repository has a remote")
    config = {line.strip().lower() for line in git_text(repo, "config", "--local", "--name-only", "--list").splitlines() if line.strip()}
    if not config.issubset(_CONFIG_KEYS):
        raise NeedsAttentionError("worker Git configuration is outside the approved allowlist")
    expected_config = {key.lower() for key in _CONFIG_KEYS}
    if not expected_config.issubset(config):
        raise NeedsAttentionError("worker Git configuration is incomplete")
    candidate_base = validate_git_oid(history_base_oid, "worker history base") if history_base_oid is not None else base_oid
    candidates = git_text(repo, "log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%G?", f"{candidate_base}..HEAD")
    records = [line.split("\x00") for line in candidates.splitlines() if line]
    if any(len(record) != 6 for record in records):
        raise NeedsAttentionError("worker candidate history is invalid")
    for _, author_name, author_email, committer_name, committer_email, signature in records:
        if (author_name, author_email, committer_name, committer_email) != (*identity, *identity):
            raise NeedsAttentionError("worker candidate commit identity is not the fixed Pisec identity")
        if signature not in {"N", " "}:
            raise NeedsAttentionError("signed worker candidate commits are unsupported")
    refs = set(filter(None, git_text(repo, "for-each-ref", "--format=%(refname)").splitlines()))
    inert_target_ref = f"refs/remotes/origin/{target_branch or branch_name.removeprefix('pisec/').split('/')[0]}"
    allowed = {f"refs/heads/{branch_name}", inert_target_ref}
    allowed.update(ref for ref in refs if ref.startswith("refs/reviewr/"))
    if allowed_private_ref is not None:
        if not allowed_private_ref.startswith("refs/pisec/target/"):
            raise InvalidRequestError("worker private ref is invalid")
        allowed.add(allowed_private_ref)
    allowed.add("refs/remotes/origin/HEAD")
    if refs - allowed:
        raise NeedsAttentionError("worker repository contains an unexpected ref")
    expected_review_base = validate_git_oid(review_base_oid, "worker Reviewr base") if review_base_oid is not None else base_oid
    if _oid(repo, inert_target_ref) != expected_review_base:
        raise NeedsAttentionError("worker Reviewr base ref does not match the approved base")
    symbolic_head = git_text(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if symbolic_head != f"origin/{target_branch or branch_name.removeprefix('pisec/').split('/')[0]}":
        raise NeedsAttentionError("worker Reviewr HEAD ref is not the approved base ref")
    _check_no_gitlinks(repo)
    return head


def validate_worker_resume_git(store: Any, binding: Mapping[str, Any]) -> str | None:
    """Validate the ordinary worker repository before launch or resume."""
    workstream_id = str(binding.get("workstream_id") or "")
    row = store.conn.execute(
        "SELECT w.kind,w.branch_name,w.worktree_path,w.base_commit_oid,w.target_ref "
        "FROM workstreams w WHERE w.workstream_id=?",
        (workstream_id,),
    ).fetchone()
    if row is None:
        raise NeedsAttentionError("worker Git binding is missing")
    if row["kind"] != "worker":
        return None
    target = str(row["target_ref"])
    target_branch = target.removeprefix("refs/heads/")
    private_ref = None
    job = store.conn.execute(
        "SELECT integration_id,state,target_oid FROM integration_jobs WHERE workstream_id=? ORDER BY created_at DESC LIMIT 1",
        (workstream_id,),
    ).fetchone()
    if job is not None and job["state"] in {"awaiting_worker", "queued", "refreshing", "verifying", "applying"}:
        private_ref = f"refs/pisec/target/{job['integration_id']}"
    history_base_oid = str(job["target_oid"]) if job is not None and job["target_oid"] else None
    review_base_oid = history_base_oid if private_ref is not None else None
    return validate_worker_repository(
        Path(str(row["worktree_path"])),
        branch_name=str(row["branch_name"]),
        base_oid=str(row["base_commit_oid"]),
        target_branch=target_branch,
        allowed_private_ref=private_ref,
        history_base_oid=history_base_oid,
        review_base_oid=review_base_oid,
    )
