"""Package manifest and lock-file identity helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def file_digest(path: str | Path) -> str | None:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        return None
    return "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()


def package_files(root: str | Path) -> list[tuple[str, str, str]]:
    base = Path(root).resolve(strict=True)
    candidates = {
        "npm": ("package.json", "package-lock.json"),
        "python": ("pyproject.toml", "uv.lock"),
        "cargo": ("Cargo.toml", "Cargo.lock"),
        "go": ("go.mod", "go.sum"),
    }
    result: list[tuple[str, str, str]] = []
    for ecosystem, (manifest_name, lock_name) in candidates.items():
        manifest = base / manifest_name
        if manifest.is_file() and not manifest.is_symlink():
            result.append((ecosystem, manifest_name, lock_name if (base / lock_name).is_file() else ""))
    return result


def package_identity(root: str | Path, *, platform: str, image_config_id: str) -> dict[str, Any]:
    base = Path(root).resolve(strict=True)
    found = package_files(base)
    if not found:
        return {"ecosystem": "none", "manifestPath": None, "manifestDigest": None, "lockPath": None, "lockDigest": None, "platform": platform, "imageConfigId": image_config_id}
    ecosystem, manifest_name, lock_name = found[0]
    manifest = base / manifest_name
    lock = base / lock_name if lock_name else None
    return {"ecosystem": ecosystem, "manifestPath": manifest_name, "manifestDigest": file_digest(manifest), "lockPath": lock_name or None, "lockDigest": file_digest(lock) if lock else None, "platform": platform, "imageConfigId": image_config_id}


__all__ = ["file_digest", "package_files", "package_identity"]
