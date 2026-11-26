#!/usr/bin/env python3
"""
web_gateway.py -- Phase 3 (tasks 18-19): the HTTP front door of the local stack.

Serves the archived client (the snapshot's working copy) on http://<public_host>:<port>/
-- by default http://local.mope.io:8080/, so the client's domain lock accepts it -- and is
also the client's "account server" origin, routing API paths to the right service:

  /servers/get, GET /master/{regions,client-config,health}  -> master server
  /auth/*, /shop/{get,purchases,buy,wardrobe/save}, /xsolla/*,
  /addCoins, /resetAccount, /playerSettings_update            -> account server (Phase 4;
                                                                  503 JSON until enabled)

Two rewrites are applied while serving (files on disk are never modified; both are listed
at /__local/rewrites and can be switched off in the stack config):

  1. client.js: the `$config = {...}` literal is replaced by the archived values merged
     with the master's /master/client-config overlay -- local gameServers urls
     (<public_host>:<port>) and useDevAccountServer/devAccountServerUrl pointing at this
     gateway. The rest of client.js is byte-for-byte the archived file.
  2. index.html: the Cloudflare Turnstile loader is replaced by the local stand-in
     shims/turnstile_local.js (the page only starts client.js from Turnstile's onload).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from services import common  # noqa: E402

VERSION = "1.0.0"
TURNSTILE_RE = re.compile(rb"https://challenges\.cloudflare\.com/turnstile/v0/api\.js")
DISCORD_BUTTON_RE = re.compile(rb'(<button id="discordLoginBtn"[^>]*>.*?</button>)', re.S)
LOCAL_LOGIN_BUTTON = (b'\n      <button id="localLoginBtn" class="discordLoginBtn" type="button" '
                      b'style="background:#2d8b2d" onclick="location.href=\'/auth/local\'">'
                      b'Login with a local account</button>')
LOCAL_TURNSTILE = b"/__local/turnstile_local.js"
LIVE_SKIN_RE = re.compile(r"^/(?:\./)?assets/s/(\d+)/.+\.png$")
MASTER_GET = {"/servers/get", "/master/regions", "/master/client-config", "/master/health"}
ACCOUNT_EXACT = {"/shop/get", "/shop/purchases", "/shop/buy", "/shop/wardrobe/save", "/addCoins",
                 "/resetAccount", "/playerSettings_update"}
ACCOUNT_PREFIXES = ("/auth/", "/xsolla/")


class ClientConfigRewriter:
    """Replace the `$config = {...}` literal at the top of client.js."""

    def __init__(self, path: Path, log):
        self.path = path
        self.log = log
        self.lock = threading.Lock()
        self._parsed = None      # (mtime, data, start, end, base_config)
        self._rendered = None    # (key, bytes)

    def _parse(self):
        mtime = self.path.stat().st_mtime
        if self._parsed and self._parsed[0] == mtime:
            return self._parsed
        import rebuild_client_js as rb  # tree-sitter based helpers from Phase 2
        data = self.path.read_bytes()
        tree = rb.make_parser().parse(data)
        for statement in tree.root_node.named_children[:5]:
            expr = statement.named_children[0] if statement.type == "expression_statement" and \
                statement.named_children else None
            if expr is not None and expr.type == "assignment_expression":
                left, right = expr.child_by_field_name("left"), expr.child_by_field_name("right")
                if left is not None and rb.node_text(left, data) == "$config" and right.type == "object":
                    base = rb.literal_value(right, data)
                    if "$expression" in json.dumps(base):
                        raise ValueError("$config contains non-literal values; refusing to rewrite it")
                    self._parsed = (mtime, data, right.start_byte, right.end_byte, base)
                    self.log.info("client.js: $config literal found (bytes %d-%d, %d keys)", right.start_byte,
                                  right.end_byte, len(base))
                    return self._parsed
        raise ValueError("no top-level `$config = {...}` found in client.js")

    def render(self, overlay: dict) -> bytes:
        with self.lock:
            mtime, data, start, end, base = self._parse()
            key = hashlib.sha256(json.dumps([mtime, overlay], sort_keys=True).encode()).hexdigest()
            if self._rendered and self._rendered[0] == key:
                return self._rendered[1]
            merged = copy.deepcopy(base)
            merged.update(overlay)
            body = data[:start] + json.dumps(merged, ensure_ascii=True).encode("ascii") + data[end:]
            self._rendered = (key, body)
            return body

    def archived_config(self) -> dict:
        with self.lock:
            return copy.deepcopy(self._parse()[4])


class Gateway:
    def __init__(self, config: dict, log):
        self.config = config
        self.log = log
        self.root = common.project_path(config, config["client_root"]).resolve()
        if not (self.root / "index.html").is_file():
            raise common.ConfigError(f"client_root has no index.html: {self.root}")
        self.rewrites = {"client_config": True, "turnstile_shim": True, "local_login_button": True,
                         **config["web"].get("rewrites", {})}
        self.updates_file = Path(config.get("settings", {}).get("WEB_UPDATES_FILE", "")) \
            if config.get("settings", {}).get("WEB_UPDATES_FILE") else None
        self.shim = PROJECT / "shims" / "turnstile_local.js"
        self.client_js = ClientConfigRewriter(self.root / "client.js", log)
        live = config.get("settings", {}).get("SHOP_LIVE_SKINS_DIR") or ""
        self.live_dir = Path(live) if live else None
        self._live = (None, {})
        self.master = common.master_url(config)
        self.account = common.account_url(config)
        self.remote = config.get("remote")
        self.upstream_headers = common.remote_headers(config)
        self.allowed_origins = set(config["web"].get("allowed_origins", [])) | {common.web_origin(config)}
        self.started_at = time.time()
        self._last_overlay = None

    def overlay(self) -> tuple[dict, str]:
        """The $config overlay, from the master (fallback: last known, then static config)."""
        try:
            status, _, raw = common.http_raw("GET", self.master + "/master/client-config",
                                             headers=self.upstream_headers, timeout=5.0 if self.remote else 2.0)
            body = json.loads(raw) if status == 200 else None
        except (OSError, ValueError) as exc:  # master down / refused / not JSON
            status, body = f"unreachable ({exc.__class__.__name__})", None
        if status == 200 and isinstance(body, dict):
            if self.remote:
                # The browser reaches the remote regions through the local relay, and the
                # account API through this gateway.
                body = dict(body)
                body["gameServers"] = [{**server, "url": common.region_public_url(self.config, server)}
                                       for server in body.get("gameServers", [])]
                body["devAccountServerUrl"] = common.web_origin(self.config)
            self._last_overlay = body
            return body, "remote master" if self.remote else "master"
        if self._last_overlay is not None:
            self.log.warning("master unavailable (%s); serving last known region list", status)
            return self._last_overlay, "cached"
        self.log.warning("master unavailable (%s); serving region list from the stack config", status)
        servers = [{"id": r["id"], "name": r["name"], "url": common.region_public_url(self.config, r),
                    "region": r["region"], "gm": int(r.get("gm", 0))}
                   for r in self.config["regions"] if r.get("enabled", True)]
        settings = self.config.get("settings", {})
        env = settings.get("CLIENT_ENV", "production")
        return {"gameServers": servers, "useDevAccountServer": True,
                "devAccountServerUrl": common.web_origin(self.config), "env": env,
                "isProduction": env == "production", "isTestingMode": bool(settings.get("CLIENT_TESTING_MODE")),
                "isBetaMode": bool(settings.get("CLIENT_BETA_MODE"))}, "config"

    def live_skin(self, path: str) -> Path | None:
        """/assets/s/<season>/....png for a season assigned by import_live_skins.py -> its picture."""
        match = LIVE_SKIN_RE.match(path)
        if not match or self.live_dir is None:
            return None
        manifest = self.live_dir / "manifest.json"
        try:
            mtime = manifest.stat().st_mtime
        except OSError:
            return None
        if self._live[0] != mtime:
            try:
                seasons = json.loads(manifest.read_text(encoding="utf-8")).get("seasons", {})
            except (OSError, ValueError) as exc:
                self.log.error("live skins: cannot read %s (%s)", manifest, exc)
                seasons = {}
            self._live = (mtime, seasons)
        rel = self._live[1].get(match.group(1))
        if not rel:
            return None
        file = (self.live_dir / rel).resolve()
        return file if self.live_dir.resolve() in file.parents and file.is_file() else None

    def static_path(self, path: str) -> Path | None:
        rel = unquote(path).lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None


def make_handler(gw: Gateway):
    log = gw.log

    class Handler(BaseHTTPRequestHandler):
        server_version = f"mope-local-gateway/{VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- responses -----------------------------------------------------------
        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and origin in gw.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Credentials", "true")

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None, head: bool = False):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self._cors()
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _json(self, status: int, payload, head: bool = False):
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json", head=head)

        def _proxy(self, base: str, head: bool = False):
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() in ("content-type", "accept")}
            headers.update(gw.upstream_headers)
            try:
                status, resp_headers, data = common.http_raw(self.command, base + self.path, body, headers,
                                                             timeout=15.0 if gw.remote else 5.0)
            except OSError as exc:
                log.warning("upstream %s unreachable: %s", base, exc)
                return self._json(502, {"success": False, "error": "upstream service unavailable"}, head)
            ctype = resp_headers.get("Content-Type", "application/json")
            return self._send(status, data, ctype, head=head)

        # -- routing ---------------------------------------------------------------
        def _route(self, head: bool = False):
            path = urlsplit(self.path).path
            if path == "/__local/health":
                return self._json(200, {"ok": True, "service": "web", "version": VERSION,
                                        "uptime_seconds": round(time.time() - gw.started_at, 1)}, head)
            if path == "/__local/rewrites":
                overlay, source = gw.overlay()
                return self._json(200, {"rewrites": gw.rewrites, "client_root": str(gw.root),
                                        "config_overlay": overlay, "overlay_source": source}, head)
            if path == "/updates.json" and gw.updates_file is not None and gw.updates_file.is_file():
                return self._send(200, gw.updates_file.read_bytes(), "application/json", head=head)
            if path == "/__local/turnstile_local.js":
                return self._send(200, gw.shim.read_bytes(), "application/javascript", head=head)
            if path in MASTER_GET:
                if self.command not in ("GET", "HEAD"):
                    return self._json(405, {"error": "method not allowed"}, head)
                return self._proxy(gw.master, head)
            if path in ACCOUNT_EXACT or path.startswith(ACCOUNT_PREFIXES):
                if gw.account is None:
                    return self._json(503, {"success": False,
                                            "error": "local account server not enabled yet (Phase 4)"}, head)
                return self._proxy(gw.account, head)
            if self.command not in ("GET", "HEAD"):
                return self._json(405, {"error": "method not allowed"}, head)
            return self._static(path, head)

        def _static(self, path: str, head: bool):
            live = gw.live_skin(path)
            if live is not None:
                return self._send(200, live.read_bytes(), "image/webp", {"X-Local-Rewrite": "live skin picture"},
                                  head)
            file = gw.static_path(path)
            if file is None:
                return self._send(404, b"not in the archived snapshot\n", "text/plain", head=head)
            name = file.name
            if name == "client.js" and file.parent == gw.root and gw.rewrites["client_config"]:
                overlay, source = gw.overlay()
                try:
                    body = gw.client_js.render(overlay)
                except (ValueError, OSError) as exc:
                    log.error("client.js $config rewrite failed (%s); serving the archived file", exc)
                    body = file.read_bytes()
                    source = "none (rewrite failed)"
                return self._send(200, body, "application/javascript",
                                  {"X-Local-Rewrite": f"$config overlay from {source}"}, head)
            data = file.read_bytes()
            if name == "index.html" and file.parent == gw.root:
                notes = []
                if gw.rewrites["turnstile_shim"]:
                    data, count = TURNSTILE_RE.subn(LOCAL_TURNSTILE, data)
                    notes.append(f"turnstile loader -> local shim ({count})")
                if gw.rewrites["local_login_button"] and gw.account is not None:
                    data, count = DISCORD_BUTTON_RE.subn(lambda m: m.group(1) + LOCAL_LOGIN_BUTTON, data, count=1)
                    notes.append(f"local login button ({count})")
                return self._send(200, data, "text/html; charset=utf-8", {"X-Local-Rewrite": "; ".join(notes)}, head)
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8" if "charset" not in ctype else ""
            return self._send(200, data, ctype, head=head)

        def do_GET(self):
            self._route()

        def do_HEAD(self):
            self._route(head=True)

        def do_POST(self):
            self._route()

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local mope.io web gateway")
    parser.add_argument("--config", help="stack config (default config/local_stack.json)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    config = common.load_stack_config(args.config)
    log = common.setup_logging(config, "web", args.verbose)
    gw = Gateway(config, log)
    if gw.rewrites["client_config"]:
        gw.client_js.archived_config()  # parse once at startup so errors show early
    bind, port = config["bind_host"], int(config["web"]["port"])
    server = ThreadingHTTPServer((bind, port), make_handler(gw))
    server.daemon_threads = True
    log.info("web gateway %s on http://%s:%d -> open %s/ (client root %s; rewrites %s; account server %s%s)",
             VERSION, bind, port, common.web_origin(config), gw.root,
             ", ".join(k for k, v in gw.rewrites.items() if v) or "none", gw.account or "not enabled",
             f"; REMOTE backend, game traffic via the relay on :{gw.remote['relay_port']}" if gw.remote else "")

    def shutdown(signum, frame):
        log.info("web gateway stopping (signal %s)", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    server.server_close()
    log.info("web gateway stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
