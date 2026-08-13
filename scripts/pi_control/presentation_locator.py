"""Versioned presentation locators: pure parse/build helpers.

A locator is an observation snapshot of where one controller conversation is
presented. It is never authority: conversation identity comes from the
controller, and a locator only names the tmux session/window/pane where the
exact conversation is (or was) presented. Version 1 is the only accepted
shape; legacy unversioned shapes parse to None and are rewritten after the
next observation.
"""

from __future__ import annotations

from typing import Any, Mapping

LOCATOR_VERSION = 1

_REQUIRED = (
    "backend", "surface", "session", "window", "pane",
    "projectId", "conversationId", "role", "layout",
    "argvDigest",
)


def build_locator(
    *,
    surface: str,
    session: str,
    window: str,
    pane: str | None,
    project_id: str,
    conversation_id: str,
    role: str,
    layout: str,
    argv_digest: str,
    workstream_id: str | None = None,
    owner_pid: int | None = None,
    owner_start_identity: str | None = None,
) -> dict[str, Any]:
    """Build one version-1 locator. All identity fields are controller-owned."""
    if pane is None:
        raise ValueError("a locator requires a pane id or an empty presentation marker")
    value: dict[str, Any] = {
        "version": LOCATOR_VERSION,
        "backend": "tmux",
        "surface": surface,
        "session": session,
        "window": window,
        "pane": pane,
        "projectId": project_id,
        "conversationId": conversation_id,
        "role": role,
        "layout": layout,
        "argvDigest": argv_digest,
    }
    if workstream_id is not None:
        value["workstreamId"] = workstream_id
    if owner_pid is not None:
        value["ownerPid"] = owner_pid
    if owner_start_identity is not None:
        value["ownerStartIdentity"] = owner_start_identity
    return value


def parse_locator(value: Mapping[str, Any] | str | None) -> dict[str, Any] | None:
    """Parse one locator; None means unversioned, malformed, or absent.

    The parsed locator is a projection for observation; callers must never
    derive lifecycle identity from it.
    """
    if value is None:
        return None
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
    elif isinstance(value, Mapping):
        parsed = dict(value)
    else:
        return None
    if not isinstance(parsed, dict) or parsed.get("version") != LOCATOR_VERSION:
        return None
    if parsed.get("backend") != "tmux":
        return None
    if any(not isinstance(parsed.get(key), str) or not parsed[key] for key in _REQUIRED):
        return None
    return parsed


def locator_matches(locator: Mapping[str, Any], *, session: str, window: str, pane: str) -> bool:
    """True when the locator names the exact current presentation target."""
    parsed = parse_locator(locator)
    if parsed is None:
        return False
    return parsed["session"] == session and parsed["window"] == window and parsed["pane"] == pane


__all__ = ["LOCATOR_VERSION", "build_locator", "locator_matches", "parse_locator"]
