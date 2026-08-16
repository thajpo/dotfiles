"""Deterministic external npm/Python artifact caches for P6 acceptance."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile


NODE_IMAGE_CONFIG = "sha256:dd46de48f5b716df050c50afd6a80bccbfc17127fb6abee0499777d9ae6bb2f6"
NODE_IMAGE_REFERENCE = NODE_IMAGE_CONFIG
PYTHON_IMAGE_CONFIG = "sha256:1455a91ef4da65c2451f26ef959105bd6b21fe3a6445326649a341fa01f672d3"
PYTHON_IMAGE_REFERENCE = "python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write(path: Path, body: bytes, mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(body)
    os.chmod(path, mode)


def _npm_tarball() -> bytes:
    package = _canonical({"name": "p6-tiny-npm", "version": "1.0.0", "main": "index.js"})
    index = b"module.exports = { version: '1.0.0' };\n"
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, body in (("package/package.json", package), ("package/index.js", index)):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def _record_hash(value: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode("ascii").rstrip("=")


def _python_wheel() -> bytes:
    files = {
        "p6_tiny_python/__init__.py": b"__version__ = '1.0.0'\n",
        "p6_tiny_python-1.0.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: p6-tiny-python\nVersion: 1.0.0\n",
        "p6_tiny_python-1.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: pi-p6-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    records = [f"{name},{_record_hash(body)},{len(body)}" for name, body in sorted(files.items())]
    record_name = "p6_tiny_python-1.0.0.dist-info/RECORD"
    files[record_name] = ("\n".join(records + [f"{record_name},,"]) + "\n").encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, body)
    return output.getvalue()


def create_package_caches(root: Path, state_root: Path) -> dict[str, object]:
    cache = root / "package-cache"
    cache.mkdir(mode=0o700)
    npm_path = cache / "npm/p6-tiny-npm-1.0.0.tgz"
    python_path = cache / "python/p6_tiny_python-1.0.0-py3-none-any.whl"
    npm_body = _npm_tarball()
    python_body = _python_wheel()
    _write(npm_path, npm_body)
    _write(python_path, python_body)
    inventory = {
        "schemaVersion": 1,
        "artifacts": [
            {"ecosystem": "npm", "path": "npm/p6-tiny-npm-1.0.0.tgz", "sha256": _sha256(npm_body), "size": len(npm_body)},
            {"ecosystem": "python", "path": "python/p6_tiny_python-1.0.0-py3-none-any.whl", "sha256": _sha256(python_body), "size": len(python_body)},
        ],
    }
    inventory_body = _canonical(inventory)
    _write(cache / "inventory.json", inventory_body)
    for directory in (cache / "npm", cache / "python", cache):
        os.chmod(directory, 0o500)
    marker = state_root / ".pi-package-cache-test-fixture"
    _write(marker, b"P6-NONPRODUCTION-PACKAGE-CACHE\n", 0o600)
    policy = {
        "schemaVersion": 1,
        "testOnly": True,
        "cacheRoot": str(cache),
        "inventoryDigest": _sha256(inventory_body),
        "ecosystems": {
            "npm": {"imageReference": NODE_IMAGE_REFERENCE, "imageConfigId": NODE_IMAGE_CONFIG, "platform": "linux/amd64", "allowLocalConfigIdOnly": True},
            "python": {"imageReference": PYTHON_IMAGE_REFERENCE, "imageConfigId": PYTHON_IMAGE_CONFIG, "platform": "linux/amd64", "allowLocalConfigIdOnly": False},
        },
    }
    _write(state_root / "package-cache-policy.json", _canonical(policy), 0o600)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(npm_body).digest()).decode("ascii")
    return {
        "cacheRoot": str(cache), "inventoryDigest": policy["inventoryDigest"], "npmIntegrity": integrity,
        "pythonSha256": hashlib.sha256(python_body).hexdigest(), "policy": policy,
    }


__all__ = ["NODE_IMAGE_CONFIG", "NODE_IMAGE_REFERENCE", "PYTHON_IMAGE_CONFIG", "PYTHON_IMAGE_REFERENCE", "create_package_caches"]
