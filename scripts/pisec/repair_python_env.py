"""Attach an approved python env to existing workstreams (one-time repair).

Amends the immutable scope trail of already-created workers so a subsequent
`pisec project refresh --all` re-renders their Fence policies with the python
env (and its interpreter home) exposed read-only.

Usage:
  python3 scripts/pisec/repair_python_env.py <workstream_id>... --env <absolute-path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .events import append_event_in_transaction
from .fence import resolve_python_env_paths
from .models import NeedsAttentionError, canonical_json, json_digest, utc_now, validate_id
from .pi_store import PiStore


def _scope_digest(scope: dict) -> str:
    return json_digest(scope)


def attach_python_env(store: PiStore, workstream_id: str, python_env: str) -> dict:
    workstream_id = validate_id(workstream_id, prefix="ws")
    resolved_env = resolve_python_env_paths(python_env)
    normalized = resolved_env[0]
    operation = store.conn.execute(
        "SELECT * FROM operations WHERE workstream_id=? AND kind='workstream.create'",
        (workstream_id,),
    ).fetchone()
    if operation is None:
        raise ValueError(f"no workstream.create operation for {workstream_id}")
    scope = json.loads(operation["result_json"])
    if not isinstance(scope, dict) or scope.get("workstreamId") != workstream_id:
        raise ValueError(f"stored scope for {workstream_id} is invalid")
    scope["pythonEnv"] = normalized
    digest = _scope_digest(scope)
    with store.transaction():
        store.conn.execute(
            "UPDATE operations SET result_json=?,updated_at=? WHERE operation_id=?",
            (canonical_json(scope), utc_now(), operation["operation_id"]),
        )
        authorization = store.conn.execute(
            "SELECT authorization_id FROM authorizations WHERE operation_id=?", (operation["operation_id"],)
        ).fetchone()
        if authorization is not None:
            store.conn.execute(
                "UPDATE authorizations SET scope_json=?,scope_sha256=?,consumed_at=consumed_at WHERE authorization_id=?",
                (canonical_json(scope), digest, authorization["authorization_id"]),
            )
        append_event_in_transaction(
            store.conn,
            kind="workstream.python_env_attached",
            project_id=operation["project_id"],
            workstream_id=workstream_id,
            operation_id=operation["operation_id"],
            payload=canonical_json({"pythonEnv": normalized, "readPaths": resolved_env}),
        )
    return {"workstreamId": workstream_id, "pythonEnv": normalized, "readPaths": resolved_env}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Attach an approved python env to existing workstreams")
    parser.add_argument("workstream_ids", nargs="+", help="workstream ids to repair (ws_...)")
    parser.add_argument("--env", required=True, help="absolute path of the approved python env (venv or interpreter dir)")
    parser.add_argument("--state", default=None, help="pisec state root (default: user state root)")
    args = parser.parse_args(argv)
    try:
        store = PiStore(args.state)
        with store:
            for workstream_id in args.workstream_ids:
                result = attach_python_env(store, workstream_id, args.env)
                print(canonical_json(result))
    except NeedsAttentionError as error:
        print(f"repair rejected: {error}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as error:
        print(f"repair failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
