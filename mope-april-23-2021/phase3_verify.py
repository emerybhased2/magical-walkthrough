#!/usr/bin/env python3
"""
phase3_verify.py -- Phase 3, tasks 19-20: verify the local stack end to end.

Protocol checks (Python WebSocket client playing the archived client's packets):
  P1  every region: /ping echo, join -> server info (2) + connected (3), play -> notice (16)
  P2  several clients on one region; the master's player count follows
  P3  session resume with the session id from message 2
  P4  abrupt disconnect, then reconnect
  P5  unavailable region: stop it -> clients get disconnect (11), master marks it offline and
      /servers/get omits it; start it -> online again
  P6  master restart: regions re-register through their heartbeats; discovery is restored
  P7  gateway with the master down: client.js is still served (last known region list)

Browser checks (the REAL archived client, served by the gateway, in headless Chromium):
  B1  client.js runs with the local $config (local region urls, local account origin)
  B2  server list shows every region with player counts from the master
  B3  ping test reaches every region
  B4  handshake completes ("Connecting..." hidden); the master sees the player
  B5  switching to each region through the server <select> connects to that region
  B6  Play reaches the region server and its notice is shown
  B7  stopping the current region moves the client to another region automatically

Browsers in some sandboxes cannot open WebSockets at all. The browser checks therefore
carry the page's WebSocket traffic over the DevTools protocol to real WebSocket
connections made from Python (a transport bridge); the client, gateway and region
servers are unmodified. On a normal machine the client connects directly.

Usage:
  python phase3_verify.py                 # uses the running stack
  python phase3_verify.py --start --stop  # start it first, stop it afterwards
"""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from services import common  # noqa: E402

try:
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover
    connect = None
try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

VERSION = "1.0.0"


def localstack(*args: str) -> str:
    result = subprocess.run([sys.executable, str(PROJECT / "localstack.py"), *args], capture_output=True,
                            text=True, cwd=PROJECT, timeout=60)
    return result.stdout + result.stderr


def wait_for(predicate, timeout: float, interval: float = 0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


class Checks:
    def __init__(self):
        self.results: list[dict] = []

    def record(self, check_id: str, name: str, ok: bool, detail: str = "", data=None):
        self.results.append({"id": check_id, "name": name, "ok": bool(ok), "detail": detail, "data": data})
        print(f"  [{'PASS' if ok else 'FAIL'}] {check_id} {name}" + (f" -- {detail}" if detail else ""))


# =============================================================================
# Protocol checks
# =============================================================================

class Protocol:
    def __init__(self, config: dict, checks: Checks):
        self.config = config
        self.checks = checks
        self.master = common.master_url(config)

    def ws_url(self, region: dict, path: str = "/") -> str:
        return f"ws://127.0.0.1:{int(region['port'])}{path}"

    def regions_view(self) -> dict:
        status, body = common.http_json("GET", self.master + "/master/regions", timeout=2)
        return {r["id"]: r for r in body["regions"]} if status == 200 else {}

    def stats(self) -> dict:
        status, body = common.http_json("GET", f"http://127.0.0.1:{self.config['web']['port']}/servers/get")
        return {e["id"]: e["totalPlayerCount"] for e in body} if status == 200 else {}

    def handshake(self, ws, session: str = "") -> dict:
        ws.send(common.build_join(session_id=session))
        info = common.PacketReader(ws.recv(timeout=3))
        result = {"type": info.u8(), "session_id": info.string_s2c(), "game_version": info.u16(),
                  "game_mode": info.u8(), "season": info.u8(), "server_id": info.string_s2c(),
                  "alive": info.u8(), "static_objects": info.u16()}
        result["next_type"] = ws.recv(timeout=3)[0]
        return result

    def p1_regions(self):
        version = int(self.config["game"]["version"])
        for region in self.config["regions"]:
            try:
                with connect(self.ws_url(region, "/ping"), proxy=None, open_timeout=3) as ws:
                    ws.send(b"\xff")
                    pong = ws.recv(timeout=3)
                with connect(self.ws_url(region), proxy=None, open_timeout=3) as ws:
                    hs = self.handshake(ws)
                    ws.send(common.build_play("verifier"))
                    notice = common.PacketReader(ws.recv(timeout=3))
                    notice_type, notice_text = notice.u8(), notice.string_s2c()
                    ws.send(bytes([common.OP_LEAVE]))
                ok = (pong == b"\xff" and hs["type"] == 2 and hs["next_type"] == 3 and hs["server_id"] == region["id"]
                      and hs["game_version"] == version and hs["game_mode"] == int(region.get("gm", 0))
                      and notice_type == 16 and region["name"] in notice_text)
                self.checks.record("P1", f"region {region['id']} ({region['name']}): ping, join, play", ok,
                                   f"session {hs['session_id']}, version {hs['game_version']}", hs)
            except Exception as exc:  # noqa: BLE001 - report any failure
                self.checks.record("P1", f"region {region['id']} ({region['name']})", False, repr(exc))

    def p2_multiple_clients(self):
        region = self.config["regions"][0]
        with contextlib.ExitStack() as stack:
            sockets = [stack.enter_context(connect(self.ws_url(region), proxy=None, open_timeout=3)) for _ in range(3)]
            sessions = [self.handshake(ws)["session_id"] for ws in sockets]
            counted = wait_for(lambda: self.stats().get(region["id"]) == 3, 6)
        drained = wait_for(lambda: self.stats().get(region["id"]) == 0, 6)
        self.checks.record("P2", f"3 clients on {region['id']}: master count 3, then 0 after they leave",
                           bool(counted and drained and len(set(sessions)) == 3), f"sessions {sessions}")

    def p3_resume(self):
        region = self.config["regions"][1]
        with connect(self.ws_url(region), proxy=None, open_timeout=3) as ws:
            first = self.handshake(ws)["session_id"]
        time.sleep(0.3)
        with connect(self.ws_url(region), proxy=None, open_timeout=3) as ws:
            again = self.handshake(ws, session=first)["session_id"]
        with connect(self.ws_url(region), proxy=None, open_timeout=3) as ws:
            bogus = self.handshake(ws, session="not-a-session")["session_id"]
        self.checks.record("P3", "session resume: same id returned; unknown ids get a new session",
                           first == again and bogus != "not-a-session", f"{first} -> {again}")

    def p4_disconnect_reconnect(self):
        region = self.config["regions"][2]
        with contextlib.suppress(Exception), connect(self.ws_url(region), proxy=None, open_timeout=3) as ws:
            self.handshake(ws)
            joined = wait_for(lambda: self.stats().get(region["id"]) == 1, 6)
            ws.socket.close()  # abrupt: no type 11, no close frame
        dropped = wait_for(lambda: self.stats().get(region["id"]) == 0, 8)
        with connect(self.ws_url(region), proxy=None, open_timeout=3) as ws2:
            hs = self.handshake(ws2)
        self.checks.record("P4", "abrupt disconnect is noticed; reconnecting works",
                           bool(joined and dropped and hs["type"] == 2))

    def p5_unavailable_region(self):
        region = self.config["regions"][-1]
        service = f"region-{region['id']}"
        notice = {}
        with contextlib.suppress(Exception), connect(self.ws_url(region), proxy=None, open_timeout=3) as ws:
            self.handshake(ws)
            localstack("stop", service)
            try:
                packet = common.PacketReader(ws.recv(timeout=5))
                notice = {"type": packet.u8(), "code": packet.u8(), "reason": packet.string_s2c(),
                          "flag": packet.u8()}
            except Exception as exc:  # noqa: BLE001
                notice = {"error": repr(exc)}
        offline = wait_for(lambda: self.regions_view().get(region["id"], {}).get("status") == "offline", 10)
        omitted = region["id"] not in self.stats()
        refused = False
        try:
            with connect(self.ws_url(region), proxy=None, open_timeout=2):
                pass
        except Exception:  # noqa: BLE001 - connection refused is the expected outcome
            refused = True
        self.checks.record("P5", f"stop {service}: clients told (type 11), master offline, omitted from "
                           f"/servers/get, connections refused",
                           bool(notice.get("type") == 11 and notice.get("code") == 0 and offline and omitted and refused),
                           f"disconnect notice: {notice}")
        localstack("start", service)
        online = wait_for(lambda: self.regions_view().get(region["id"], {}).get("status") == "online", 10)
        self.checks.record("P5", f"start {service} again: back online and listed", bool(online and
                           region["id"] in self.stats()))

    def p6_master_restart(self):
        before = self.regions_view()
        localstack("restart", "master")
        restored = wait_for(lambda: (lambda view: len(view) == len(self.config["regions"]) and
                                     all(r["status"] == "online" for r in view.values()))(self.regions_view()),
                            float(self.config["master"].get("heartbeat_interval_seconds", 2)) * 3 + 2)
        listed = len(self.stats()) == len(self.config["regions"])
        self.checks.record("P6", "master restart: every region re-registers via heartbeat; /servers/get restored",
                           bool(before and restored and listed))

    def p7_gateway_without_master(self):
        localstack("stop", "master")
        try:
            status, headers, body = common.http_raw("GET", f"http://127.0.0.1:{self.config['web']['port']}/client.js")
        except OSError as exc:
            status, headers, body = repr(exc), {}, b""
        finally:
            localstack("start", "master")
        ok = status == 200 and f"{self.config['public_host']}:".encode() in body[:4000]
        self.checks.record("P7", "gateway serves client.js with the local region list while the master is down",
                           ok, headers.get("X-Local-Rewrite", ""))
        wait_for(lambda: all(r["status"] == "online" for r in self.regions_view().values()), 10)

    def run(self):
        print("Protocol checks")
        for step in (self.p1_regions, self.p2_multiple_clients, self.p3_resume, self.p4_disconnect_reconnect,
                     self.p5_unavailable_region, self.p6_master_restart, self.p7_gateway_without_master):
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                self.checks.record(step.__name__.split("_")[0].upper(), step.__name__, False, repr(exc))


# =============================================================================
# Browser checks with the real archived client
# =============================================================================

BRIDGE_JS = r"""(() => {
  const sockets = new Map(); let nextId = 1;
  const toHex = b => Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  const fromHex = h => { const out = new Uint8Array(h.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16); return out; };
  class BridgeWebSocket extends EventTarget {
    constructor(url) {
      super(); this.url = String(url); this.readyState = 0; this.binaryType = 'blob'; this.protocol = '';
      this.extensions = ''; this.bufferedAmount = 0; this.onopen = this.onmessage = this.onclose = this.onerror = null;
      this._id = nextId++; sockets.set(this._id, this);
      window.__localBridge(JSON.stringify({op: 'open', id: this._id, url: this.url}));
    }
    _fire(type, ev) { this.dispatchEvent(ev); const h = this['on' + type]; if (typeof h === 'function') h.call(this, ev); }
    send(data) {
      if (this.readyState !== 1) throw new DOMException(
        "Failed to execute 'send' on 'WebSocket': Still in CONNECTING state.", 'InvalidStateError');
      let b; if (data instanceof ArrayBuffer) b = new Uint8Array(data);
      else if (ArrayBuffer.isView(data)) b = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      else b = new TextEncoder().encode(String(data));
      window.__localBridge(JSON.stringify({op: 'send', id: this._id, hex: toHex(b)}));
    }
    close(code = 1000) { if (this.readyState >= 2) return; this.readyState = 2;
      window.__localBridge(JSON.stringify({op: 'close', id: this._id, code})); }
  }
  for (const [n, v] of [['CONNECTING', 0], ['OPEN', 1], ['CLOSING', 2], ['CLOSED', 3]]) {
    Object.defineProperty(BridgeWebSocket, n, {value: v}); Object.defineProperty(BridgeWebSocket.prototype, n, {value: v}); }
  window.__bridgeDeliver = (id, kind, arg) => {
    const s = sockets.get(id); if (!s) return;
    if (kind === 'open') { s.readyState = 1; s._fire('open', new Event('open')); }
    else if (kind === 'message') { const b = fromHex(arg).buffer;
      s._fire('message', new MessageEvent('message', {data: s.binaryType === 'arraybuffer' ? b : new Blob([b])})); }
    else if (kind === 'error') s._fire('error', new Event('error'));
    else if (kind === 'close') { s.readyState = 3; sockets.delete(id);
      s._fire('close', new CloseEvent('close', {code: arg || 1006, wasClean: arg === 1000})); }
  };
  window.WebSocket = BridgeWebSocket;
})();"""

KEEP_CONSOLE_JS = """(() => { for (const n of ['log','warn','info','error','debug']) { const o = console[n];
  if (typeof o === 'function') Object.defineProperty(console, n, {configurable: false, get() { return o; }, set(_) {}}); } })();"""


class Bridge:
    """Carries the page's WebSocket traffic to real local WebSocket connections."""

    def __init__(self, public_host: str):
        self.public_host = public_host
        self.events: queue.Queue = queue.Queue()
        self.conns: dict[int, object] = {}
        self.opened: list[str] = []
        self.lock = threading.Lock()

    def _run(self, sid: int, url: str):
        parts = urlsplit(url)
        if parts.hostname != self.public_host:
            self.events.put((sid, "error", None))
            self.events.put((sid, "close", 1006))
            return
        target = f"ws://127.0.0.1:{parts.port or 80}{parts.path or '/'}"
        try:
            with connect(target, proxy=None, open_timeout=3) as ws:
                with self.lock:
                    self.conns[sid] = ws
                self.events.put((sid, "open", None))
                code = 1000
                try:
                    for message in ws:
                        data = message if isinstance(message, bytes) else message.encode()
                        self.events.put((sid, "message", data.hex()))
                except Exception as exc:  # noqa: BLE001
                    code = getattr(getattr(exc, "rcvd", None), "code", 1006)
                code = getattr(ws.close_rcvd, "code", code) if getattr(ws, "close_rcvd", None) else code
                self.events.put((sid, "close", code))
        except Exception:  # noqa: BLE001 - refused / failed handshake, like a real browser
            self.events.put((sid, "error", None))
            self.events.put((sid, "close", 1006))
        finally:
            with self.lock:
                self.conns.pop(sid, None)

    def on_binding(self, event: dict):
        if event.get("name") != "__localBridge":
            return
        message = json.loads(event["payload"])
        sid = message["id"]
        if message["op"] == "open":
            self.opened.append(message["url"])
            threading.Thread(target=self._run, args=(sid, message["url"]), daemon=True).start()
            return
        with self.lock:
            ws = self.conns.get(sid)
        if ws is None:
            return
        if message["op"] == "send":
            try:
                ws.send(bytes.fromhex(message["hex"]))
            except Exception:  # noqa: BLE001
                pass
        elif message["op"] == "close":
            threading.Thread(target=ws.close, daemon=True).start()


class Browser:
    def __init__(self, config: dict, checks: Checks, protocol: Protocol):
        self.config = config
        self.checks = checks
        self.protocol = protocol
        self.bridge = Bridge(config["public_host"])

    def pump(self, page, cdp, seconds: float, until=None):
        deadline = time.time() + seconds
        while time.time() < deadline:
            page.wait_for_timeout(15)
            while not self.bridge.events.empty():
                sid, kind, arg = self.bridge.events.get()
                cdp.send("Runtime.evaluate", {"expression": f"__bridgeDeliver({sid}, {json.dumps(kind)}, "
                                                            f"{json.dumps(arg)})"})
            if until is not None and until():
                return True
        return until() if until is not None else True

    def run(self):
        print("Browser checks (archived client)")
        origin = common.web_origin(self.config)
        host = self.config["public_host"]
        regions = self.config["regions"]
        with sync_playwright() as p:
            browser = p.chromium.launch(args=[f"--host-resolver-rules=MAP {host} 127.0.0.1"])
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.route("**/*", lambda r: r.continue_() if urlsplit(r.request.url).hostname == host else r.abort())
            cdp = context.new_cdp_session(page)
            cdp.on("Runtime.bindingCalled", self.bridge.on_binding)
            cdp.send("Runtime.enable")
            cdp.send("Runtime.addBinding", {"name": "__localBridge"})
            context.add_init_script(KEEP_CONSOLE_JS)
            context.add_init_script(BRIDGE_JS)

            def ev(expression: str):
                reply = cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                return reply.get("result", {}).get("value")

            def players() -> dict:
                return {rid: r.get("players", 0) for rid, r in self.protocol.regions_view().items()}

            def connected_region():
                counts = players()
                busy = [rid for rid, n in counts.items() if n > 0]
                hidden = ev("getComputedStyle(document.getElementById('connecting')).display") == "none"
                return busy[0] if hidden and len(busy) == 1 else None

            page.goto(origin + "/", wait_until="domcontentloaded", timeout=30000)
            self.pump(page, cdp, 4)

            cfg = json.loads(ev("JSON.stringify(typeof $config === 'undefined' ? null : $config)") or "null")
            expected = {f"{host}:{r['port']}" for r in regions}
            ok = bool(cfg) and {s["url"] for s in cfg["gameServers"]} == expected and \
                cfg.get("useDevAccountServer") is True and cfg.get("devAccountServerUrl") == origin
            self.checks.record("B1", "client.js runs with the local $config", ok,
                               f"{len(cfg['gameServers']) if cfg else 0} local servers, account origin "
                               f"{cfg.get('devAccountServerUrl') if cfg else None}")

            self.pump(page, cdp, 3)
            groups = ev("JSON.stringify([...document.querySelectorAll('#serverSelect option')].map(o => o.text))")
            options = json.loads(groups or "[]")
            ok = len(options) == len(regions) and all("players" in o and "..." not in o for o in options)
            self.checks.record("B2", "server list shows every region with player counts from the master", ok,
                               "; ".join(options[:3]) + (" ..." if len(options) > 3 else ""), options)

            pings = {urlsplit(u).port for u in self.bridge.opened if u.endswith("/ping")}
            self.checks.record("B3", "ping test opened /ping on every region",
                               pings == {int(r["port"]) for r in regions}, f"{len(pings)} regions pinged")

            current = self.pump(page, cdp, 15, connected_region) and connected_region()
            self.checks.record("B4", "handshake completes and the master counts the player", bool(current),
                               f"connected to {current}")

            # B5: switch to every region through the client's own <select>
            results = {}
            for index, region in enumerate(regions):
                ev(f"(() => {{ const s = document.getElementById('serverSelect'); s.selectedIndex = {index};"
                   f" s.dispatchEvent(new Event('change')); }})()")
                ok = self.pump(page, cdp, 10, lambda rid=region["id"]: connected_region() == rid)
                results[region["id"]] = bool(ok)
            self.checks.record("B5", "switching regions with the server list connects to each region",
                               all(results.values()), f"{sum(results.values())}/{len(results)} regions", results)

            # B6: press Play
            page.locator("#startButton").click(timeout=5000)
            shown = self.pump(page, cdp, 6, lambda: ev("document.body.innerText.includes('early gameplay')"))
            self.checks.record("B6", "Play reaches the region server and its notice is shown", bool(shown))
            page.screenshot(path=str(self.screenshot_dir / "phase3_client_connected.png"))

            # B7: stop the region the client is on; the client should move by itself
            before = self.pump(page, cdp, 3, connected_region) and connected_region()
            if before:
                localstack("stop", f"region-{before}")
                moved = self.pump(page, cdp, 20, lambda: (connected_region() or before) != before)
                after = connected_region()
                notice = ev("document.body.innerText.includes('is shutting down')")
                self.checks.record("B7", "stopping the current region moves the client to another region",
                                   bool(moved and after and after != before),
                                   f"{before} -> {after}; disconnect reason shown: {bool(notice)}")
                localstack("start", f"region-{before}")
            else:
                self.checks.record("B7", "stopping the current region moves the client", False, "not connected")
            browser.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify the local mope.io stack (Phase 3, tasks 19-20).")
    parser.add_argument("--config", default=str(common.DEFAULT_CONFIG))
    parser.add_argument("--start", action="store_true", help="start the stack first")
    parser.add_argument("--stop", action="store_true", help="stop the stack afterwards")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--report", help="report path (default working/phase3/verify_report.json)")
    args = parser.parse_args(argv)
    if connect is None:
        print("error: pip install websockets", file=sys.stderr)
        return 2
    config = common.load_stack_config(args.config)
    if args.start:
        print(localstack("start"))
    checks = Checks()
    protocol = Protocol(config, checks)
    if not protocol.regions_view():
        print("error: the stack is not running (python localstack.py start, or use --start)", file=sys.stderr)
        return 2
    started = time.time()
    protocol.run()
    out_dir = common.project_path(config, "working/phase3")
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_browser:
        if sync_playwright is None:
            checks.record("B*", "browser checks", False, "playwright not installed")
        else:
            browser = Browser(config, checks, protocol)
            browser.screenshot_dir = out_dir
            try:
                browser.run()
            except Exception as exc:  # noqa: BLE001
                checks.record("B*", "browser checks aborted", False, repr(exc))
    passed = sum(1 for r in checks.results if r["ok"])
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "verifier_version": VERSION,
              "seconds": round(time.time() - started, 1), "passed": passed,
              "failed": len(checks.results) - passed, "checks": checks.results}
    path = Path(args.report) if args.report else out_dir / "verify_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(checks.results)} checks passed in {report['seconds']}s; report: {path}")
    if args.stop:
        print(localstack("stop"))
    return 0 if passed == len(checks.results) else 1


if __name__ == "__main__":
    sys.exit(main())
