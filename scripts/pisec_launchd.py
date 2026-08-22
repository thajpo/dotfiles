#!/usr/bin/env python3
"""Generate and manage macOS launchd services for the Pisec stack.

Usage:
  pisec_launchd.py render [--out DIR] [--home HOME] [--dotfiles DIR] [--herdr-bin PATH]
  pisec_launchd.py bootstrap [--home HOME] [--dotfiles DIR] [--herdr-bin PATH]
  pisec_launchd.py bootout [--home HOME]
  pisec_launchd.py status

`render` writes LaunchAgents plists; `bootstrap` renders then loads them via
launchctl (booting out any prior generation first). Service environment values
are sourced from ~/.config/pisec/ports.env when present. This command is a
no-op guard on non-Darwin hosts except for `render --out DIR`.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import platform
import subprocess
import sys

LABELS = ("com.dotfiles.pisec-auth-broker", "com.dotfiles.pisec-auth-gateway", "com.dotfiles.pisec-broker", "com.dotfiles.herdr")
DEFAULT_PORTS = {"PISEC_AUTH_BROKER_PORT": "8765", "PISEC_AUTH_GATEWAY_PORT": "4000", "PISEC_COLLIE_PORT": "8787"}
BREW_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin"


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"pisec_launchd: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_ports_env(home: pathlib.Path) -> dict[str, str]:
    values = dict(DEFAULT_PORTS)
    path = home / ".config" / "pisec" / "ports.env"
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key.isidentifier() and value:
            values[key] = value
    return values


def herdr_binary(explicit: str | None, home: pathlib.Path) -> str:
    if explicit:
        return explicit
    for candidate in ("/opt/homebrew/bin/herdr", "/usr/local/bin/herdr"):
        if pathlib.Path(candidate).exists():
            return candidate
    return str(home / ".local/lib/pisec/bin/herdr")


def base_environment(home: pathlib.Path, ports: dict[str, str], *, include_key: bool = False) -> dict[str, str]:
    del include_key
    return {
        "PATH": f"{home / '.local/lib/pisec/bin'}:{home / '.local/bin'}:{BREW_PATH}",
        "PISEC_AUTH_BROKER_PORT": ports["PISEC_AUTH_BROKER_PORT"],
        "PISEC_AUTH_GATEWAY_PORT": ports["PISEC_AUTH_GATEWAY_PORT"],
        "PISEC_COLLIE_PORT": ports["PISEC_COLLIE_PORT"],
        "LANG": "en_US.UTF-8",
    }


def service_definition(label: str, home: pathlib.Path, dotfiles: pathlib.Path, ports: dict[str, str], herdr_bin: str | None) -> dict:
    log_dir = home / ".local/state/pisec/log"
    common = {
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 2,
        "Umask": 0o077,
        "StandardOutPath": str(log_dir / f"{label}.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
    }
    if label == "com.dotfiles.pisec-auth-broker":
        arguments = ["/bin/bash", str(home / ".local/lib/pisec/bin/pisec-auth-broker")]
        environment = base_environment(home, ports, include_key=False)
    elif label == "com.dotfiles.pisec-auth-gateway":
        arguments = ["/bin/bash", str(home / ".local/lib/pisec/bin/pisec-auth-gateway")]
        environment = base_environment(home, ports, include_key=False)
    elif label == "com.dotfiles.pisec-broker":
        arguments = ["/bin/bash", str(dotfiles / "bin/pisec"), "broker"]
        environment = base_environment(home, ports)
    else:
        arguments = [herdr_binary(herdr_bin, home)]
        environment = base_environment(home, ports, include_key=False)
        environment["HERDR_SESSION"] = "main"
        environment["HERDR_CONFIG_PATH"] = str(home / ".config/herdr/config.toml")
    return {
        "Label": label,
        "ProgramArguments": arguments,
        "EnvironmentVariables": environment,
        "ProcessType": "Background",
        **common,
    }


def render(out_dir: pathlib.Path | None, home: pathlib.Path, dotfiles: pathlib.Path, herdr_bin: str | None) -> list[pathlib.Path]:
    ports = load_ports_env(home)
    out_dir = out_dir or home / "Library/LaunchAgents"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for label in LABELS:
        definition = service_definition(label, home, dotfiles, ports, herdr_bin)
        target = out_dir / f"{label}.plist"
        target.write_bytes(plistlib.dumps(definition, fmt=plistlib.FMT_XML))
        os.chmod(target, 0o644)
        written.append(target)
    return written


def gui_uid() -> str:
    return subprocess.run(["id", "-u"], text=True, capture_output=True, check=True).stdout.strip()


def launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["launchctl", *arguments], text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:256]
        fail(f"launchctl {' '.join(arguments)} failed: {detail}")
    return result


def bootstrap(home: pathlib.Path, dotfiles: pathlib.Path, herdr_bin: str | None) -> None:
    if platform.system() != "Darwin":
        fail("bootstrap requires Darwin; use render --out DIR to inspect plists elsewhere")
    uid = gui_uid()
    plists = render(None, home, dotfiles, herdr_bin)
    for plist_path in plists:
        print(f"rendered {plist_path}")
    for plist_path in plists:
        label = plist_path.stem
        launchctl("bootout", f"gui/{uid}/{label}", check=False)
        launchctl("bootstrap", f"gui/{uid}", str(plist_path))
        print(f"bootstrapped {label}")


def bootout(home: pathlib.Path) -> None:
    if platform.system() != "Darwin":
        fail("bootout requires Darwin")
    uid = gui_uid()
    for label in LABELS:
        launchctl("bootout", f"gui/{uid}/{label}", check=False)
        print(f"booted out {label}")


def status() -> None:
    uid = gui_uid()
    for label in LABELS:
        result = launchctl("print", f"gui/{uid}/{label}", check=False)
        state = "not loaded"
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("state ="):
                state = stripped.split("=", 1)[1].strip()
                break
        print(f"{label}: {state}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("render", "bootstrap", "bootout", "status"))
    parser.add_argument("--out", metavar="DIR", help="render target directory (default: ~/Library/LaunchAgents)")
    parser.add_argument("--home", metavar="DIR", help="override home directory (default: current $HOME)")
    parser.add_argument("--dotfiles", metavar="DIR", help="dotfiles checkout (default: derived from this script)")
    parser.add_argument("--herdr-bin", metavar="PATH", help="explicit herdr binary path")
    args = parser.parse_args(argv)
    home = pathlib.Path(args.home or os.environ.get("HOME", pathlib.Path.home())).expanduser()
    default_dotfiles = pathlib.Path(__file__).resolve().parents[1]
    dotfiles = pathlib.Path(args.dotfiles).expanduser() if args.dotfiles else default_dotfiles
    if args.command == "render":
        render(pathlib.Path(args.out).expanduser() if args.out else None, home, dotfiles, args.herdr_bin)
    elif args.command == "bootstrap":
        bootstrap(home, dotfiles, args.herdr_bin)
    elif args.command == "bootout":
        bootout(home)
    else:
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
