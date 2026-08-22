"""Single source of truth for Pisec socket operation contracts."""
from __future__ import annotations

from typing import Any

SOCKET_OPERATIONS = {
    "admin": frozenset({
        "project.register", "project.list", "project.open", "project.activity", "fleet.activity",
        "project.refresh", "project.deactivate", "project.activate", "secretary.ensure", "secretary.focus",
        "first_mate.ensure", "first_mate.focus", "workstream.focus", "workstream.cleanup", "system.status",
        "system.reconcile", "system.doctor", "workspace.startup", "workspace.event", "presentation.snapshot",
    }),
    "secretary": frozenset({
        "project.status", "project.activity", "issue.report", "secretary.issue.report", "issue.list", "issue.inspect", "issue.add_context", "issue.verify", "access.list", "git.status", "git.push", "git.workstream_changes",
        "git.merge.prepare", "git.merge.apply", "workstream.list", "workstream.inspect", "workstream.prepare",
        "workstream.authorize_apply", "workstream.send", "workstream.focus", "workstream.complete",
        "workstream.retire", "coordination.list", "coordination.inspect", "coordination.answer", "decision.list",
        "decision.record", "decision.resolve", "research.list", "research.inspect", "research.claim",
        "research.request_context", "research.answer", "research.decline",
    }),
    "fleet": frozenset({
        "fleet.status", "fleet.activity", "fleet.events", "fleet.issue.list", "fleet.issue.inspect", "fleet.issue.add_context", "fleet.issue.acknowledge", "fleet.issue.resolve",
        "fleet.access.list", "fleet.access.inspect", "fleet.access.grant.prepare", "fleet.access.grant.apply", "fleet.access.revoke.prepare", "fleet.access.revoke.apply",
        "fleet.secretary.send", "fleet.workstream.list", "fleet.workstream.inspect", "fleet.git.workstream_changes", "fleet.workstream.prepare",
        "fleet.workstream.authorize_apply", "fleet.git.merge.prepare", "fleet.git.merge.apply",
    }),
    "runtime": frozenset({
        "runtime.report", "runtime.turn.prepare", "session.switch.prepare", "task.get", "runtime.bootstrap.get",
        "runtime.bootstrap.ack", "issue.report", "issue.list", "issue.inspect", "issue.add_context", "issue.verify", "access.effective",
        "workstream.checkpoint", "workstream.completion.submit", "coordination.request", "coordination.list", "coordination.inspect", "coordination.resolve",
        "research.request", "research.list", "research.inspect", "research.add_context", "research.acknowledge",
    }),
}


def operation_manifest() -> dict[str, list[str]]:
    """Return a stable, JSON-safe manifest for doctor and parity checks."""
    return {socket: sorted(operations) for socket, operations in SOCKET_OPERATIONS.items()}


def operation_allowed(socket: str, operation: str) -> bool:
    return operation in SOCKET_OPERATIONS.get(socket, ())
