"""Compatibility CLI for the production Pi harness generation builder."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any

from scripts.pi_control.pi_install import InstallUnavailable, stage as build_generation


ROOT = Path(__file__).resolve().parents[2]
StagedInstallUnavailable = InstallUnavailable


def _raise_offline_install_error(error: subprocess.CalledProcessError) -> None:
    """Retained for callers checking STOP/77 versus integrity failure."""

    stdout = error.stdout if isinstance(error.stdout, bytes) else str(error.stdout or "").encode()
    stderr = error.stderr if isinstance(error.stderr, bytes) else str(error.stderr or "").encode()
    detail = (stderr + b"\n" + stdout).decode("utf-8", errors="replace")
    if "ENOTCACHED" in detail and "EINTEGRITY" not in detail:
        raise StagedInstallUnavailable("the offline npm cache cannot materialize the pinned dependency tree") from error
    raise RuntimeError(f"offline npm install failed integrity or resolution checks: {detail.strip()[-1024:]}") from error


def install(
    output_root: Path,
    *,
    pi_core_tarball: Path | None = None,
    npm_cache: Path | None = None,
) -> dict[str, Any]:
    return build_generation(ROOT, output_root, pi_core_tarball=pi_core_tarball, npm_cache=npm_cache)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(Path(args.output_root)), sort_keys=True))
        return 0
    except StagedInstallUnavailable as error:
        print(f"STOP/77: disposable staged install unavailable: {error}", file=__import__("sys").stderr)
        return 77
    except (FileNotFoundError, RuntimeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FAIL: disposable staged install is invalid: {error}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
