#!/usr/bin/env python3
"""Build and manage this checkout's four local analytics services on macOS."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
from urllib.request import urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
AGENTS = Path.home() / "Library/LaunchAgents"
LOGS = Path.home() / "Library/Logs/fomo-radar-local"
PREFIX = "com.masonearl.fomo-radar-local"
SERVICES = ("api", "site", "collector", "watcher")


def environment(node: str | None = None) -> dict[str, str]:
    # Read only this checkout's configuration, including explicit empty values. Never copy
    # application keys into launchd plists; the service reads .env when it starts.
    env = dict(os.environ)
    env.update({k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None})
    env["PYTHONUNBUFFERED"] = "1"
    env["ASTRO_TELEMETRY_DISABLED"] = "1"
    env["API_HOST"] = "127.0.0.1"
    env["HOST"] = "127.0.0.1"
    env["PORT"] = "4322"
    env["API_BASE"] = "http://127.0.0.1:8767"
    env["API_PORT"] = "8767"
    env["PUBLIC_SITE_URL"] = "http://127.0.0.1:4322"
    env["PUBLIC_LOCAL_MODE"] = "true"
    env["PUBLIC_TOKEN_CA"] = ""
    if node:
        env["LOCAL_RADAR_NODE"] = node
    return env


def node_binary(value: str | None) -> str:
    found = value or os.environ.get("LOCAL_RADAR_NODE") or shutil.which("node")
    if not found:
        raise SystemExit("Node.js is missing; pass --node /absolute/path/to/node.")
    resolved = str(Path(found).resolve())
    version = subprocess.check_output([resolved, "--version"], text=True).strip()
    major, minor, *_ = map(int, version.removeprefix("v").split("."))
    if (major, minor) < (22, 19):
        raise SystemExit(f"Node {version} is too old for the lockfile; use Node >=22.19.")
    return resolved


def service(name: str, node: str | None) -> None:
    env = environment(node)
    if name == "site":
        cmd = [node_binary(node), str(ROOT / "site/dist/server/entry.mjs")]
        os.chdir(ROOT / "site")
    else:
        command = {"api": "serve", "collector": "run", "watcher": "watch"}[name]
        cmd = [str(PYTHON), "-m", "fomo_agent.cli", command]
        os.chdir(ROOT)
    os.execve(cmd[0], cmd, env)


def target(name: str) -> str:
    return f"gui/{os.getuid()}/{PREFIX}.{name}"


def plist_path(name: str) -> Path:
    return AGENTS / f"{PREFIX}.{name}.plist"


def loaded(name: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", "print", target(name)], capture_output=True, text=True)


def install(node: str) -> None:
    if not (ROOT / "site/dist/server/entry.mjs").exists():
        raise SystemExit("Build the site before installing services.")
    AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        path = plist_path(name)
        args = [str(PYTHON), str(Path(__file__).resolve()), "service", name, "--node", node]
        contents = {
            "Label": f"{PREFIX}.{name}", "ProgramArguments": args,
            "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 30, "ProcessType": "Background", "Umask": 0o077,
            "StandardOutPath": str(LOGS / f"{name}.log"),
            "StandardErrorPath": str(LOGS / f"{name}.error.log"),
        }
        path.write_bytes(plistlib.dumps(contents))
        path.chmod(0o600)
        print(f"Installed {path}")


def start() -> None:
    # Complete schema migrations once before four processes open the database together.
    subprocess.run([str(PYTHON), "-m", "fomo_agent.cli", "init"],
                   cwd=ROOT, env=environment(), check=True, stdout=subprocess.DEVNULL)
    for name in SERVICES:
        if not plist_path(name).exists():
            raise SystemExit(f"Missing {plist_path(name)}; run install first.")
        if loaded(name).returncode == 0:
            print(f"{name}: already loaded")
            continue
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path(name))], check=True)
        print(f"{name}: started")


def stop() -> None:
    for name in reversed(SERVICES):
        if loaded(name).returncode == 0:
            subprocess.run(["launchctl", "bootout", target(name)], check=True)
        print(f"{name}: stopped")


def status() -> None:
    for name in SERVICES:
        result = loaded(name)
        details = [line.strip() for line in result.stdout.splitlines()
                   if line.strip().startswith(("state =", "pid =", "last exit code ="))]
        print(f"{name}: {', '.join(details) if result.returncode == 0 else 'not loaded'}")
    for endpoint in ("health", "stats", "system"):
        try:
            with urlopen(f"http://127.0.0.1:8767/api/{endpoint}", timeout=5) as response:
                print(f"{endpoint}: {json.dumps(json.load(response))}")
        except Exception as exc:
            print(f"{endpoint}: unavailable ({exc})")
    print(f"Dashboard: http://127.0.0.1:4322\nLogs: {LOGS}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "install", "start", "stop", "restart", "status", "uninstall", "service"))
    parser.add_argument("name", nargs="?", choices=SERVICES)
    parser.add_argument("--node", help="Path to Node.js >=22.19 (used for build/install/site)")
    args = parser.parse_args()
    if args.action == "service":
        if not args.name:
            parser.error("service requires a service name")
        service(args.name, args.node)
    elif args.action == "build":
        node = node_binary(args.node)
        subprocess.run([node, "node_modules/astro/astro.js", "build"], cwd=ROOT / "site", env=environment(node), check=True)
    elif args.action == "install":
        install(node_binary(args.node))
    elif args.action == "start":
        start()
    elif args.action == "stop":
        stop()
    elif args.action == "restart":
        stop()
        start()
    elif args.action == "uninstall":
        stop()
        for name in SERVICES:
            plist_path(name).unlink(missing_ok=True)
    else:
        status()


if __name__ == "__main__":
    main()
