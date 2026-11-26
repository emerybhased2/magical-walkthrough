#!/usr/bin/env python3
"""
master_server.py -- Phase 3 (tasks 16-17, 24): local master server.

The archived 2025 client keeps its region list in $config.gameServers and asks
{ACCOUNT_SERVER}/servers/get for live player counts (phase2/README.md, task 11). This
service is the single source of truth for both:

  public (reached through the web gateway, same origin as the client)
    GET  /servers/get            -> [{"id", "totalPlayerCount"}] for ONLINE regions
                                    (exact shape the client parses; offline regions are
                                    omitted, which the client shows as "..." / -1)
    GET  /master/regions         -> full registry with status, players, url, timestamps
    GET  /master/client-config   -> the $config overlay the gateway injects into
                                    client.js: local gameServers + account server URL
    GET  /master/health

  internal (loopback only, X-Local-Stack-Token header)
    POST /master/register        {id, port, pid, version}
    POST /master/heartbeat       {id, port, pid, players, sessions}   (also upserts)
    POST /master/deregister      {id, reason}

A region is ONLINE while its heartbeats arrive within heartbeat_timeout_seconds, OFFLINE
after a deregister or a missed timeout, and UNKNOWN until it first registers. Regions
re-register automatically after a master restart because every heartbeat upserts.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common  # noqa: E402

VERSION = "1.0.0"


class Registry:
    def __init__(self, config: dict, log):
        self.config = config
        self.log = log
        self.timeout = float(config["master"].get("heartbeat_timeout_seconds", 6))
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.regions: dict[str, dict] = {}
        for region in config["regions"]:
            self.regions[region["id"]] = {
                "id": region["id"], "name": region["name"], "region": region["region"],
                "gm": int(region.get("gm", 0)), "port": int(region["port"]),
                "enabled": bool(region.get("enabled", True)),
                "url": common.region_public_url(config, region),
                "original_url": region.get("original_url"), "origin": region.get("origin", "unspecified"),
                "registered": False, "deregistered": False, "pid": None, "version": None,
                "players": 0, "sessions": 0, "registered_at": None, "last_heartbeat": None,
                "registrations": 0,
            }

    def status_of(self, entry: dict, now: float | None = None) -> str:
        now = now or time.time()
        if not entry["enabled"]:
            return "disabled"
        if not entry["registered"]:
            return "unknown"
        if entry["deregistered"]:
            return "offline"
        if entry["last_heartbeat"] is None or now - entry["last_heartbeat"] > self.timeout:
            return "offline"
        return "online"

    def upsert(self, payload: dict, heartbeat: bool) -> tuple[int, dict]:
        region_id = payload.get("id")
        with self.lock:
            entry = self.regions.get(region_id)
            if entry is None:
                return 404, {"error": f"unknown region id {region_id!r}"}
            if int(payload.get("port", entry["port"])) != entry["port"]:
                return 409, {"error": f"region {region_id} is configured on port {entry['port']}"}
            was = self.status_of(entry)
            now = time.time()
            if not entry["registered"] or entry["deregistered"] or not heartbeat or was == "offline":
                entry["registrations"] += 1
                entry["registered_at"] = now
            entry.update(registered=True, deregistered=False, last_heartbeat=now,
                         pid=payload.get("pid", entry["pid"]), version=payload.get("version", entry["version"]))
            if heartbeat:
                entry["players"] = max(0, int(payload.get("players", 0)))
                entry["sessions"] = max(0, int(payload.get("sessions", 0)))
            now_status = self.status_of(entry, now)
        if was != now_status:
            self.log.info("region %s: %s -> %s (pid %s)", region_id, was, now_status, entry["pid"])
        return 200, {"ok": True, "status": now_status, "heartbeat_interval_seconds":
                     float(self.config["master"].get("heartbeat_interval_seconds", 2))}

    def deregister(self, payload: dict) -> tuple[int, dict]:
        region_id = payload.get("id")
        with self.lock:
            entry = self.regions.get(region_id)
            if entry is None:
                return 404, {"error": f"unknown region id {region_id!r}"}
            entry.update(deregistered=True, players=0, sessions=0)
        self.log.info("region %s deregistered (%s)", region_id, payload.get("reason", "no reason given"))
        return 200, {"ok": True, "status": "offline"}

    def snapshot(self) -> list[dict]:
        now = time.time()
        with self.lock:
            out = []
            for entry in self.regions.values():
                item = {k: v for k, v in entry.items()}
                item["status"] = self.status_of(entry, now)
                item["seconds_since_heartbeat"] = round(now - entry["last_heartbeat"], 2) \
                    if entry["last_heartbeat"] else None
                if item["status"] != "online":
                    item["players"] = 0
                out.append(item)
            return out

    def client_stats(self) -> list[dict]:
        """GET /servers/get -- the client matches entries to $config.gameServers by id."""
        return [{"id": r["id"], "totalPlayerCount": r["players"]}
                for r in self.snapshot() if r["status"] == "online"]

    def client_config(self) -> dict:
        """$config overlay: local game servers + account server on the gateway origin."""
        servers = [{"id": r["id"], "name": r["name"], "url": r["url"], "region": r["region"], "gm": r["gm"]}
                   for r in self.snapshot() if r["enabled"]]
        settings = self.config.get("settings", {})
        env = settings.get("CLIENT_ENV", "production")
        return {"gameServers": servers, "useDevAccountServer": True,
                "devAccountServerUrl": common.web_origin(self.config),
                "env": env, "isProduction": env == "production",
                "isTestingMode": bool(settings.get("CLIENT_TESTING_MODE", False)),
                "isBetaMode": bool(settings.get("CLIENT_BETA_MODE", False))}


def make_handler(registry: Registry, token: str, log):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"mope-local-master/{VERSION}"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _json(self, status: int, payload) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _internal_allowed(self) -> bool:
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return False
            return self.headers.get("X-Local-Stack-Token", "") == token

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/servers/get":
                return self._json(200, registry.client_stats())
            if path == "/master/regions":
                return self._json(200, {"regions": registry.snapshot(), "heartbeat_timeout_seconds":
                                        registry.timeout, "master_started_at": registry.started_at})
            if path == "/master/client-config":
                return self._json(200, registry.client_config())
            if path == "/master/health":
                return self._json(200, {"ok": True, "service": "master", "version": VERSION,
                                        "uptime_seconds": round(time.time() - registry.started_at, 1)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/master/register", "/master/heartbeat", "/master/deregister"):
                return self._json(404, {"error": "not found"})
            if not self._internal_allowed():
                return self._json(403, {"error": "internal endpoint"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            if path == "/master/deregister":
                status, body = registry.deregister(payload)
            else:
                status, body = registry.upsert(payload, heartbeat=path.endswith("heartbeat"))
            return self._json(status, body)

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local mope.io master server")
    parser.add_argument("--config", help="stack config (default config/local_stack.json)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    config = common.load_stack_config(args.config)
    log = common.setup_logging(config, "master", args.verbose)
    token = common.stack_token(config)
    registry = Registry(config, log)
    bind, port = config["bind_host"], int(config["master"]["port"])
    server = ThreadingHTTPServer((bind, port), make_handler(registry, token, log))
    server.daemon_threads = True
    log.info("master %s listening on http://%s:%d (%d regions configured, heartbeat timeout %ss)",
             VERSION, bind, port, len(registry.regions), registry.timeout)

    def shutdown(signum, frame):
        log.info("master stopping (signal %s)", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    server.server_close()
    log.info("master stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
