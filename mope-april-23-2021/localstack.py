#!/usr/bin/env python3
"""
localstack.py -- start, stop and inspect the local mope.io stack (Phase 3, task 14).

Services (from config/local_stack.json):
  master                 master server            http://127.0.0.1:8090 (internal)
  region-<id>            one per region (9)       ws://local.mope.io:7001..7009
  web                    web gateway              http://local.mope.io:8080  <- open this
  (account               Phase 4)

Every service is its own process, so each can be started and stopped independently.
Logs: logs/services/<service>.log (+ .out for raw stdout/stderr). PIDs: run/<service>.pid.

Usage:
  python localstack.py up                      # start everything, Ctrl-C stops everything
  python localstack.py start [targets...]      # targets: all (default), master, web, regions, <region-id>
  python localstack.py stop  [targets...]
  python localstack.py restart [targets...]
  python localstack.py status

Before opening the client, map the public host to your machine once:
  127.0.0.1 local.mope.io      (hosts file: /etc/hosts or C:\\Windows\\System32\\drivers\\etc\\hosts)
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from services import common  # noqa: E402


class Service:
    def __init__(self, name: str, script: str, port: int, extra: list[str], health: str | None):
        self.name = name
        self.script = script
        self.port = port
        self.extra = extra
        self.health = health


def services(config: dict) -> list[Service]:
    out = [Service("master", "master_server.py", int(config["master"]["port"]), [],
                   f"{common.master_url(config)}/master/health")]
    for region in config["regions"]:
        if region.get("enabled", True):
            out.append(Service(f"region-{region['id']}", "region_server.py", int(region["port"]),
                               ["--region", region["id"]], None))
    out.append(Service("web", "web_gateway.py", int(config["web"]["port"]), [],
                       f"http://127.0.0.1:{int(config['web']['port'])}/__local/health"))
    return out


def select(config: dict, targets: list[str]) -> list[Service]:
    every = services(config)
    if not targets or "all" in targets:
        return every
    chosen = []
    for target in targets:
        if target == "regions":
            matches = [s for s in every if s.name.startswith("region-")]
        else:
            matches = [s for s in every if s.name in (target, f"region-{target}")]
        if not matches:
            names = ", ".join(s.name for s in every)
            raise SystemExit(f"error: unknown target {target!r} (known: all, regions, {names})")
        chosen += [m for m in matches if m not in chosen]
    order = {s.name: i for i, s in enumerate(every)}
    return sorted(chosen, key=lambda s: order[s.name])


# -- process helpers -------------------------------------------------------------------

def pid_file(config: dict, svc: Service) -> Path:
    return common.run_dir(config) / f"{svc.name}.pid"


def running_pid(config: dict, svc: Service) -> int | None:
    path = pid_file(config, svc)
    try:
        pid = int(path.read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.exists() and svc.script.encode() not in cmdline.read_bytes():
        path.unlink(missing_ok=True)  # PID reused by something else
        return None
    return pid


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0


def ready(config: dict, svc: Service) -> bool:
    if svc.health:
        try:
            status, _ = common.http_json("GET", svc.health, timeout=0.5)
            return status == 200
        except OSError:
            return False
    return port_in_use(config["bind_host"], svc.port)


def tail(path: Path, lines: int = 12) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no log)"


def start(config: dict, svc: Service, config_path: str, token: str) -> bool:
    pid = running_pid(config, svc)
    if pid:
        print(f"  {svc.name:<24} already running (pid {pid})")
        return True
    if port_in_use(config["bind_host"], svc.port):
        print(f"  {svc.name:<24} FAILED: port {svc.port} is already in use by another program")
        return False
    logs = common.project_path(config, config.get("logs_dir", "logs")) / "services"
    logs.mkdir(parents=True, exist_ok=True)
    out = open(logs / f"{svc.name}.out", "ab")
    cmd = [sys.executable, str(PROJECT / "services" / svc.script), "--config", config_path, *svc.extra]
    env = dict(os.environ, **{common.TOKEN_ENV: token, "PYTHONUNBUFFERED": "1"})
    proc = subprocess.Popen(cmd, cwd=PROJECT, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            env=env, start_new_session=True)
    pid_file(config, svc).write_text(str(proc.pid))
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"  {svc.name:<24} FAILED (exit {proc.returncode}); last log lines:\n{tail(logs / f'{svc.name}.out')}")
            pid_file(config, svc).unlink(missing_ok=True)
            return False
        if ready(config, svc):
            print(f"  {svc.name:<24} started (pid {proc.pid}, port {svc.port})")
            return True
        time.sleep(0.1)
    print(f"  {svc.name:<24} started but not ready after 15s (pid {proc.pid}); see logs/services/{svc.name}.log")
    return False


def stop(config: dict, svc: Service, timeout: float = 8.0) -> bool:
    pid = running_pid(config, svc)
    if not pid:
        print(f"  {svc.name:<24} not running")
        return True
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        print(f"  {svc.name:<24} did not stop in {timeout:.0f}s; killed")
    pid_file(config, svc).unlink(missing_ok=True)
    print(f"  {svc.name:<24} stopped")
    return True


def status(config: dict) -> int:
    regions = {}
    master_up = False
    try:
        code, body = common.http_json("GET", f"{common.master_url(config)}/master/regions", timeout=1.0)
        if code == 200:
            master_up = True
            regions = {r["id"]: r for r in body["regions"]}
    except OSError:
        pass
    print(f"{'service':<26}{'pid':>8}  {'port':>5}  {'process':<8}  {'master view':<12}{'players':>8}")
    for svc in services(config):
        pid = running_pid(config, svc)
        view, players = "", ""
        if svc.name.startswith("region-"):
            entry = regions.get(svc.name[len("region-"):])
            view = entry["status"] if entry else ("-" if master_up else "master down")
            players = str(entry["players"]) if entry else ""
        print(f"{svc.name:<26}{(pid or '-'):>8}  {svc.port:>5}  {'running' if pid else 'stopped':<8}  "
              f"{view:<12}{players:>8}")
    print(f"\nopen: {common.web_origin(config)}/   (hosts file: 127.0.0.1 {config['public_host']})")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage the local mope.io stack.")
    parser.add_argument("command", choices=["up", "start", "stop", "restart", "status"])
    parser.add_argument("targets", nargs="*", help="all (default), master, web, regions, or a region id")
    parser.add_argument("--config", default=str(common.DEFAULT_CONFIG))
    args = parser.parse_args(argv)
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # quiet exit when piped into head/less
    config_path = str(Path(args.config).resolve())
    config = common.load_stack_config(config_path)
    token = common.stack_token(config, create=True)

    if args.command == "status":
        return status(config)
    chosen = select(config, args.targets)
    if args.command in ("stop", "restart"):
        for svc in reversed(chosen):
            stop(config, svc)
    if args.command in ("start", "restart", "up"):
        ok = all([start(config, svc, config_path, token) for svc in chosen])
        print(f"\nopen: {common.web_origin(config)}/   (hosts file: 127.0.0.1 {config['public_host']})")
        if args.command != "up":
            return 0 if ok else 1
        print("stack is up; press Ctrl-C to stop everything")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print()
        for svc in reversed(chosen):
            stop(config, svc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
