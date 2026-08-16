from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import socket
import socketserver
import threading
import unittest
from unittest.mock import patch

from scripts.pisec import doctor
from scripts.pisec.host_config import write_collie_env
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
        response = {
            "protocolVersion": 1,
            "requestId": request["requestId"],
            "ok": True,
            "result": {"ok": True} if request["operation"] == "system.doctor" else {},
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
        self._write_commands()
        self.runtime_root = self.root / "runtime"
        self._sockets = []
        self._threads = []
        self._start_unix(self.runtime_root / "admin" / "control.sock", FakeAdminHandler)
        self._start_unix(self.runtime_root / "secretary" / "control.sock")
        self._start_unix(self.runtime_root / "runtime" / "control.sock")
        self.herdr_socket = self.home / ".config" / "herdr" / "sessions" / "pisec" / "herdr.sock"
        self._start_unix(self.herdr_socket)
        self.personal_herdr_socket = self.home / ".config" / "herdr" / "sessions" / "pi-personal" / "herdr.sock"
        self._start_unix(self.personal_herdr_socket)
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

    def _write_commands(self):
        self._write_command("herdr", r'''
if [[ "${1:-}" == "--version" ]]; then echo "herdr 0.8.0"; exit 0; fi
if [[ "${1:-}" == "plugin" ]]; then
  collie_dir="$HOME/.config/herdr/plugins/config/herdr.collie"
  case "${2:-}" in
    list)
      if [[ -f "$collie_dir/installed" ]]; then
        echo '{"result":{"plugins":[{"plugin_id":"herdr.collie","version":"0.28.0"}]}}'
      else
        echo '{"result":{"plugins":[]}}'
      fi
      ;;
    install)
      mkdir -p "$collie_dir"
      touch "$collie_dir/installed"
      ;;
    config-dir) echo "$collie_dir" ;;
    *) exit 0 ;;
  esac
  exit 0
fi
exit 0
''')
        self._write_command("omp", r'''
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
        self._write_command("fence", r'''
if [[ "${1:-}" == "--version" ]]; then echo "Fence Version: 0.1.66"; exit 0; fi
if [[ "${1:-}" == "--linux-features" ]]; then printf '%s\n' '  Bubblewrap                 core sandbox                         ok' '  Landlock                   extra filesystem enforcement          ok' '  Network namespace          direct network isolation             ok' '  eBPF monitor               enhanced monitor mode                 unavailable'; exit 0; fi
exit 0
''')
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
                "PATH": str(self.fake_bin) + ":/usr/bin:/bin",
            }
        )
        return environment

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
        for name in ("real-omp", "fence", "omp", "pisec-shell", "pisec-broker", "pisec-auth-broker", "pisec-auth-gateway", "herdr", "herdr-personal"):
            path = stable / name
            self.assertTrue(path.is_file() and not path.is_symlink())
            self.assertTrue(path.stat().st_mode & stat.S_IXUSR)
        personal_omp = self.home / ".local" / "lib" / "pisec" / "personal-bin" / "omp"
        self.assertTrue(personal_omp.is_file() and personal_omp.stat().st_mode & stat.S_IXUSR)
        sticky = self.home / ".omp" / "plugins" / "node_modules" / "@burneikis" / "pi-sticky" / "index.ts"
        self.assertTrue(sticky.is_file() and not sticky.is_symlink())
        self.assertFalse(old_link.exists())
        config = json.loads((self.home / ".config" / "pisec" / "config.json").read_text())
        self.assertEqual(config["schemaVersion"], 3)
        self.assertEqual(config["harness"]["id"], "omp")
        self.assertEqual(config["harness"]["config"]["executablePath"], str(stable / "real-omp"))
        self.assertEqual(config["fencePath"], str(stable / "fence"))
        herdr_config = (self.home / ".config" / "pisec" / "herdr.toml").read_text()
        self.assertIn(f'default_shell = "{stable / "pisec-shell"}"', herdr_config)
        self.assertIn('shell_mode = "non_login"', herdr_config)
        self.assertEqual((self.home / ".config" / "pisec" / "herdr.toml").stat().st_mode & 0o777, 0o600)
        for name, target in {
            "pisec-auth-broker.service": "pisec-auth-broker",
            "pisec-auth-gateway.service": "pisec-auth-gateway",
            "pisec-broker.service": "pisec-broker",
            "herdr-pisec.service": "herdr",
            "herdr-pi-personal.service": "herdr-personal",
        }.items():
            unit = (self.home / ".config" / "systemd" / "user" / name).read_text()
            self.assertIn(f"ExecStart=%h/.local/lib/pisec/bin/{target}", unit)
            if name == "herdr-pisec.service":
                self.assertIn("Environment=HERDR_CONFIG_PATH=%h/.config/pisec/herdr.toml", unit)
            if name == "herdr-pi-personal.service":
                self.assertIn("Environment=PATH=%h/.local/lib/pisec/personal-bin:", unit)
        collie_env = self.collie_dir / ".env"
        self.assertEqual(collie_env.stat().st_mode & 0o777, 0o600)
        values = dict(line.split("=", 1) for line in collie_env.read_text().splitlines() if "=" in line)
        self.assertEqual(values["COLLIE_HOST"], "127.0.0.1")
        self.assertEqual(values["COLLIE_PUBLIC_HOSTS"], "pisec.example.ts.net")
        self.assertEqual(values["COLLIE_TRANSCRIPT_ROOT"], "%h/.omp/agent/sessions,%h/.local/state/pisec/omp,%h/.local/state/pisec-personal/profiles")
        self.assertEqual((self.home / ".omp" / "auth-gateway.token").stat().st_mode & 0o777, 0o600)

    def test_capability_failure_precedes_home_mutation(self):
        fence = self.fake_bin / "fence"
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
        self.assertIn("existing Pisec state is unsafe or not epoch 3", result.stderr)
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
            self.home / ".local" / "bin" / "pisec",
            self.home / ".config" / "pisec" / "config.json",
            self.home / ".config" / "pisec" / "herdr.toml",
            self.home / ".config" / "herdr" / "config.toml",
            self.home / ".config" / "pisec" / "ports.env",
            self.home / ".omp" / "auth-gateway.token",
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
