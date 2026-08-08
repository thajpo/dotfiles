"""Read-only typed legacy migration adapters."""

from .base import AdapterError, AdapterRecord, AdapterResult
from .artifacts import observe as observe_artifacts
from .backups import observe as observe_backups
from .docker import observe as observe_docker
from .git import observe as observe_git
from .herdr import observe as observe_herdr
from .installed_build import observe as observe_installed_build
from .policy import observe as observe_policy
from .processes import observe as observe_processes
from .root_sessions import observe as observe_root_sessions
from .routes_leases import observe as observe_routes_leases
from .secretary import observe as observe_secretary
from .tmux import observe as observe_tmux

__all__ = [
    "AdapterError", "AdapterRecord", "AdapterResult", "observe_artifacts",
    "observe_backups", "observe_docker", "observe_git", "observe_herdr",
    "observe_installed_build", "observe_policy", "observe_processes",
    "observe_root_sessions", "observe_routes_leases", "observe_secretary",
    "observe_tmux",
]
