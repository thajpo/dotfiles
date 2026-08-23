from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import socket
import socketserver
import tarfile
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

from scripts.pisec import doctor
from scripts.pisec.host_config import patch_herdr_config, patch_pisec_config, write_collie_env
from scripts.pisec.pi_store import PiStore
from scripts.pisec.adapters import AdapterHealth, AdapterRegistry, HarnessManifest, WorkspaceManifest
ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "agent-workflow-install.sh"


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Tailscale-User-Login") == "untrusted@example.invalid":
            status = 403
        elif self.headers.get("Host") == "wrong.example.invalid":
            status = 403
        elif self.headers.get("Origin") == "https://wrong.example.invalid":
            status = 403
        else:
            status = 200
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok\n")

    def log_message(self, *_args):
        pass


class FakeAdminHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = json.loads(self.rfile.readline())
        if request["operation"] == "system.doctor":
            result = {"ok": True}
        elif request["operation"] == "runtime.release.build":
            result = {"release_id": "rel_" + "a" * 32, "content_sha256": "a" * 64, "reused": False}
        elif request["operation"] == "runtime.release.activate":
            result = {"release_id": request["payload"]["releaseId"], "content_sha256": "a" * 64, "activated": True}
        elif request["operation"] == "project.refresh":
            result = {"ok": True, "generation": None, "upgraded": [], "pending": [], "skipped": [], "failed": []}
        else:
            result = {}
        response = {
            "protocolVersion": 1,
            "requestId": request["requestId"],
            "ok": True,
            "result": result,
        }
        self.wfile.write((json.dumps(response) + "\n").encode())


class FakeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.collie_dir = self.home / ".config" / "herdr" / "plugins" / "config" / "herdr.collie"
        self.collie_dir.mkdir(parents=True)
        self.collie_source_dir = self.home / ".config" / "herdr" / "plugins" / "github" / "herdr.collie"
        self._write_collie_source()
        self._make_treehouse_archive()
        self._write_commands()
        self.runtime_root = self.root / "runtime"
        self._sockets = []
        self._threads = []
        self._start_unix(self.runtime_root / "admin" / "control.sock", FakeAdminHandler)
        self._start_unix(self.runtime_root / "secretary" / "control.sock")
        self._start_unix(self.runtime_root / "runtime" / "control.sock")
        self.herdr_socket = self.home / ".config" / "herdr" / "sessions" / "main" / "herdr.sock"
        self._start_unix(self.herdr_socket)
        self.health_servers = []
        self.ports = {}
        for name in ("PISEC_AUTH_BROKER_PORT", "PISEC_AUTH_GATEWAY_PORT", "PISEC_COLLIE_PORT"):
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            self.ports[name] = probe.getsockname()[1]
            probe.close()
        for port in self.ports.values():
            server = ThreadingHTTPServer(("127.0.0.1", port), HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.health_servers.append(server)
            self._threads.append(thread)
        state = self.root / "state" / "pisec"
        with PiStore(state):
            pass
        token = self.home / ".omp" / "auth-broker.token"
        token.parent.mkdir(parents=True)
        token.write_text("b" * 48 + "\n")
        os.chmod(token, 0o600)

    def tearDown(self):
        for server in self.health_servers:
            server.shutdown()
            server.server_close()
        for server in self._sockets:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self.temp.cleanup()

    def _start_unix(self, path, handler=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        server = socketserver.ThreadingUnixStreamServer(str(path), handler or socketserver.StreamRequestHandler)
        os.chmod(path, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._sockets.append(server)
        self._threads.append(thread)

    def _write_command(self, name, content):
        path = self.fake_bin / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + content)
        os.chmod(path, 0o700)

    def _write_collie_source(self):
        bridge = self.collie_source_dir / "bridge"
        bridge.mkdir(parents=True)
        (bridge / "activity.ts").write_text(
            '''import { mkdir, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { Config } from "./config.ts";

// When did each pane last DO something, and when did you last LOOK at it? Herdr answers neither —
// its pane records carry no timestamps at all (HERDR_API.md) — so Collie derives and owns both.
//
// Two numbers per pane are enough for the whole dashboard:
//   • activeAt — the last agent status transition this bridge observed
//   • seenAt   — the last time you opened or drove the pane THROUGH COLLIE
//
// "Unseen" is then a comparison, not a stored fact: an agent is newly-finished-and-unread exactly
// when `status === "done" && activeAt > seenAt`. Opening the pane sets seenAt = now, and the row
// leaves the section on its own — nothing to mark read, nothing to keep in sync.
//
// Bridge-side and shared across devices on purpose, and deliberately blind to what you do at the
// desk in Herdr itself — see .adr/0003-one-shared-seen.md. Persisted to the state dir like Snooze
// and NotifyPrefsStore, so it survives the `systemctl restart` every backend change needs.

/** The two timestamps Collie keeps for a pane. Epoch ms. */
export interface PaneActivity {
  /** Last agent status transition observed by the state engine. */
  activeAt: number;
  /** Last time you opened or drove this pane through Collie. */
  seenAt: number;
}

/** Disk shape: session name → pane id → activity. */
export type ActivityFile = Record<string, Record<string, PaneActivity>>;


'''
        )
        (bridge / "activity.test.ts").write_text(
            '''import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  ActivityLedger,
  coerceActivityFile,
  meaningfulTabLabel,
  PRUNE_AFTER_MS,
} from "./activity.ts";

function ledger(start = 1_000_000) {
  return start;
}

/** The one derivation the whole feature rests on. Mirrors the client's `isUnseen`. */
const unseen = (a: { activeAt: number; seenAt: number } | undefined) =>
  !!a && a.activeAt > a.seenAt;

describe("meaningfulTabLabel", () => {
  test("keeps a real name", () => {
    expect(meaningfulTabLabel("fix-auth", 1)).toBe("fix-auth");

  });
});
'''
        )
        (bridge / "server.ts").write_text(
            '''import { mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { extname, join, normalize, sep } from "node:path";
import type { ActivityLedger } from "./activity.ts";
import type { AuditLog } from "./audit.ts";
import type { Config } from "./config.ts";
import type { HerdrClient, PaneRead } from "./herdr-client.ts";
''' + "\n" * 171 + '''
export function startServer(opts: {
        if (!rt) return unknownSession();
        const { agents, shellPanes, workspaces, tabs, bridge } = rt.engine.current();
        const device = deviceAuth(req, cfg);
        // Attach each pane's activity timestamps. Done here rather than in the state engine so the
        // engine stays a pure Herdr-poller with no knowledge of the ledger — and so the two numbers
        // are read at serialise time, i.e. as fresh as the request.
        const withActivity = (p: AgentView): AgentView => {
          const a = activity.get(rt.name, p.paneId);
          return a ? { ...p, lastActiveAt: a.activeAt, lastSeenAt: a.seenAt } : p;
        };
        // Tag every snapshot poll with the on-disk build id so an open client notices a live rebuild
        // between polls — the no-service-worker self-update path (web/src/lib/self-update.ts).
        return withBuildHeader(
}
'''
        )

    def _make_treehouse_archive(self):
        source = self.root / "treehouse"
        source.write_text('#!/bin/sh\nif [ "${1:-}" = "--version" ]; then printf "v2.1.1\\n"; else printf "treehouse fixture\\n"; fi\n')
        os.chmod(source, 0o755)
        self.treehouse_archive = self.root / "treehouse-v2.1.1-linux-amd64.tar.gz"
        with tarfile.open(self.treehouse_archive, "w:gz") as archive:
            archive.add(source, arcname="treehouse", recursive=False)

    def _write_commands(self):
        self._write_command("curl", r'''
output=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--output" ]]; then output="$2"; shift 2; else shift; fi
done
[[ -n "$output" ]] || exit 2
cp "$FAKE_TREEHOUSE_ARCHIVE" "$output"
''')
        self._write_command("sha256sum", r'''
[[ "${1:-}" == "-c" && -n "${2:-}" ]] || exit 2
cat "$2" > "$TREEHOUSE_CHECKSUM_LOG"
if [[ "${TREEHOUSE_CHECKSUM_FAIL:-0}" == "1" ]]; then exit 1; fi
''')
        self._write_command("herdr-fixture", r'''
if [[ "${1:-}" == "--version" ]]; then echo "herdr 0.8.0"; exit 0; fi
if [[ "${1:-}" == "plugin" ]]; then
  config_root="$HOME/.config/herdr/plugins/config"
  github_root="$HOME/.config/herdr/plugins/github"
  collie_dir="$config_root/herdr.collie"
  case "${2:-}" in
    list)
      plugins=""
      append_plugin() {
        if [[ -n "$plugins" ]]; then plugins+=","; fi
        plugins+="$1"
      }
      if [[ -f "$collie_dir/installed" ]]; then
        append_plugin "{\"plugin_id\":\"herdr.collie\",\"version\":\"0.28.0\",\"plugin_root\":\"$github_root/herdr.collie\"}"
      fi
      if [[ -f "$config_root/chmarax.herdr-nvim/installed" ]]; then
        append_plugin "{\"plugin_id\":\"chmarax.herdr-nvim\",\"version\":\"0.1.1\",\"plugin_root\":\"$github_root/chmarax.herdr-nvim\",\"manifest_path\":\"$github_root/chmarax.herdr-nvim/herdr-plugin.toml\"}"
      fi
      if [[ -f "$config_root/persiyanov.reviewr/installed" ]]; then
        append_plugin "{\"plugin_id\":\"persiyanov.reviewr\",\"version\":\"0.32.1\",\"plugin_root\":\"$github_root/persiyanov.reviewr\",\"manifest_path\":\"$github_root/persiyanov.reviewr/herdr-plugin.toml\"}"
      fi
      printf '{"result":{"plugins":[%s]}}\n' "$plugins"
      ;;
    install)
      case "${3:-}" in
        AltanS/collie) plugin_id="herdr.collie" ;;
        ChmaraX/herdr-nvim) plugin_id="chmarax.herdr-nvim" ;;
        persiyanov/herdr-reviewr) plugin_id="persiyanov.reviewr" ;;
        *) exit 2 ;;
      esac
      mkdir -p "$config_root/$plugin_id"
      touch "$config_root/$plugin_id/installed"
      if [[ "$plugin_id" != "herdr.collie" ]]; then
        mkdir -p "$github_root/$plugin_id"
        touch "$github_root/$plugin_id/herdr-plugin.toml"
      fi
      printf '%s\n' "$plugin_id" >> "$HERDR_PLUGIN_LOG"
      ;;
    config-dir) echo "$collie_dir" ;;
    *) exit 0 ;;
  esac
  exit 0
fi
exit 0
''')
        self._write_command("herdr", "exit 126\n")
        self._write_command("omp-fixture", r'''
if [[ "${1:-}" == "--version" ]]; then echo "omp/17.3.4"; exit 0; fi
if [[ "${1:-}" == "plugin" && "${2:-}" == "install" ]]; then
  plugin_dir="$HOME/.omp/plugins/node_modules/@burneikis/pi-sticky"
  mkdir -p "$plugin_dir"
  printf '%s\n' 'export default function piSticky() {}' > "$plugin_dir/index.ts"
  exit 0
fi
if [[ "${1:-}" == "auth-broker" && "${2:-}" == "token" ]]; then
  mkdir -p "$HOME/.omp"
  printf '%s\n' 'b-token' > "$HOME/.omp/auth-broker.token"
  chmod 600 "$HOME/.omp/auth-broker.token"
  exit 0
fi
if [[ "${1:-}" == "auth-gateway" && "${2:-}" == "token" ]]; then
  mkdir -p "$HOME/.omp"
  printf '%s\n' 'g-token' > "$HOME/.omp/auth-gateway.token"
  chmod 600 "$HOME/.omp/auth-gateway.token"
  exit 0
fi
if [[ "${1:-}" == "auth-gateway" ]]; then echo '{"ok":true}'; exit 0; fi
exit 0
''')
        self._write_command("fence-fixture", r'''
if [[ "${1:-}" == "--version" ]]; then echo "Fence Version: 0.1.66"; exit 0; fi
if [[ "${1:-}" == "--linux-features" ]]; then printf '%s\n' '  Bubblewrap                 core sandbox                         ok' '  Landlock                   extra filesystem enforcement          ok' '  Network namespace          direct network isolation             ok' '  eBPF monitor               enhanced monitor mode                 unavailable'; exit 0; fi
exit 0
''')
        self._write_command("omp", "exit 126\n")
        self._write_command("fence", "exit 126\n")
        self._write_command("tailscale", r'''
if [[ "${1:-}" == "funnel" ]]; then echo '{}'; exit 0; fi
if [[ "${1:-}" == "serve" && "${2:-}" == "status" ]]; then
  echo "{\"TCP\":{\"443\":{\"HTTPS\":true}},\"Web\":{\"pisec.example.ts.net:443\":{\"Handlers\":{\"/\":{\"Proxy\":\"http://127.0.0.1:${PISEC_COLLIE_PORT:-8787}\"}}}}}"
  exit 0
fi
if [[ "${1:-}" == "version" ]]; then echo 'tailscale 1.0'; exit 0; fi
exit 0
''')
        self._write_command("bun", "echo '1.0.0'\n")
        self._write_command("bwrap", "echo 'bubblewrap 0.1'\n")
        self._write_command("socat", "echo 'socat 1.0'\n")
        self._write_command("loginctl", "exit 0\n")
        self._write_command("systemctl", "exit 0\n")

    def env(self):
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.home),
                "USER": "tester",
                "PISEC_AUTH_BROKER_PORT": str(self.ports["PISEC_AUTH_BROKER_PORT"]),
                "PISEC_AUTH_GATEWAY_PORT": str(self.ports["PISEC_AUTH_GATEWAY_PORT"]),
                "PISEC_COLLIE_PORT": str(self.ports["PISEC_COLLIE_PORT"]),
                "PISEC_COLLIE_PROBE_URL": f"http://127.0.0.1:{self.ports['PISEC_COLLIE_PORT']}",
                "DOTFILES_DIR": str(ROOT),
                "XDG_STATE_HOME": str(self.root / "state"),
                "XDG_RUNTIME_DIR": str(self.runtime_root.parent),
                "PISEC_RUNTIME_ROOT": str(self.runtime_root),
                "PISEC_BWRAP_DIR": str(self.root / "pisec-bwrap"),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "PATH": str(self.fake_bin) + ":/usr/bin:/bin",
                "HERDR_PATH": str(self.fake_bin / "herdr-fixture"),
                "REAL_OMP_PATH": str(self.fake_bin / "omp-fixture"),
                "FENCE_REAL_PATH": str(self.fake_bin / "fence-fixture"),
                "FAKE_TREEHOUSE_ARCHIVE": str(self.treehouse_archive),
                "TREEHOUSE_CHECKSUM_LOG": str(self.root / "treehouse-checksum.log"),
                "HERDR_PLUGIN_LOG": str(self.root / "herdr-plugin.log"),
            }
        )
        return environment

    def test_existing_herdr_workspace_is_rehomed_to_main(self):
        config_path = self.home / ".config" / "pisec" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = json.loads((ROOT / "pisec" / "config.example.json").read_text())
        config["workspace"]["config"] = {
            "sessionName": "pisec",
            "socketPath": str(self.home / ".config" / "herdr" / "sessions" / "pisec" / "herdr.sock"),
        }
        config_path.write_text(json.dumps(config))
        real_omp = self.root / "real-omp"
        fence = self.root / "fence"
        real_omp.write_text("omp")
        fence.write_text("fence")
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            patch_pisec_config(config_path, real_omp_path=str(real_omp), fence_path=str(fence))
        patched = json.loads(config_path.read_text())
        self.assertEqual(patched["workspace"]["config"]["sessionName"], "main")
        self.assertEqual(
            patched["workspace"]["config"]["socketPath"],
            str(self.home / ".config" / "herdr" / "sessions" / "main" / "herdr.sock"),
        )

    def test_herdr_config_patch_preserves_user_keys_and_is_idempotent(self):
        path = self.home / ".config" / "herdr" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '[keys]\nprefix = "ctrl+b"\n\n'
            '[[keys.command]]\nkey = "prefix+y"\ncommand = "custom:action"\n'
        )

        patch_herdr_config(path)
        first = path.read_text()
        patch_herdr_config(path)

        self.assertEqual(path.read_text(), first)
        config = tomllib.loads(first)
        self.assertEqual(config["keys"]["prefix"], "ctrl+a")
        self.assertEqual(
            config["keys"]["command"],
            [
                {"key": "prefix+y", "command": "custom:action"},
                {"key": "prefix+shift+e", "command": "plugin:chmarax.herdr-nvim:toggle"},
                {"key": "prefix+shift+f", "command": "plugin:chmarax.herdr-nvim:pick-file"},
                {"key": "prefix+shift+v", "command": "plugin:persiyanov.reviewr:toggle"},
            ],
        )

    def test_full_install_links_stable_services_and_waits_for_health(self):
        old_dir = self.home / ".omp" / "agent" / "extensions"
        old_dir.mkdir(parents=True)
        old_link = old_dir / "pisec.ts"
        old_link.symlink_to(ROOT / "omp" / "extensions" / "pisec.ts")
        result = subprocess.run(
            [str(INSTALLER), "--collie-host", "pisec.example.ts.net", "--collie-trusted-user", "tester@example.invalid", "--reset-pisec-state"],
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        archives = list((self.root / "state").glob("pisec.archive-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].is_dir())
        self.assertFalse((self.root / "state" / "pisec").exists())
        stable = self.home / ".local" / "lib" / "pisec" / "bin"
        for name in ("real-omp", "fence", "omp", "omp-admin", "pisec-broker", "pisec-auth-broker", "pisec-auth-gateway", "herdr"):
            path = stable / name
            self.assertTrue(path.is_file() and not path.is_symlink())
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
        treehouse = self.home / ".local" / "bin" / "treehouse"
        self.assertTrue(treehouse.is_file() and not treehouse.is_symlink())
        self.assertEqual(subprocess.run(["bash", str(treehouse), "--version"], text=True, capture_output=True, check=True).stdout.strip(), "v2.1.1")
        self.assertIn("2fe3e01220ae51a967c3e5ba6ccf10ec83bdbae8e420368d194285a8d04c9ef8", (self.root / "treehouse-checksum.log").read_text())
        blocked = subprocess.run(["bash", str(stable / "omp")], env={**self.env(), "PISEC_WORKSTREAM_ID": "ws_test"}, text=True, capture_output=True)
        self.assertEqual(blocked.returncode, 126)
        self.assertIn("pisec project open", blocked.stderr)
        admin = subprocess.run(["bash", str(stable / "omp-admin"), "--version"], env={**self.env(), "PISEC_WORKSTREAM_ID": "ws_test"}, text=True, capture_output=True)
        self.assertEqual(admin.returncode, 0, admin.stderr)
        self.assertIn("omp/17.3.4", admin.stdout)
        herdr_cli = subprocess.run(["bash", str(stable / "herdr"), "--version"], env=self.env(), text=True, capture_output=True)
        self.assertEqual(herdr_cli.returncode, 0, herdr_cli.stderr)
        self.assertIn("herdr 0.8.0", herdr_cli.stdout)
        self.assertEqual((self.home / ".omp" / "agent" / "skills").resolve(), (ROOT / "skills").resolve())
        self.assertEqual((self.home / ".codex" / "skills").resolve(), (ROOT / "skills").resolve())
        self.assertEqual((self.home / ".config" / "opencode" / "skills").resolve(), (ROOT / "skills").resolve())
        self.assertEqual((self.home / ".config" / "nvim").resolve(), (ROOT / "nvim").resolve())
        sticky = self.home / ".omp" / "plugins" / "node_modules" / "@burneikis" / "pi-sticky" / "index.ts"
        self.assertTrue(sticky.is_file() and not sticky.is_symlink())
        self.assertFalse(old_link.exists())
        config = json.loads((self.home / ".config" / "pisec" / "config.json").read_text())
        self.assertEqual(config["schemaVersion"], 3)
        self.assertEqual(config["harness"]["id"], "omp")
        self.assertEqual(config["harness"]["config"]["executablePath"], str(stable / "real-omp"))
        self.assertEqual(config["fencePath"], str(stable / "fence"))
        herdr_config_path = self.home / ".config" / "herdr" / "config.toml"
        herdr_config = herdr_config_path.read_text()
        parsed_herdr_config = tomllib.loads(herdr_config)
        self.assertEqual(parsed_herdr_config["keys"]["prefix"], "ctrl+a")
        self.assertEqual(
            parsed_herdr_config["keys"]["command"],
            [
                {"key": "prefix+shift+e", "command": "plugin:chmarax.herdr-nvim:toggle"},
                {"key": "prefix+shift+f", "command": "plugin:chmarax.herdr-nvim:pick-file"},
                {"key": "prefix+shift+v", "command": "plugin:persiyanov.reviewr:toggle"},
            ],
        )
        self.assertFalse(parsed_herdr_config["session"]["resume_agents_on_restore"])
        self.assertFalse(parsed_herdr_config["experimental"]["pane_history"])
        self.assertEqual(herdr_config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (self.root / "herdr-plugin.log").read_text().splitlines(),
            ["chmarax.herdr-nvim", "persiyanov.reviewr", "herdr.collie"],
        )
        for name, target in {
            "pisec-auth-broker.service": "pisec-auth-broker",
            "pisec-auth-gateway.service": "pisec-auth-gateway",
            "pisec-broker.service": "pisec-broker",
            "herdr.service": "herdr",
        }.items():
            unit = (self.home / ".config" / "systemd" / "user" / name).read_text()
            self.assertIn(f"ExecStart=%h/.local/lib/pisec/bin/{target}", unit)
            if name == "herdr.service":
                self.assertIn("Environment=HERDR_SESSION=main", unit)
                self.assertIn("Environment=HERDR_CONFIG_PATH=%h/.config/herdr/config.toml", unit)
        broker_unit = (self.home / ".config" / "systemd" / "user" / "pisec-broker.service").read_text()
        self.assertIn("Requires=herdr.service pisec-auth-gateway.service", broker_unit)
        self.assertIn("PartOf=herdr.service", broker_unit)
        for retired in ("herdr-pisec.service", "herdr-pi-personal.service"):
            self.assertFalse((self.home / ".config" / "systemd" / "user" / retired).exists())
        self.assertFalse((self.home / ".local" / "lib" / "pisec" / "personal-bin").exists())
        collie_env = self.collie_dir / ".env"
        self.assertEqual(collie_env.stat().st_mode & 0o777, 0o600)
        values = dict(line.split("=", 1) for line in collie_env.read_text().splitlines() if "=" in line)
        self.assertEqual(values["COLLIE_HOST"], "127.0.0.1")
        self.assertEqual(values["COLLIE_PUBLIC_HOSTS"], "pisec.example.ts.net")
        self.assertEqual(values["COLLIE_MULTI_SESSION"], "off")
        self.assertEqual(values["COLLIE_TRANSCRIPT_ROOT"], "%h/.config/herdr/sessions/main")
        activity_source = (self.collie_source_dir / "bridge" / "activity.ts").read_text()
        server_source = (self.collie_source_dir / "bridge" / "server.ts").read_text()
        self.assertIn("export function presentActivity", activity_source)
        self.assertIn('status = activity.activeAt > activity.seenAt ? "done" : "idle"', activity_source)
        self.assertIn("presentActivity(p, activity.get(rt.name, p.paneId))", server_source)
        patch = ROOT / "patches" / "collie-v0.28-unread-idle.patch"
        applied = subprocess.run(
            ["git", "-C", str(self.collie_source_dir), "apply", "--reverse", "--check", str(patch)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((self.home / ".omp" / "auth-gateway.token").stat().st_mode & 0o777, 0o600)

    def test_capability_failure_precedes_home_mutation(self):
        fence = self.fake_bin / "fence-fixture"
        fence.write_text("#!/usr/bin/env bash\nif [[ \"${1:-}\" == \"--version\" ]]; then echo 'Fence Version: 0.1.66'; else echo 'Bubblewrap: unavailable'; fi\n")
        os.chmod(fence, 0o700)
        result = subprocess.run(
            [str(INSTALLER), "--collie-host", "pisec.example.ts.net", "--collie-trusted-user", "tester"],
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Fence user namespaces/Landlock are unavailable", result.stderr)
        self.assertFalse((self.home / ".config" / "opencode").exists())
        self.assertFalse((self.home / ".local" / "lib" / "pisec").exists())

    def test_bwrap_runtime_failure_reports_persistent_apparmor_fix(self):
        self._write_command("bwrap", "exit 1\n")
        restrict_path = self.root / "apparmor-restrict-userns"
        restrict_path.write_text("1\n")
        profile_path = self.root / "bwrap-userns-restrict"
        profile_path.write_text("profile bwrap {}\n")
        environment = self.env()
        environment["PISEC_APPARMOR_RESTRICT_PATH"] = str(restrict_path)
        environment["PISEC_BWRAP_APPARMOR_PROFILE"] = str(profile_path)
        result = subprocess.run(
            [str(INSTALLER), "--collie-host", "pisec.example.ts.net", "--collie-trusted-user", "tester"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Ubuntu's AppArmor user-namespace restriction", result.stderr)
        self.assertIn("pisec-linux-prereqs-install.sh", result.stderr)
        self.assertFalse((self.home / ".config" / "opencode").exists())
        self.assertFalse((self.home / ".local" / "lib" / "pisec").exists())

    def test_checksum_failure_precedes_home_mutation(self):
        environment = self.env()
        environment["TREEHOUSE_CHECKSUM_FAIL"] = "1"
        result = subprocess.run(
            [str(INSTALLER), "--collie-host", "pisec.example.ts.net", "--collie-trusted-user", "tester"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pinned Treehouse checksum failed", result.stderr)
        self.assertIn("2fe3e01220ae51a967c3e5ba6ccf10ec83bdbae8e420368d194285a8d04c9ef8", (self.root / "treehouse-checksum.log").read_text())
        for path in (
            self.home / ".config" / "opencode",
            self.home / ".codex",
            self.home / ".skills",
            self.home / ".omp" / "agent" / "AGENTS.md",
            self.home / ".omp" / "agent" / "skills",
            self.home / ".local" / "lib" / "pisec",
        ):
            self.assertFalse(path.exists() or path.is_symlink(), path)
    def test_epoch_one_state_requires_explicit_reset(self):
        state = self.root / "state" / "pisec"
        database = state / "control.db"
        database.unlink()
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TABLE control_meta (singleton INTEGER PRIMARY KEY, schema_name TEXT, schema_version INTEGER, schema_sha256 TEXT, migration_name TEXT)"
            )
            connection.execute(
                "INSERT INTO control_meta VALUES (1, 'pisec-core', 1, 'old', 'pisec-core-epoch-1')"
            )
            connection.commit()
        os.chmod(database, 0o600)
        result = subprocess.run(
            [str(INSTALLER), "--collie-host", "pisec.example.ts.net", "--collie-trusted-user", "tester"],
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing Pisec state is unsafe or has an unsupported schema", result.stderr)
        self.assertTrue(state.is_dir())
        self.assertFalse(list(self.root.glob("state/pisec.archive-*")))

    def test_late_failure_rolls_back_file_mutations(self):
        self._write_command("systemctl", r'''
if [[ "${1:-}" == "--user" && "${2:-}" == "daemon-reload" ]]; then
  exit 1
fi
exit 0
''')
        broker_token = self.home / ".omp" / "auth-broker.token"
        broker_token_before = broker_token.read_bytes()
        result = subprocess.run(
            [str(INSTALLER), "--collie-host", "pisec.example.ts.net", "--collie-trusted-user", "tester", "--reset-pisec-state"],
            cwd=ROOT,
            env=self.env(),
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        for path in (
            self.home / ".config" / "opencode" / "opencode.jsonc",
            self.home / ".config" / "opencode" / "skills",
            self.home / ".codex" / "skills",
            self.home / ".skills",
            self.home / ".omp" / "agent" / "extensions" / "pisec.ts",
            self.home / ".codex" / "AGENTS.md",
            self.home / ".omp" / "agent" / "AGENTS.md",
            self.home / ".omp" / "agent" / "skills",
            self.home / ".local" / "bin" / "pisec",
            self.home / ".config" / "herdr" / "config.toml",
            self.home / ".config" / "pisec" / "ports.env",
        ):
            self.assertFalse(path.exists() or path.is_symlink(), path)
        self.assertEqual(broker_token.read_bytes(), broker_token_before)
        archives = list((self.root / "state").glob("pisec.archive-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].is_dir())
        self.assertTrue((self.root / "state" / "pisec").is_dir())
        stable = self.home / ".local" / "lib" / "pisec" / "bin"
        self.assertFalse(any(stable.iterdir()) if stable.exists() else False)
        unit_dir = self.home / ".config" / "systemd" / "user"
        self.assertFalse(any(unit_dir.iterdir()) if unit_dir.exists() else False)


    def test_doctor_passes_against_fake_live_stack(self):
        state = self.root / "state" / "pisec"
        gateway_token = self.home / ".omp" / "auth-gateway.token"
        gateway_token.write_text("g" * 48 + "\n")
        os.chmod(gateway_token, 0o600)

        class FakeHarness:
            manifest = HarnessManifest("fixture-harness", "fixture-agent", "fixture-1")

            def health_checks(self, _binding, _workstream):
                return (AdapterHealth("fixture harness", True, "fixture"),)

        class FakeWorkspace:
            manifest = WorkspaceManifest("fixture-workspace", "fixture-session", "fixture-1", None)

            def health_checks(self):
                return (AdapterHealth("fixture workspace", True, "fixture"),)

        config = {
            "schemaVersion": 3,
            "fencePath": str(self.fake_bin / "fence"),
            "harness": {
                "id": "fixture-harness",
                "config": {
                    "executablePath": str(self.fake_bin / "omp"),
                    "gateway": {"baseUrl": "http://127.0.0.1:4000", "tokenFile": str(gateway_token)},
                },
            },
            "workspace": {
                "id": "fixture-workspace",
                "config": {"sessionName": "fixture-session", "socketPath": str(self.herdr_socket)},
            },
        }
        registry = AdapterRegistry()
        registry.register_harness(FakeHarness())
        registry.register_workspace(FakeWorkspace())
        with patch.dict(os.environ, self.env(), clear=False), PiStore(state) as store:
            result = doctor.run_doctor(store=store, config=config, registry=registry)
        self.assertTrue(result["ok"], [check for check in result["checks"] if check["status"] != "ok"])


if __name__ == "__main__":
    unittest.main()
