#!/usr/bin/env python3
"""
edge.py -- one public port in front of the stack, for hosting the backend (Sevalla etc.).

Two modes share the WebSocket piping:

  cloud   (run by cloud.py inside the hosted container, on the platform's $PORT)
      GET  /servers/get, /master/{regions,client-config,health}   -> master server (loopback)
      *    /auth/*, /shop/*, /xsolla/*, /addCoins, /resetAccount,
           /playerSettings_update                                 -> account server (loopback)
      WS   /r/<region-id>       -> ws://127.0.0.1:<region port>/
      WS   /r/<region-id>/ping  -> ws://127.0.0.1:<region port>/ping
      GET  /__edge/health
    Nothing else is forwarded: the master's internal register/heartbeat endpoints and the
    archived client are never reachable from outside. If BACKEND_ACCESS_KEY is set, every
    request (except /__edge/health) must carry it in the X-Backend-Key header.

  relay   (run by localstack.py when REMOTE_BACKEND_URL is set, on REMOTE_RELAY_PORT)
      WS   ws://<public_host>:<relay port>/r/<id>[/ping] -> wss://<remote>/r/<id>[/ping]
    The browser loads the client from the local gateway over plain http, so it opens ws://
    sockets; the relay turns them into wss:// connections to the hosted backend.

The hosted platform's proxy closes idle sockets (Cloudflare: ~100 s), so both sides send
protocol-level pings every 25 s. They are invisible to the game client.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common  # noqa: E402
from services.web_gateway import ACCOUNT_EXACT, ACCOUNT_PREFIXES, MASTER_GET  # noqa: E402

try:
    import aiohttp
    from aiohttp import WSMsgType, web
except ImportError:  # pragma: no cover
    aiohttp = None

VERSION = "1.0.0"
HEARTBEAT = 25.0
KEY_ENV = "BACKEND_ACCESS_KEY"
PUBLIC_PATHS = {"/", "/__edge/health"}   # readable without the access key
INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mope.io backend</title>
<style>
 :root{color-scheme:light dark;--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--ok:#1a7f37}
 @media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#f2f2f2;--muted:#a1a1a6;--ok:#3fb950}}
 body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,sans-serif}
 main{max-width:640px;margin:0 auto;padding:32px 16px}
 h1{font-size:1.4rem;margin:0 0 4px} .ok{color:var(--ok);font-weight:600} p,li{color:var(--muted)}
 code{font-size:.9em}
</style></head><body><main>
<h1>mope.io backend</h1>
<p class="ok">Running &middot; {regions} regions</p>
<p>This address is the shared <strong>server</strong> for a local mope.io archive: accounts, the region list and
the game servers. It has no game page, so opening it in a browser shows only this note.</p>
<p>To play, run the archived client on your own computer (it does not work on a phone) and point it here:</p>
<ul>
 <li>in <code>mope-april-23-2021/.env</code> set <code>REMOTE_BACKEND_URL</code> to this address and
 <code>REMOTE_BACKEND_KEY</code> to the access key you were given</li>
 <li>run <code>python localstack.py up</code> and open <code>http://localhost:8080/</code></li>
</ul>
<p>Every other address here needs the access key.</p>
</main></body></html>
"""
FORWARD_HEADERS = ("content-type", "accept")


async def pipe(client: "web.WebSocketResponse", upstream: "aiohttp.ClientWebSocketResponse") -> None:
    """Copy messages both ways until either side closes, then close the other with its code."""

    async def copy(src, dst, label):
        async for msg in src:
            if msg.type == WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
            elif msg.type == WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
        return label

    tasks = [asyncio.create_task(copy(client, upstream, "client")),
             asyncio.create_task(copy(upstream, client, "upstream"))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
    if not client.closed:
        await client.close(code=upstream.close_code or 1000)
    if not upstream.closed:
        await upstream.close(code=client.close_code or 1000)


class Edge:
    def __init__(self, config: dict, mode: str, log):
        self.config = config
        self.mode = mode
        self.log = log
        self.started_at = time.time()
        self.session: aiohttp.ClientSession | None = None
        self.connections = 0
        if mode == "cloud":
            self.key = os.environ.get(KEY_ENV, "")
            self.master = common.master_url(config)
            self.account = common.account_url(config)
            self.regions = {r["id"]: int(r["port"]) for r in config["regions"] if r.get("enabled", True)}
        else:
            remote = config["remote"]
            scheme = "wss" if remote["url"].startswith("https://") else "ws"
            self.upstream_base = f"{scheme}://{remote['url'].split('://', 1)[1]}"
            self.upstream_headers = common.remote_headers(config)

    async def start(self, app):
        # trust_env=False: never route loopback or backend traffic through a local proxy.
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=10))

    async def stop(self, app):
        if self.session:
            await self.session.close()

    # -- access ------------------------------------------------------------------------
    @web.middleware
    async def guard(self, request, handler):
        if self.mode == "cloud" and self.key and request.path not in PUBLIC_PATHS:
            given = request.headers.get(common.BACKEND_KEY_HEADER, "")
            if not hmac.compare_digest(given.encode(), self.key.encode()):
                return web.json_response({"success": False, "error": "backend access key required"}, status=401)
        return await handler(request)

    # -- handlers ----------------------------------------------------------------------
    async def health(self, request):
        info = {"ok": True, "service": f"edge-{self.mode}", "version": VERSION,
                "uptime_seconds": round(time.time() - self.started_at, 1), "open_sockets": self.connections}
        if self.mode == "cloud":
            info["regions"] = len(self.regions)
            info["access_key_required"] = bool(self.key)
        return web.json_response(info)

    async def index(self, request):
        return web.Response(text=INDEX_HTML.replace("{regions}", str(len(self.regions))),
                            content_type="text/html", charset="utf-8")

    async def http_proxy(self, request):
        path = request.path
        if path in MASTER_GET:
            if request.method not in ("GET", "HEAD"):
                return web.json_response({"error": "method not allowed"}, status=405)
            base = self.master
        elif path in ACCOUNT_EXACT or path.startswith(ACCOUNT_PREFIXES):
            base = self.account
            if base is None:
                return web.json_response({"success": False, "error": "account server not enabled"}, status=503)
        else:
            return web.json_response({"error": "not found"}, status=404)
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS}
        body = await request.read()
        try:
            async with self.session.request(request.method, base + request.rel_url.path_qs, data=body or None,
                                            headers=headers, allow_redirects=False,
                                            timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.read()
                return web.Response(status=resp.status, body=data,
                                    headers={"Content-Type": resp.headers.get("Content-Type", "application/json"),
                                             "Cache-Control": "no-store"})
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self.log.warning("upstream %s unreachable: %s", base, exc)
            return web.json_response({"success": False, "error": "upstream service unavailable"}, status=502)

    async def region_socket(self, request):
        region_id = request.match_info["region"]
        sub = request.match_info.get("sub", "")
        if sub not in ("", "ping"):
            return web.Response(status=404, text="unknown path\n")
        query = f"?{request.query_string}" if request.query_string else ""
        if self.mode == "cloud":
            port = self.regions.get(region_id)
            if port is None:
                return web.Response(status=404, text="unknown region\n")
            target, headers = f"ws://127.0.0.1:{port}/{sub}{query}", {}
        else:
            target = f"{self.upstream_base}/r/{region_id}" + (f"/{sub}" if sub else "") + query
            headers = self.upstream_headers
        try:
            upstream = await self.session.ws_connect(target, headers=headers, heartbeat=HEARTBEAT,
                                                     max_msg_size=2 ** 20, autoping=True)
        except aiohttp.WSServerHandshakeError as exc:
            self.log.info("region %s%s refused the socket: %s", region_id, f"/{sub}" if sub else "", exc.status)
            return web.Response(status=exc.status if 400 <= exc.status < 600 else 502, text="region refused\n")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.log.warning("region %s unreachable at %s: %s", region_id, target, exc)
            return web.Response(status=502, text="region unreachable\n")
        client = web.WebSocketResponse(heartbeat=HEARTBEAT, max_msg_size=2 ** 20)
        await client.prepare(request)
        self.connections += 1
        try:
            await pipe(client, upstream)
        finally:
            self.connections -= 1
            if not upstream.closed:
                await upstream.close()
        return client

    def app(self) -> "web.Application":
        app = web.Application(middlewares=[self.guard], client_max_size=2 ** 20)
        app.on_startup.append(self.start)
        app.on_cleanup.append(self.stop)
        app.router.add_get("/__edge/health", self.health)
        app.router.add_get("/r/{region}", self.region_socket)
        app.router.add_get("/r/{region}/{sub}", self.region_socket)
        if self.mode == "cloud":
            app.router.add_get("/", self.index)
            app.router.add_route("*", "/{tail:.*}", self.http_proxy)
        return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Single-port edge for the mope.io local stack")
    parser.add_argument("mode", choices=["cloud", "relay"])
    parser.add_argument("--config", help="stack config (default config/local_stack.json)")
    parser.add_argument("--host", help="bind address (cloud default 0.0.0.0, relay default the stack's bind_host)")
    parser.add_argument("--port", type=int, help="port (cloud default $PORT or 8080, relay default REMOTE_RELAY_PORT)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if aiohttp is None:
        print("error: pip install aiohttp", file=sys.stderr)
        return 2
    config = common.load_stack_config(args.config)
    if args.mode == "relay" and not config.get("remote"):
        print("error: relay mode needs REMOTE_BACKEND_URL", file=sys.stderr)
        return 2
    log = common.setup_logging(config, f"edge-{args.mode}", args.verbose)
    if args.mode == "cloud":
        host, port = args.host or "0.0.0.0", args.port or int(os.environ.get("PORT", "8080"))
    else:
        host, port = args.host or config["bind_host"], args.port or int(config["remote"]["relay_port"])
    edge = Edge(config, args.mode, log)
    if args.mode == "cloud":
        log.info("edge (cloud) %s on http://%s:%d: %d regions at /r/<id>; access key %s", VERSION, host, port,
                 len(edge.regions), "required" if edge.key else "not set")
    else:
        log.info("edge (relay) %s on ws://%s:%d/r/<id> -> %s/r/<id>", VERSION, host, port, edge.upstream_base)
    web.run_app(edge.app(), host=host, port=port, print=None, handle_signals=True, access_log=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
