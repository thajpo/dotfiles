"""Immutable conversation role policy for the Pi controller."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class RoleProfile:
    role: str
    authority_profile: str
    scope_source: str
    working_copy_purpose: str | None
    working_copy_required: bool


ROLE_PROFILES: Mapping[str, RoleProfile] = MappingProxyType({
    "secretary": RoleProfile("secretary", "host-read-only", "project-primary", None, False),
    "investigator": RoleProfile("investigator", "host-read-only", "controller-assignment", None, True),
    "reviewer": RoleProfile("reviewer", "host-read-only", "controller-assignment", "review", True),
    "personal": RoleProfile("personal", "writer-container", "assigned-working-copy", "personal", True),
    "workstream": RoleProfile("workstream", "writer-container", "assigned-working-copy", "workstream", True),
    "integration": RoleProfile("integration", "writer-container", "assigned-working-copy", "integration", True),
})


def role_profile(role: str) -> RoleProfile:
    try:
        return ROLE_PROFILES[role]
    except (KeyError, TypeError) as error:
        raise ValueError("conversation role is not supported") from error


def validate_role_assignment(profile: RoleProfile, working_copy: Mapping[str, Any] | None) -> None:
    if profile.working_copy_required and working_copy is None:
        raise ValueError(f"{profile.role} conversations require a controller-assigned working copy")
    if not profile.working_copy_required and working_copy is not None:
        raise ValueError(f"{profile.role} conversations cannot own a working copy")
    if working_copy is None:
        return
    if profile.working_copy_purpose is not None and working_copy["purpose"] != profile.working_copy_purpose:
        raise ValueError(f"{profile.role} conversation working-copy purpose does not match role")
    if profile.authority_profile == "writer-container" and working_copy["effective_mode"] == "read-only":
        raise ValueError("writer conversations require a writable working-copy mode")
    if profile.role == "reviewer" and working_copy["effective_mode"] != "read-only":
        raise ValueError("reviewer conversations require a read-only assignment")


__all__ = ["ROLE_PROFILES", "RoleProfile", "role_profile", "validate_role_assignment"]
