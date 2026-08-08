"""Exact disposable source/stage/loaded-byte checks for C10a.

The module never writes to the repository or live install roots.  It is also
usable by the staged runner when a caller supplies an explicit disposable
staging root and loaded-resource attestation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any

from scripts.pi_control.staged_build import BuildManifest, create_build_manifest, load_build_manifest, write_build_manifest


def copy_manifest_entries(source: Path, destination: Path, manifest: BuildManifest) -> None:
    for entry in manifest.payload["files"]:
        relative = Path(entry["path"])
        source_path = source / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "symlink":
            os.symlink(entry["target"], target)
        else:
            shutil.copy2(source_path, target, follow_symlinks=False)
            os.chmod(target, entry["mode"])


def create_disposable_proof(source: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pi-c10a-") as raw:
        stage = Path(raw) / "stage"
        stage.mkdir(mode=0o700)
        manifest = create_build_manifest(source, repository=source, metadata={"tier": "C10a"}, test_outcomes={"source": "PASS"})
        copy_manifest_entries(source, stage, manifest)
        written = write_build_manifest(manifest, stage / "build-manifest.json")
        loaded = load_build_manifest(stage / "build-manifest.json")
        loaded.verify_files(stage, exclude=["build-manifest.json", "loaded-resources.json"])
        return {"buildId": loaded.build_id, "manifestDigest": loaded.digest, "fileCount": len(written.payload["files"])}


def prove_loaded_root(root: Path, loaded_build_id: str | None) -> dict[str, Any]:
    manifest = load_build_manifest(root / "build-manifest.json")
    manifest.verify_files(root, exclude=["build-manifest.json", "loaded-resources.json"])
    resources_path = root / "loaded-resources.json"
    if not resources_path.is_file():
        raise ValueError("loaded resource attestation is missing")
    resources = json.loads(resources_path.read_text(encoding="utf-8"))
    if resources.get("buildId") != manifest.build_id or resources.get("legacyCoLoad") != []:
        raise ValueError("loaded resource attestation is invalid or includes legacy co-load")
    if not loaded_build_id:
        raise RuntimeError("STOP/77: pinned Pi loaded-build attestation is unavailable")
    if loaded_build_id != manifest.build_id:
        raise ValueError("loaded Pi build ID does not match staged manifest")
    return {"buildId": manifest.build_id, "manifestDigest": manifest.digest, "fileCount": len(manifest.payload["files"]), "loaded": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            result = create_disposable_proof(Path(__file__).resolve().parents[2] / "pi" / "packages" / "pi-sandbox-control")
        elif args.root:
            result = prove_loaded_root(Path(args.root).expanduser().resolve(), os.environ.get("PI_SYSTEM_LOADED_BUILD_ID"))
        else:
            parser.error("--root or --self-test is required")
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
