"""Refresh active runtime bindings while the updater owns the control-plane lock."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .bootstrap import build_adapters
from .config import load_config
from .pi_store import PiStore
from .refresh import refresh_runtimes


def refresh_for_update(state_root: Path, wait_seconds: float) -> dict:
    with PiStore(state_root) as store:
        active = store.conn.execute(
            "SELECT 1 FROM runtime_bindings r JOIN workstreams w USING(workstream_id) "
            "WHERE w.desired_state='active' AND w.provisioning_state='bound' LIMIT 1"
        ).fetchone()
    if active is None:
        return {"generation": None, "upgraded": [], "pending": [], "skipped": [], "failed": [], "ok": True}
    dispatcher = build_adapters(load_config(), state_root)
    dispatcher.wait_for_workspace()
    with PiStore(state_root) as store:
        return refresh_runtimes(
            store,
            dispatcher.harness,
            dispatcher.workspace,
            wait_seconds=wait_seconds,
            harness_resolver=lambda workstream_id: dispatcher._harness_for_workstream(store, workstream_id),
            surface_resolver=dispatcher._surface_for_harness,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pisec update-refresh")
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    result = refresh_for_update(args.state_root, args.wait_seconds)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") is True and not result.get("failed") and not result.get("pending") else 1


if __name__ == "__main__":
    raise SystemExit(main())
