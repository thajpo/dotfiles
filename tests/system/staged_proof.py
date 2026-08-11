"""Verify an exact production generation and bind separate runtime evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from scripts.pi_control.greenfield_install import verify_stage


def prove_loaded_root(root: Path, loaded_build_id: str | None) -> dict[str, Any]:
    verified = verify_stage(root)
    if not loaded_build_id:
        raise RuntimeError("STOP/77: pinned Pi loaded-build attestation is unavailable")
    if loaded_build_id != verified["buildId"]:
        raise ValueError("loaded Pi build ID does not match staged manifest")
    return {**verified, "loaded": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    try:
        result = prove_loaded_root(Path(args.root).expanduser().resolve(), os.environ.get("PI_SYSTEM_LOADED_BUILD_ID"))
        print(json.dumps(result, sort_keys=True))
        return 0
    except RuntimeError as error:
        if str(error).startswith("STOP/77:"):
            print(str(error), file=__import__("sys").stderr)
            return 77
        print(str(error), file=__import__("sys").stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
