"""Test-only bridge helper: prepare one real controller run manifest.

Used by tests/pi-manifest-bridge.test.mjs so the sandbox manifest adapter is
exercised against a manifest produced by the current controller protocol
without materializing a full P1 stage. The registered-build verification is
bypassed exactly like the control-plane unit fixtures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.pi_control.pi_client import PiControllerClient
from scripts.pi_control.pi_store import PiStore
from scripts.pi_control.models import canonical_json, utc_now
from tests.pi_test_build import allow_test_only_registered_build_rows

_BUILD_ID = "build_" + "b" * 32
_DIGEST = "sha256:" + "a" * 64


def _host(role: str = "secretary") -> dict[str, object]:
    executable = Path("/usr/bin/true").resolve(strict=True)
    return {
        "executable": str(executable),
        "executableSha256": "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
        "argv": [str(executable)],
        "toolProfile": role,
        "environmentKeys": ["PATH"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    state = Path(args.state_root).absolute()
    repository = Path(args.repository).absolute()
    client = PiControllerClient(state)
    registered = client.register_project(str(repository))
    with allow_test_only_registered_build_rows():
        with PiStore(state) as store:
            build = state / "stage"
            build.mkdir(parents=True, exist_ok=True)
            (build / "build-manifest.json").write_text("test", encoding="utf-8")
            (build / "release-resources.json").write_text("test", encoding="utf-8")
            store.conn.execute(
                "INSERT INTO installed_builds(build_id,source_commit,source_tree_hash,build_manifest_path,build_manifest_digest,resource_manifest_path,resource_manifest_digest,pi_version,package_lock_hash,status,installed_at,activated_at,rollback_path,verification_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_BUILD_ID, None, _DIGEST, str(build / "build-manifest.json"), _DIGEST, str(build / "release-resources.json"), _DIGEST, "0.83.0", _DIGEST, "staged", utc_now(), None, None, canonical_json({"verified": True})),
            )
            conversation = store.conn.execute("SELECT * FROM conversations WHERE project_id=?", (registered["project_id"],)).fetchone()
            prepared = client.prepare_run(conversation_id=conversation["conversation_id"], build_id=_BUILD_ID, host_process=_host())
    print(json.dumps({
        "projectId": registered["project_id"],
        "manifestPath": prepared["manifestPath"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
