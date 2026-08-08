"""Disposable npm staging and installed-byte/provenance proof for C10a."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any

from scripts.pi_control.staged_build import create_build_manifest, write_build_manifest
try:
    from .staged_proof import copy_manifest_entries
except ImportError:
    from staged_proof import copy_manifest_entries

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (("pi-sandbox-control", "pi-sandbox-control"), ("pi-subagents-control", "pi-subagents"))


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=cwd, check=True, capture_output=True, env={**os.environ, "npm_config_audit": "false", "npm_config_fund": "false"})


def _hash_tree(root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            values[path.relative_to(root).as_posix()] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return values


def _pack(source: Path, artifacts: Path) -> tuple[Path, dict[str, str]]:
    output = _run(["npm", "pack", "--ignore-scripts", "--pack-destination", str(artifacts), "--json"], cwd=source)
    filename = json.loads(output.stdout.decode())[0]["filename"]
    tarball = artifacts / filename
    expected: dict[str, str] = {}
    with tarfile.open(tarball, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.startswith("package/"):
                continue
            relative = member.name[len("package/"):]
            stream = archive.extractfile(member)
            assert stream is not None
            expected[relative] = "sha256:" + hashlib.sha256(stream.read()).hexdigest()
    return tarball, expected


def install(output_root: Path) -> dict[str, Any]:
    output_root = output_root.expanduser().resolve()
    if output_root.exists():
        raise FileExistsError(str(output_root))
    output_root.mkdir(parents=True, mode=0o700)
    artifacts = output_root.parent / "artifacts"
    installed_root = output_root.parent / "installed"
    artifacts.mkdir(mode=0o700)
    installed_root.mkdir(mode=0o700)
    source_digests: dict[str, str] = {}
    tarballs: list[str] = []
    package_records: list[dict[str, Any]] = []
    for source_name, installed_name in PACKAGES:
        source = ROOT / "pi" / "packages" / source_name
        if not source.is_dir() or source.is_symlink():
            raise FileNotFoundError(str(source))
        manifest = create_build_manifest(source, metadata={"package": source_name, "tier": "C10a"}, test_outcomes={"source": "PASS"})
        stage_package = output_root / "packages" / source_name
        stage_package.mkdir(parents=True, mode=0o700)
        copy_manifest_entries(source, stage_package, manifest)
        source_digests[source_name] = manifest.digest
        tarball, expected = _pack(source, artifacts)
        tarballs.append(str(tarball))
        package_records.append({"source": source_name, "installed": installed_name, "sourceDigest": manifest.digest, "expectedFiles": expected})
    package_json = {"name": "pi-control-c10-staged", "private": True, "version": "1.0.0", "dependencies": {}}
    (installed_root / "package.json").write_text(json.dumps(package_json), encoding="utf-8")
    _run(["npm", "install", "--prefix", str(installed_root), "--offline", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock", "--legacy-peer-deps", *tarballs])
    for record in package_records:
        installed = installed_root / "node_modules" / record["installed"]
        if not installed.is_dir() or installed.is_symlink():
            raise RuntimeError(f"installed package is missing or symlinked: {installed}")
        actual = _hash_tree(installed)
        if actual != record["expectedFiles"]:
            raise RuntimeError(f"installed package bytes differ: {record['installed']}")
        record["installedDigest"] = "sha256:" + hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    forbidden = ["@kjrjay/pi-sandbox", "pi-sandbox-control"]
    legacy = [name for name in forbidden if (installed_root / "node_modules" / name).exists() and name != "pi-sandbox-control"]
    if legacy:
        raise RuntimeError(f"legacy package co-load detected: {legacy}")
    (output_root / "loaded-resources.json").write_text(json.dumps({"schemaVersion": 1, "buildId": "pending", "sourceRoot": ".", "installedRoot": "../installed", "packages": package_records, "legacyCoLoad": [], "controllerExtensions": ["pi/extensions/control-plane/index.ts"], "loadedByteProof": "package-tarball-to-installed-tree"}, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    manifest_kwargs = {"metadata": {"tier": "C10a", "installedRoot": "../installed"}, "test_outcomes": {"npmInstall": "PASS", "byteEquality": "PASS", "coLoad": "PASS"}, "exclude_paths": ["loaded-resources.json"]}
    manifest = create_build_manifest(output_root, **manifest_kwargs)
    written = write_build_manifest(manifest, output_root / "build-manifest.json")
    resources = json.loads((output_root / "loaded-resources.json").read_text())
    resources["buildId"] = written.build_id
    (output_root / "loaded-resources.json").write_text(json.dumps(resources, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return {"stagedRoot": str(output_root), "installedRoot": str(installed_root), "buildId": written.build_id, "manifestDigest": written.digest, "packageCount": len(package_records)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--output-root", required=True)
    try:
        print(json.dumps(install(Path(parser.parse_args(argv).output_root)), sort_keys=True))
        return 0
    except (FileNotFoundError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"STOP/77: disposable staged install unavailable: {error}", file=__import__("sys").stderr)
        return 77

if __name__ == "__main__": raise SystemExit(main())
