#!/usr/bin/env python3
"""Review normalized Pi subagent harness feedback for a Git repository.

Feedback is written by Pi to user-owned state, not to a project worktree. This
read-only projection makes the records for the current repository easy to
review without copying raw prompts or secrets into the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

MAX_RECORD_BYTES = 256 * 1024
MAX_TEXT_CHARS = 4096
FEEDBACK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
PENDING_OUTCOMES = {"unreviewed", "replied"}
VALID_OUTCOMES = PENDING_OUTCOMES | {"accepted", "rejected", "deferred"}
VALID_LIFECYCLES = {"submitted", "delivered", "awaiting_reply", "replied", "reviewed", "expired", "inactive"}
VALID_REASONS = {"need_decision", "interview_request", "progress_update"}
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
SECRET_PATTERNS = (
    (re.compile(r"\b(?:sk|rk|pk|ghp|gho|ghs|github_pat|glpat|npm|xoxb|xoxp)_[A-Za-z0-9_-]{8,}\b"), False),
    (re.compile(r"\b(?:sk|rk|pk|ghp|gho|ghs|github_pat|glpat|xoxb|xoxp)-[A-Za-z0-9_-]{8,}\b"), False),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), False),
    (re.compile(r"-----BEGIN [^-]+ PRIVATE KEY-----[\s\S]*?-----END [^-]+ PRIVATE KEY-----"), False),
    (re.compile(r"(\b(?:password|passwd|secret|token|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.IGNORECASE), True),
)


def _agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if configured and Path(configured).is_absolute():
        return Path(configured).expanduser()
    return Path.home() / ".pi" / "agent"


def _git(repository: Path, *args: str) -> str:
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key)
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"git {' '.join(args)} timed out") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _repository_identity(repository: str) -> tuple[Path, str]:
    supplied = Path(repository).expanduser()
    if not supplied.is_absolute():
        supplied = Path.cwd() / supplied
    root = Path(_git(supplied, "rev-parse", "--show-toplevel")).resolve(strict=True)
    common_value = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_path = Path(common_value)
    if not common_path.is_absolute():
        common_path = root / common_path
    common = common_path.resolve(strict=True)
    project_id = hashlib.sha256(str(common).encode("utf-8")).hexdigest()
    return root, project_id


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _bounded_text(value: Any, maximum: int = MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    text = CONTROL_RE.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    for pattern, preserve_prefix in SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1) if preserve_prefix else ''}[redacted]", text)
    if not text:
        return None
    return text[:maximum]


def _normalized_source(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    limits = {
        "role": 128,
        "agent": 128,
        "runId": 128,
        "orchestratorSessionId": 128,
        "orchestratorTarget": 256,
        "childTarget": 256,
        "workstreamId": 63,
    }
    for key, maximum in limits.items():
        item = _bounded_text(value.get(key), maximum)
        if item is not None:
            result[key] = item
    project_id = _bounded_text(value.get("projectId"), 64)
    if project_id and PROJECT_ID_RE.fullmatch(project_id):
        result["projectId"] = project_id
    repository = _bounded_text(value.get("repository"), 512)
    if repository and Path(repository).is_absolute():
        result["repository"] = str(Path(repository))
    child_index = value.get("childIndex")
    if isinstance(child_index, int) and not isinstance(child_index, bool) and 0 <= child_index <= 1_000_000:
        result["childIndex"] = child_index
    return result


def _normalized_form(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, maximum in {
        "schema": 128,
        "kind": 128,
        "title": 512,
        "want": MAX_TEXT_CHARS,
        "blocked_by": MAX_TEXT_CHARS,
        "why": MAX_TEXT_CHARS,
        "recommendation": MAX_TEXT_CHARS,
        "summary": MAX_TEXT_CHARS,
    }.items():
        item = _bounded_text(value.get(key), maximum)
        if item is not None:
            result[key] = item
    evidence = value.get("evidence")
    if isinstance(evidence, list):
        normalized = [item for item in (_bounded_text(entry, 1000) for entry in evidence[:32]) if item]
        if normalized:
            result["evidence"] = normalized
    options = value.get("options")
    if isinstance(options, list):
        normalized_options: list[dict[str, str]] = []
        for option in options[:16]:
            if not isinstance(option, dict):
                continue
            normalized_option: dict[str, str] = {}
            for key, item in list(option.items())[:8]:
                normalized_key = _bounded_text(str(key), 80)
                normalized_value = _bounded_text(item, 1000)
                if normalized_key is not None and normalized_value is not None:
                    normalized_option[normalized_key] = normalized_value
            if normalized_option:
                normalized_options.append(normalized_option)
        if normalized_options:
            result["options"] = normalized_options
    if isinstance(value.get("decision_needed"), bool):
        result["decision_needed"] = value["decision_needed"]
    return result


def _public_record(value: dict[str, Any]) -> dict[str, Any] | None:
    """Return a strict normalized projection; never forward unknown/raw keys."""
    if value.get("schemaVersion") != 1:
        return None
    feedback_id = _bounded_text(value.get("feedbackId"), 128)
    source = _normalized_source(value.get("source"))
    form = _normalized_form(value.get("form"))
    outcome = value.get("outcome", "unreviewed")
    lifecycle = value.get("lifecycle", "submitted")
    reason = value.get("reason", "progress_update")
    if (not feedback_id or not FEEDBACK_ID_RE.fullmatch(feedback_id) or source is None or form is None
            or outcome not in VALID_OUTCOMES or lifecycle not in VALID_LIFECYCLES or reason not in VALID_REASONS):
        return None
    record: dict[str, Any] = {
        "schemaVersion": 1,
        "feedbackId": feedback_id,
        "source": source,
        "reason": reason,
        "form": form,
        "lifecycle": lifecycle,
        "outcome": outcome,
    }
    for key in ("createdAt", "updatedAt"):
        item = _bounded_text(value.get(key), 64)
        if item is not None:
            record[key] = item
    digest = _bounded_text(value.get("contentDigest"), 64)
    if digest and DIGEST_RE.fullmatch(digest):
        record["contentDigest"] = digest
    response = value.get("response")
    if isinstance(response, dict) and response.get("outcome") in VALID_OUTCOMES:
        normalized_response: dict[str, Any] = {"outcome": response["outcome"]}
        message = _bounded_text(response.get("message"))
        updated_at = _bounded_text(response.get("updatedAt"), 64)
        if message is not None:
            normalized_response["message"] = message
        if updated_at is not None:
            normalized_response["updatedAt"] = updated_at
        record["response"] = normalized_response
    return record


def _safe_record_bytes(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())):
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(MAX_RECORD_BYTES + 1)
        return data if len(data) <= MAX_RECORD_BYTES else None
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _load_records(project_id: str | None, include_reviewed: bool, limit: int | None) -> list[dict[str, Any]]:
    agent_dir = _agent_dir()
    feedback_dir = agent_dir / "feedback"
    root = feedback_dir / "records"
    for directory in (agent_dir, feedback_dir, root):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return []
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())):
            raise RuntimeError(f"feedback path is not a private directory: {directory}")

    records: list[dict[str, Any]] = []
    identity_cache: dict[str, str | None] = {}
    try:
        paths = list(root.glob("*.json"))
    except OSError as error:
        raise RuntimeError(f"cannot scan feedback records: {error}") from error
    for path in paths:
        raw = _safe_record_bytes(path)
        if raw is None:
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        record = _public_record(value)
        if record is None or path.name != f"{record['feedbackId']}.json":
            continue
        source = record["source"]
        if project_id is not None and source.get("projectId") != project_id:
            source_repository = source.get("repository")
            source_project_id: str | None = None
            if isinstance(source_repository, str):
                if source_repository not in identity_cache:
                    try:
                        identity_cache[source_repository] = _repository_identity(source_repository)[1]
                    except (OSError, RuntimeError):
                        identity_cache[source_repository] = None
                source_project_id = identity_cache[source_repository]
            if source_project_id != project_id:
                continue
        if not include_reviewed and record["outcome"] not in PENDING_OUTCOMES:
            continue
        records.append(record)

    records.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
    return records if limit is None else records[:limit]


def _display_text(value: Any, maximum: int = 1000) -> str:
    text = _bounded_text(value, maximum) or ""
    return " ".join(text.replace("\t", " ").splitlines())


def _markdown_text(value: Any, maximum: int = 1000) -> str:
    text = _display_text(value, maximum)
    return re.sub(r"([\\`*_\[\]<>#])", r"\\\1", text)


def _markdown_code(value: Any, maximum: int = 1000) -> str:
    return _display_text(value, maximum).replace("`", "'")


def _form_title(record: dict[str, Any]) -> str:
    form = record.get("form")
    if not isinstance(form, dict):
        return "(untitled feedback)"
    title = form.get("title") or form.get("summary")
    return _display_text(title, 240) if title else "(untitled feedback)"


def _render_text(records: list[dict[str, Any]], scope: str) -> str:
    safe_scope = _display_text(scope, 1000)
    if not records:
        return f"No harness feedback for {safe_scope}."
    lines = [f"Harness feedback for {safe_scope} ({len(records)}):"]
    for record in records:
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        form = record.get("form") if isinstance(record.get("form"), dict) else {}
        lines.extend([
            "",
            f"- {_display_text(record.get('feedbackId', 'unknown'), 128)} [{_display_text(form.get('kind', 'unspecified'), 128)}] {_form_title(record)}",
            f"  source: {_display_text(source.get('agent') or source.get('role') or 'unknown', 128)}; created: {_display_text(record.get('createdAt', 'unknown'), 64)}; outcome: {_display_text(record.get('outcome', 'unreviewed'), 32)}",
        ])
        for key in ("want", "blocked_by", "why", "recommendation"):
            value = form.get(key)
            if value:
                lines.append(f"  {key}: {_display_text(value)}")
        evidence = form.get("evidence")
        if isinstance(evidence, list):
            for item in evidence[:8]:
                lines.append(f"  evidence: {_display_text(item)}")
    return "\n".join(lines)


def _render_markdown(records: list[dict[str, Any]], scope: str) -> str:
    lines = [f"# Harness feedback: `{_markdown_code(scope, 1000)}`", "", "Normalized feedback only; raw prompt content is intentionally omitted.", ""]
    if not records:
        lines.append("No harness feedback.")
        return "\n".join(lines) + "\n"
    for record in records:
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        form = record.get("form") if isinstance(record.get("form"), dict) else {}
        lines.extend([
            f"## `{_markdown_code(record.get('feedbackId', 'unknown'), 128)}` — {_markdown_text(_form_title(record), 240)}",
            "",
            f"- **Kind:** `{_markdown_code(form.get('kind', 'unspecified'), 128)}`",
            f"- **Source:** `{_markdown_code(source.get('agent') or source.get('role') or 'unknown', 128)}`",
            f"- **Created:** `{_markdown_code(record.get('createdAt', 'unknown'), 64)}`",
            f"- **Outcome:** `{_markdown_code(record.get('outcome', 'unreviewed'), 32)}`",
        ])
        for key, label in (("want", "Want"), ("blocked_by", "Blocked by"), ("why", "Why"), ("recommendation", "Recommendation")):
            value = form.get(key)
            if value:
                lines.append(f"- **{label}:** {_markdown_text(value)}")
        evidence = form.get("evidence")
        if isinstance(evidence, list) and evidence:
            lines.append("- **Evidence:**")
            lines.extend(f"  - {_markdown_text(item)}" for item in evidence[:8])
        lines.append("")
    return "\n".join(lines)


def _write_output(path_value: str, repository_root: Path, content: str) -> Path:
    requested = Path(path_value).expanduser()
    if not requested.is_absolute():
        requested = repository_root / requested
    if os.path.lexists(requested) and requested.is_symlink():
        raise RuntimeError("output must not be a symlink")
    output = requested.resolve(strict=False)
    if not _within(output, repository_root):
        raise RuntimeError("output must remain inside the selected repository")
    if output == repository_root:
        raise RuntimeError("output must be a file inside the selected repository")
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = output.parent.resolve(strict=True)
    if not _within(parent, repository_root):
        raise RuntimeError("output parent escaped the selected repository")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=str(parent), text=True)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Mode 0600 is already attached to the inode. Do not chmod by path
        # after replacement, where a concurrent symlink swap could be followed.
        os.replace(temporary, output)
        try:
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    # Some supported filesystems do not fsync directories. The
                    # replacement is already complete and the file itself synced.
                    pass
            finally:
                os.close(directory_fd)
        return output
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", help="filter to one repository; omit to review every project")
    parser.add_argument("--all-projects", action="store_true", help="explicitly request the default all-projects view")
    parser.add_argument("--include-reviewed", action="store_true", help="include accepted, rejected, and deferred records")
    parser.add_argument("--limit", type=int, default=None, help="maximum records to show")
    parser.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    parser.add_argument("--output", help="write the rendered projection to a file inside the selected repository")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.repository and args.all_projects:
        parser.error("--repository and --all-projects are mutually exclusive")
    if args.output and args.format == "json":
        parser.error("--output is only supported for text or markdown projections")

    if args.output and not args.repository:
        parser.error("--output requires --repository so the destination stays inside a selected repository")

    try:
        repository_root: Path | None = None
        project_id: str | None = None
        if args.repository:
            repository_root, project_id = _repository_identity(args.repository)
        # The central Pi feedback store is intentionally global. --repository
        # narrows the view; --all-projects documents the default explicitly.
        records = _load_records(project_id if args.repository else None,
                                args.include_reviewed, args.limit)
    except (OSError, RuntimeError) as error:
        print(f"pi-harness-feedback: {error}", file=sys.stderr)
        return 2

    scope = str(repository_root) if repository_root is not None else "all projects"
    if args.format == "json":
        rendered = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    elif args.format == "markdown":
        rendered = _render_markdown(records, scope)
    else:
        rendered = _render_text(records, scope) + "\n"

    if args.output:
        assert repository_root is not None
        try:
            written_path = _write_output(args.output, repository_root, rendered)
        except OSError as error:
            print(f"pi-harness-feedback: cannot write output: {error}", file=sys.stderr)
            return 2
        except RuntimeError as error:
            print(f"pi-harness-feedback: {error}", file=sys.stderr)
            return 2
        print(f"Wrote sanitized harness feedback projection to {written_path}")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
