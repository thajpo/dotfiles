"""Exact presentation planning; never treats panes as lifecycle authority."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import PresentationUnknownError, WorkstreamConflictError
from .workstreams import update_presentation_assignment


def observe_presentation(*, backend: str, locator: Mapping[str, Any] | None, process_state: str) -> dict[str, Any]:
    if backend not in {"tmux", "herdr"}: raise PresentationUnknownError("presentation backend is unsupported")
    if process_state not in {"present", "missing", "drifted", "unknown"}: raise PresentationUnknownError("presentation process state is invalid")
    if process_state == "unknown": raise PresentationUnknownError("presentation observation is unknown")
    return {"backend": backend, "observedState": "present" if process_state == "present" else process_state, "locator": dict(locator or {}), "authority": "observation-only"}


def assign_presentation(store: Any, presentation_assignment_id: str, *, expected_resource_version: int, backend: str, locator: Mapping[str, Any], process_state: str = "present") -> dict[str, Any]:
    observation = observe_presentation(backend=backend, locator=locator, process_state=process_state)
    return update_presentation_assignment(store, presentation_assignment_id, expected_resource_version=expected_resource_version, updates={"observedState": observation["observedState"], "locator": observation["locator"]})


def plan_restart(*, managed_session: str, observed_session: str | None, process_state: str, unrelated_sessions: list[str] | None = None) -> dict[str, Any]:
    if not managed_session or observed_session != managed_session or process_state == "unknown":
        raise PresentationUnknownError("exact managed presentation cannot be proven")
    if unrelated_sessions and managed_session in unrelated_sessions:
        raise WorkstreamConflictError("managed presentation overlaps unrelated session")
    return {"action": "restart-exact-managed-session", "session": managed_session, "commands": ["stop-exact-process", "verify-stopped", "start-exact-session"], "broadKill": False}


def plan_swap(*, exact_process_state: str, live_worker: bool = False) -> dict[str, Any]:
    if exact_process_state != "stopped" or live_worker:
        raise PresentationUnknownError("presentation swap requires exact quiescence")
    return {"action": "swap-presentation", "liveWorkerPreserved": True, "requiresReobserve": True}


__all__ = ["assign_presentation", "observe_presentation", "plan_restart", "plan_swap"]
