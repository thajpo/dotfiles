"""Sole production composition root for Pisec adapters."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .adapters import AdapterRegistry
from .broker import BrokerDispatcher
from .config import PisecConfig
from .git_objects import GitObjectManager
from .harnesses.codex import CodexHarnessAdapter
from .harnesses.omp import OmpHarnessAdapter
from .pi_store import PiStore
from .workspaces.herdr import HerdrWorkspaceAdapter


def build_adapters(
    config: PisecConfig,
    state_root: Path | str,
    *,
    store_factory: Callable[[], PiStore] | None = None,
) -> BrokerDispatcher:
    registry = AdapterRegistry()
    harness = OmpHarnessAdapter(state_root=state_root, config=config)
    workspace = HerdrWorkspaceAdapter.from_config(config["workspace"]["config"], validate=False)
    registry.register_harness(harness)
    if "codex" in config.worker_harnesses:
        registry.register_harness(CodexHarnessAdapter(state_root=state_root, config=config))
    registry.register_workspace(workspace)
    selected_harness = registry.resolve_harness(config["harness"]["id"])
    selected_workspace = registry.resolve_workspace(config["workspace"]["id"])
    factory = store_factory or (lambda: PiStore(state_root))
    return BrokerDispatcher(
        factory,
        registry=registry,
        harness=selected_harness,
        workspace=selected_workspace,
        git_objects=GitObjectManager(state_root=state_root),
        config=config,
    )

def run_broker() -> None:
    from .broker import BrokerService, default_runtime_root
    from .config import load_config
    from .pi_store import default_state_root

    state_root = default_state_root()
    dispatcher = build_adapters(load_config(), state_root)
    service = BrokerService(dispatcher, runtime_root=default_runtime_root())
    dispatcher.wait_for_workspace()
    service.start()
    try:
        dispatcher.startup_reconcile()
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
