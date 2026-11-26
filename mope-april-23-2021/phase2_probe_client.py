#!/usr/bin/env python3
"""
phase2_probe_client.py -- Phase 2, task 8: serve the archived client locally and record
what it does in a real (headless) browser.

  * serves <project>/working/client/<domain>/ as the virtual host http://local.<domain>/
    inside the test browser. The 2025 client is domain-locked by its obfuscator (it
    blanks the page unless the hostname ends in .mope.io or another allowed domain), so
    it must run under such a name -- on your own machine you would add
    `127.0.0.1 local.mope.io` to your hosts file instead. client.js is NOT modified.
  * answers requests for external hosts from <project>/working/client/external-hosts/
    when a copy exists, serves local stand-ins from shims/ for scripts the page cannot
    start without (Cloudflare Turnstile), and BLOCKS + records everything else, so the
    probe never talks to a production server
  * replaces WebSocket inside the test page with a capture double (no network I/O) that
    records every frame the client sends -- e.g. its join packet -- and answers /ping
    probes, so the client's own region selection runs normally
  * keeps console output visible (the obfuscator replaces console.* with no-ops) and
    records console messages, page errors, missing local files, the populated server
    list and screenshots

Usage:
  python phase2_probe_client.py --project snapshots/2025-12-10
  python phase2_probe_client.py --project snapshots/2025-12-10 --seconds 30 --click-play
  python phase2_probe_client.py --project snapshots/2025-12-10 --no-shims --host 127.0.0.1
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import fetch_mope_archive as fm  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

VERSION = "1.2.0"

# Local stand-ins for third-party scripts the client cannot start without (local testing only).
SHIMS = {
    ("challenges.cloudflare.com", "/turnstile/v0/api.js"): SCRIPT_DIR / "shims" / "turnstile_local.js",
}

# Keep console.* usable after the obfuscator's "disable console output" routine runs:
# assignments to console methods are ignored, reads return the original functions.
KEEP_CONSOLE_JS = """(() => {
  for (const name of ['log', 'warn', 'info', 'error', 'exception', 'table', 'trace', 'debug']) {
    const original = console[name];
    if (typeof original !== 'function') continue;
    Object.defineProperty(console, name, {configurable: false, get() { return original; }, set(_) {}});
  }
})();"""

# In-page WebSocket test double. Browser WebSockets cannot connect in every environment
# (e.g. sandboxes without WebSocket egress), and Playwright's own WebSocket mocking does not
# work on this page, so the probe replaces window.WebSocket inside the test page only:
# sockets "open" immediately, every frame the client sends is recorded, and probes of
# /ping are answered with the 1-byte ping reply (255) the client expects. No network I/O.
CAPTURE_WS_JS = r"""(() => {
  const log = [];
  Object.defineProperty(window, '__wsCapture', {value: log, enumerable: false});
  const hex = bytes => Array.from(bytes.slice(0, 256), b => b.toString(16).padStart(2, '0')).join(' ');
  class CaptureWebSocket extends EventTarget {
    constructor(url, protocols) {
      super();
      this.url = String(url); this.protocol = ''; this.extensions = ''; this.binaryType = 'blob';
      this.bufferedAmount = 0; this.readyState = 0;
      this.onopen = this.onmessage = this.onclose = this.onerror = null;
      this._entry = {url: this.url, opened_ms: Math.round(performance.now()), sent: [], replies: 0};
      log.push(this._entry);
      setTimeout(() => this._fire('open', new Event('open'), () => { this.readyState = 1; }), 20);
    }
    _fire(type, event, before) {
      if (before) before();
      this.dispatchEvent(event);
      const handler = this['on' + type];
      if (typeof handler === 'function') handler.call(this, event);
    }
    send(data) {
      if (this.readyState !== 1) throw new DOMException(
        "Failed to execute 'send' on 'WebSocket': Still in CONNECTING state.", 'InvalidStateError');
      let bytes;
      if (data instanceof ArrayBuffer) bytes = new Uint8Array(data);
      else if (ArrayBuffer.isView(data)) bytes = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
      else bytes = new TextEncoder().encode(String(data));
      if (this._entry.sent.length < 200)
        this._entry.sent.push({ms: Math.round(performance.now()), len: bytes.length, hex: hex(bytes)});
      if (/\/ping$/.test(this.url) && bytes.length === 1 && bytes[0] === 255) {
        this._entry.replies += 1;
        setTimeout(() => {
          const payload = new Uint8Array([255]).buffer;
          this._fire('message', new MessageEvent('message', {data: this.binaryType === 'arraybuffer'
            ? payload : new Blob([payload])}));
        }, 25);
      }
    }
    close(code = 1000, reason = '') {
      if (this.readyState >= 2) return;
      this.readyState = 2;
      setTimeout(() => this._fire('close', new CloseEvent('close', {code, reason, wasClean: true}),
                                  () => { this.readyState = 3; }), 0);
    }
  }
  for (const [name, value] of [['CONNECTING', 0], ['OPEN', 1], ['CLOSING', 2], ['CLOSED', 3]]) {
    Object.defineProperty(CaptureWebSocket, name, {value});
    Object.defineProperty(CaptureWebSocket.prototype, name, {value});
  }
  window.WebSocket = CaptureWebSocket;
})();"""

STATE_JS = """(() => {
    const sel = document.getElementById('serverSelect');
    const groups = sel ? [...sel.querySelectorAll('optgroup')].map(g => ({
        label: g.label, options: [...g.querySelectorAll('option')].map(o => o.text)})) : null;
    const vis = id => { const el = document.getElementById(id);
        return el ? getComputedStyle(el).display + '/' + getComputedStyle(el).visibility : null; };
    return {
        href: location.href,
        title: document.title,
        client_js_ran: typeof $config !== 'undefined',
        game_version: typeof $config !== 'undefined' ? $config.gameVersion : null,
        server_groups: groups,
        connecting_overlay: vis('connecting'), start_menu: vis('startMenu'), play_button: vis('startButton'),
        canvas: !!document.querySelector('canvas'),
        connecting_text: (document.getElementById('txtServer') || {}).textContent || null,
    };
})()"""


def hexdump(data, limit: int = 96) -> str:
    raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8", "replace")
    text = raw[:limit].hex(" ")
    return text + (" ..." if len(raw) > limit else "")


def read_string(view: bytes, offset: int) -> tuple[str, int]:
    length = int.from_bytes(view[offset:offset + 2], "big")
    raw = view[offset + 2:offset + 2 + length]
    return raw.decode("utf-8", "replace"), offset + 2 + length


def decode_client_frame(frame: bytes) -> dict | None:
    """Decode the client packets whose layouts are fixed (see protocol_inventory.json)."""
    if not frame:
        return None
    opcode = frame[0]
    try:
        if opcode == 1 and len(frame) >= 9:   # join: 3 x UInt16, then session id string
            values = [int.from_bytes(frame[i:i + 2], "big") for i in (1, 3, 5)]
            session, _ = read_string(frame, 7)
            return {"message": "join (type 1)", "uint16_fields": values, "session_id": session}
        if opcode == 64 and len(frame) >= 3:  # captcha token string
            token, _ = read_string(frame, 1)
            return {"message": "captcha token (type 64)", "token": token}
        if opcode == 255:
            return {"message": "ping (type 255)"}
    except (ValueError, IndexError):
        return None
    return None


def probe(project: Path, host: str, seconds: float, click_play: bool, out_dir: Path,
          use_shims: bool = True) -> dict:
    config, _ = fm.load_config(SCRIPT_DIR / "config.json")
    domain = config["domain"]
    host = host or f"local.{domain}"
    client_root = project / "working" / "client"
    site_root = (client_root / domain).resolve()
    external_root = (client_root / "external-hosts").resolve()
    if not (site_root / "index.html").is_file():
        raise SystemExit(f"error: {site_root}/index.html not found; run fetch_mope_archive.py --working-copy first")

    base = f"http://{host}/"
    record = {"requests": [], "local_missing": [], "blocked": [], "served_external": [], "shimmed": [],
              "console": [], "page_errors": [], "websockets": [], "navigations": []}
    lock = threading.Lock()
    started = time.time()

    def stamp() -> float:
        return round(time.time() - started, 2)

    def on_route(route):
        url = route.request.url
        parts = urlsplit(url)
        if parts.hostname == host:
            rel = unquote(parts.path).lstrip("/") or "index.html"
            if rel.endswith("/"):
                rel += "index.html"
            local = (site_root / rel).resolve()
            if site_root in local.parents and local.is_file():
                ctype = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
                return route.fulfill(path=str(local), content_type=ctype,
                                     headers={"Cache-Control": "no-store"})
            with lock:
                record["local_missing"].append(parts.path)
            return route.fulfill(status=404, body="not in snapshot", content_type="text/plain")
        shim = SHIMS.get((parts.hostname, parts.path)) if use_shims else None
        if shim is not None:
            with lock:
                record["shimmed"].append(url)
            return route.fulfill(path=str(shim), content_type="application/javascript")
        local = (external_root / (parts.hostname or "") / unquote(parts.path.lstrip("/"))).resolve()
        if external_root in local.parents and local.is_file():
            with lock:
                record["served_external"].append(url)
            return route.fulfill(path=str(local))
        with lock:
            record["blocked"].append({"t": stamp(), "url": url, "type": route.request.resource_type,
                                      "method": route.request.method,
                                      "post_data": (route.request.post_data or "")[:300] or None})
        return route.abort("blockedbyclient")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        context.add_init_script(KEEP_CONSOLE_JS)
        context.add_init_script(CAPTURE_WS_JS)
        page = context.new_page()
        page.route("**/*", on_route)
        page.on("console", lambda m: record["console"].append(
            {"t": stamp(), "type": m.type, "text": m.text[:400]}))
        page.on("pageerror", lambda e: record["page_errors"].append({"t": stamp(), "error": str(e)[:600]}))
        page.on("request", lambda r: record["requests"].append({"t": stamp(), "url": r.url, "type": r.resource_type}))
        page.on("framenavigated", lambda f: record["navigations"].append({"t": stamp(), "url": f.url})
                if f == page.main_frame else None)
        page.goto(base, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(seconds * 1000)
        # Playwright's page.evaluate() returns nothing on this page, so query it through
        # the DevTools protocol instead (V8 serialises the value natively).
        cdp = context.new_cdp_session(page)

        def page_state() -> dict:
            reply = cdp.send("Runtime.evaluate", {"expression": STATE_JS, "returnByValue": True, "timeout": 5000})
            return reply.get("result", {}).get("value") or {
                "error": reply.get("exceptionDetails", {}).get("text", "state query failed")}

        state = page_state()
        suffix = "" if use_shims else "_no_shims"
        page.screenshot(path=str(out_dir / f"task8_menu{suffix}.png"))
        if click_play:
            try:
                page.click("#startButton", timeout=5000)
                page.wait_for_timeout(5000)
                page.screenshot(path=str(out_dir / f"task8_after_play{suffix}.png"))
                state["clicked_play"] = True
                state["after_play"] = page_state()
            except Exception as exc:  # the button stays hidden without a game server
                state["clicked_play"] = f"not possible: {str(exc).splitlines()[0][:200]}"
        capture = cdp.send("Runtime.evaluate", {"expression": "JSON.stringify(window.__wsCapture || [])",
                                                 "returnByValue": True}).get("result", {}).get("value") or "[]"
        browser.close()

    for sock in json.loads(capture):
        frames = []
        for sent in sock["sent"]:
            data = bytes.fromhex(sent["hex"].replace(" ", ""))
            frame = {"ms": sent["ms"], "bytes": sent["len"], "opcode": data[0] if data else None, "hex": sent["hex"]}
            decoded = decode_client_frame(data)
            if decoded:
                frame["decoded"] = decoded
            frames.append(frame)
        record["websockets"].append({"url": sock["url"], "opened_ms": sock["opened_ms"],
                                     "ping_replies_sent": sock["replies"], "frames_from_client": frames})

    blocked_hosts = Counter(urlsplit(b["url"]).hostname for b in record["blocked"])
    by_type = defaultdict(int)
    for req in record["requests"]:
        by_type[req["type"]] += 1
    console_errors = [c for c in record["console"] if c["type"] in ("error", "warning")]
    return {
        "generated_at": fm.now_iso(), "probe_version": VERSION, "project": str(project),
        "served_from": str(site_root), "url": base, "observed_seconds": seconds, "shims": use_shims,
        "page_state": state,
        "summary": {
            "requests": len(record["requests"]), "requests_by_type": dict(by_type),
            "local_missing": len(set(record["local_missing"])), "blocked_external": len(record["blocked"]),
            "served_external_from_snapshot": len(record["served_external"]), "shimmed": len(record["shimmed"]),
            "websockets": len(record["websockets"]),
            "frames_from_client": sum(len(w["frames_from_client"]) for w in record["websockets"]),
            "console_errors_and_warnings": len(console_errors), "page_errors": len(record["page_errors"]),
        },
        "navigations": record["navigations"],
        "local_missing": sorted(set(record["local_missing"])),
        "blocked_external_hosts": dict(blocked_hosts.most_common()),
        "blocked_external": record["blocked"],
        "served_external": record["served_external"],
        "shimmed": record["shimmed"],
        "websockets": record["websockets"],
        "console": record["console"],
        "page_errors": record["page_errors"],
        "requests": record["requests"],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve the archived client locally and record its behaviour.")
    parser.add_argument("--project", default=str(SCRIPT_DIR), help="project root containing working/client/")
    parser.add_argument("--host", help="virtual host name to serve under (default: local.<domain>)")
    parser.add_argument("--seconds", type=float, default=20, help="how long to observe the page")
    parser.add_argument("--click-play", action="store_true", help="also click the Play button")
    parser.add_argument("--no-shims", action="store_true",
                        help="do not stand in for blocked third-party scripts (shows the raw behaviour)")
    parser.add_argument("--out-dir", help="output directory (default: <project>/working/phase2)")
    args = parser.parse_args(argv)
    if sync_playwright is None:
        print("error: install Playwright for Python (pip install playwright)", file=sys.stderr)
        return 2
    project = Path(args.project).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else project / "working" / "phase2"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = probe(project, args.host, args.seconds, args.click_play, out_dir, use_shims=not args.no_shims)
    name = "task8_probe" + ("_no_shims" if args.no_shims else "") + \
        (f"_{re.sub(r'[^A-Za-z0-9]+', '_', args.host)}" if args.host else "")
    fm.write_json(out_dir / f"{name}.json", result)
    s, state = result["summary"], result["page_state"]
    print(f"served          : {result['url']} (virtual host) from {result['served_from']}")
    print(f"page            : href={state.get('href')} client_js_ran={state.get('client_js_ran')} "
          f"gameVersion={state.get('game_version')} canvas={state.get('canvas')}")
    groups = state.get("server_groups") or []
    print(f"server list     : {sum(len(g['options']) for g in groups)} servers in {len(groups)} region group(s)")
    print(f"requests        : {s['requests']} {s['requests_by_type']}")
    print(f"missing locally : {s['local_missing']}   blocked external: {s['blocked_external']} "
          f"{list(result['blocked_external_hosts'])[:8]}")
    print(f"websockets      : {s['websockets']} intercepted, {s['frames_from_client']} client frames captured")
    for ws in result["websockets"][:6]:
        first = ws["frames_from_client"][0] if ws["frames_from_client"] else None
        print(f"   {ws['url']:<58} first frame: {(first.get('decoded') or first['hex'][:48]) if first else '-'}")
    print(f"console         : {s['console_errors_and_warnings']} errors/warnings, {s['page_errors']} page errors")
    if state.get("clicked_play") is not None:
        print(f"play button     : {state['clicked_play']}")
    print(f"written to      : {out_dir / (name + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
