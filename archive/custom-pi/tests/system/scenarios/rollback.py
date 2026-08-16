from __future__ import annotations

try:
    from tests.system.rollback_matrix import run_matrix
except ImportError:
    from rollback_matrix import run_matrix

ROLLBACK_MATRIX = (
    {"boundary": "prepare", "restore": ("files", "modes", "symlinks"), "preserve": ("db", "refs", "worktrees", "evidence")},
    {"boundary": "package-swap", "restore": ("installed-tree", "config", "package-links"), "preserve": ("db", "refs", "worktrees", "evidence")},
    {"boundary": "image-swap", "restore": ("image-selection",), "preserve": ("db", "refs", "worktrees", "evidence")},
    {"boundary": "post-verify", "restore": ("files", "modes", "symlinks", "image-selection"), "preserve": ("db", "refs", "worktrees", "evidence")},
    {"boundary": "rollback", "restore": ("files", "modes", "symlinks", "config", "package-links", "image-selection"), "preserve": ("db", "refs", "worktrees", "evidence")},
)

def run(*, capability: bool = False):
    if not capability:
        return {"scenarioId": "ROLLBACK-MATRIX", "status": "STOP", "reason": "Docker/staged install prerequisite is unavailable", "matrix": list(ROLLBACK_MATRIX)}
    return {"scenarioId": "ROLLBACK-MATRIX", "status": "PASS", "reason": None, "matrix": run_matrix()}
