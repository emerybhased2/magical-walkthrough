#!/usr/bin/env python3
"""
phase4_verify.py -- Phase 4 checks: configuration, account API and the REAL archived client
logging in against the local account server.

  C1  configuration validates (python localstack.py check-config)
  A1  API through the gateway: register, @me, wrong password, social sign-in refused,
      developer endpoints off by default, /updates.json served
  B1  the client's login popup offers "Login with a local account"
  B2  registering on the local login page signs the client in: name and coins shown
      (the client verifies the stored login with /auth/@me after joining a region)
  B3  the shop loads from the local account server
  B4  logging out through the client clears the login
  B5  stale or forged stored credentials are rejected and cleared by the client
  B6  the archived test-account hook (?testAccountSecret=&user=) logs in a test account
      (the account server is restarted with DEV_TEST_ACCOUNTS_ENABLED for this check, then
      restored)

Browser WebSocket traffic is bridged exactly as in phase3_verify.py.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from services import common
from services.account_store import INFINITE_BALANCE  # noqa: E402
from phase3_verify import BRIDGE_JS, KEEP_CONSOLE_JS, Bridge, Checks, localstack, sync_playwright  # noqa: E402

VERSION = "1.0.0"


def localstack_env(env: dict, *args: str) -> str:
    result = subprocess.run([sys.executable, str(PROJECT / "localstack.py"), *args], capture_output=True, text=True,
                            cwd=PROJECT, timeout=60, env={**os.environ, **env})
    return result.stdout + result.stderr


class ClientPage:
    """One browser context running the archived client through the WebSocket bridge."""

    def __init__(self, browser, config: dict):
        self.config = config
        self.host = config["public_host"]
        self.bridge = Bridge(self.host)
        self.context = browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = self.context.new_page()
        self.page.route("**/*", lambda r: r.continue_() if urlsplit(r.request.url).hostname == self.host else r.abort())
        self.cdp = self.context.new_cdp_session(self.page)
        self.cdp.on("Runtime.bindingCalled", self.bridge.on_binding)
        self.cdp.send("Runtime.enable")
        self.cdp.send("Runtime.addBinding", {"name": "__localBridge"})
        self.context.add_init_script(KEEP_CONSOLE_JS)
        self.context.add_init_script(BRIDGE_JS)

    def ev(self, expression: str):
        reply = self.cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return reply.get("result", {}).get("value")

    def pump(self, seconds: float, until=None) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.page.wait_for_timeout(15)
            while not self.bridge.events.empty():
                sid, kind, arg = self.bridge.events.get()
                self.cdp.send("Runtime.evaluate", {"expression": f"typeof __bridgeDeliver === 'function' && "
                                                                 f"__bridgeDeliver({sid}, {json.dumps(kind)}, {json.dumps(arg)})"})
            if until is not None:
                try:
                    if until():
                        return True
                except Exception:  # noqa: BLE001 - page may be navigating
                    pass
        return bool(until()) if until is not None else True

    def goto(self, path: str):
        self.page.goto(common.web_origin(self.config) + path, wait_until="domcontentloaded", timeout=30000)

    def logged_in_name(self):
        return self.ev("(() => { const d = document.getElementById('onloggedInDiv');"
                       " return d && getComputedStyle(d).display !== 'none' ?"
                       " document.getElementById('loggedInNameTxt').textContent : null; })()")

    def close(self):
        self.context.close()


def api_checks(config: dict, checks: Checks) -> None:
    base = f"http://127.0.0.1:{config['web']['port']}"
    name = f"v{secrets.token_hex(3)}"
    password = secrets.token_urlsafe(12)
    status, body = common.http_json("POST", base + "/auth/local/register", {"username": name, "password": password})
    ok = status == 200 and body.get("success")
    user_id, token = (body["data"]["userId"], body["data"]["token"]) if ok else (None, None)
    me = common.http_json("POST", base + "/auth/@me", {"userId": user_id, "token": token})[1] if ok else {}
    wrong = common.http_json("POST", base + "/auth/local/login", {"username": name, "password": "nope-nope"})[0]
    social = common.http_json("POST", base + "/auth/login/google", {"accessToken": "x"})
    dev = common.http_json("GET", f"{base}/addCoins?userId={user_id}&passwordToken={token}")[0]
    updates = common.http_raw("GET", base + "/updates.json")
    good = (ok and me.get("success") and me["data"]["userId"] == user_id and wrong == 400 and social[0] == 501
            and dev == 403 and updates[0] == 200 and isinstance(json.loads(updates[2]), list))
    checks.record("A1", "account API via the gateway: register, @me, wrong password, social refused, dev endpoints "
                  "off, /updates.json", good, f"user {name} ({user_id}); social: {social[1].get('error', '')[:60]}")


def browser_checks(config: dict, checks: Checks) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[f"--host-resolver-rules=MAP {config['public_host']} 127.0.0.1"])

        # B1-B4: register on the local page, play signed in, open the shop, log out
        client = ClientPage(browser, config)
        client.goto("/")
        client.pump(3)
        button = client.ev("(() => { const b = document.getElementById('localLoginBtn');"
                           " return b ? [b.textContent, b.closest('#loginButtons') !== null] : null; })()")
        checks.record("B1", "the client's login popup offers 'Login with a local account'",
                      bool(button and button[1]), str(button))

        name, password = f"p{secrets.token_hex(3)}", secrets.token_urlsafe(10)
        client.goto("/auth/local")
        client.page.fill("#username", name)
        client.page.fill("#password", password)
        client.page.click("#register")
        client.page.wait_for_url(common.web_origin(config) + "/", timeout=10000)
        # The name alone comes from localStorage; the coins are written from the /auth/@me answer.
        coins_text = "document.getElementById('loginCoins').textContent.trim()"
        expected_coins = (INFINITE_BALANCE if config["settings"].get("ACCOUNT_INFINITE_CURRENCY")
                          else config["settings"]["ACCOUNT_STARTING_COINS"])
        signed_in = client.pump(20, lambda: client.logged_in_name() == name and
                                client.ev(coins_text) == str(expected_coins))
        coins = client.ev(coins_text)
        stored = client.ev("localStorage.getItem('login_name')")
        checks.record("B2", "local registration signs the client in (name + coins from /auth/@me shown)",
                      bool(signed_in and stored == name), f"shown name {client.logged_in_name()!r}, coins {coins!r}")

        client.page.screenshot(path=str(OUT_DIR / "phase4_logged_in.png"))
        client.ev("document.getElementById('shopbutton3').click()")
        shop = client.pump(10, lambda: client.ev("!!document.getElementById('shop_div')"))
        failed = client.ev("document.body.innerText.includes('Could not connect to AccountServer')")
        checks.record("B3", "the shop opens with data from the local account server", bool(shop and not failed))
        client.ev("(() => { const o = document.getElementById('shopOverlay'); o && o.remove(); })()")

        client.ev("document.getElementById('btnLogout2').click()")
        out = client.pump(8, lambda: client.logged_in_name() is None and
                          client.ev("localStorage.getItem('login_userId')") is None)
        checks.record("B4", "logging out through the client clears the login", bool(out))
        client.close()

        # B5: forged credentials are rejected (@me -> 401) and the client clears them
        client = ClientPage(browser, config)
        client.goto("/auth/local")
        client.ev("localStorage.setItem('login_userId', 'local_forged'); localStorage.setItem('login_passwordToken',"
                  " 'forged'); localStorage.setItem('login_name', 'forged')")
        client.goto("/")
        cleared = client.pump(20, lambda: client.ev("localStorage.getItem('login_userId')") is None)
        checks.record("B5", "forged stored credentials are rejected and cleared by the client",
                      bool(cleared and client.logged_in_name() is None))
        client.close()

        # B6: the archived test-account hook, with test accounts enabled for this check only
        secret = "verify_" + secrets.token_hex(6)
        env = {"DEV_TEST_ACCOUNTS_ENABLED": "true", "DEV_TEST_ACCOUNT_SECRET": secret}
        localstack_env(env, "restart", "account")
        try:
            client = ClientPage(browser, config)
            client.goto(f"/?testAccountSecret={secret}&user=ab")
            ok = client.pump(20, lambda: client.logged_in_name() == "test-ab")
            stored = client.ev("localStorage.getItem('login_userId')")
            checks.record("B6", "the client's test-account hook logs in test-ab (userId = secret + user)",
                          bool(ok and stored == secret + "ab"), f"stored userId matches: {stored == secret + 'ab'}")
            client.close()
        finally:
            localstack("restart", "account")
        browser.close()


def main(argv=None) -> int:
    global OUT_DIR
    parser = argparse.ArgumentParser(description="Verify Phase 4 (accounts + configuration).")
    parser.add_argument("--config", default=str(common.DEFAULT_CONFIG))
    parser.add_argument("--report", help="report path (default working/phase4/verify_report.json)")
    args = parser.parse_args(argv)
    config = common.load_stack_config(args.config)
    OUT_DIR = common.project_path(config, "working/phase4")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    started = time.time()
    result = subprocess.run([sys.executable, str(PROJECT / "localstack.py"), "check-config"], capture_output=True,
                            text=True, cwd=PROJECT)
    checks.record("C1", "configuration validates (localstack.py check-config)", result.returncode == 0,
                  result.stdout.strip().splitlines()[-1] if result.stdout else result.stderr.strip()[:200])
    if common.http_json("GET", f"http://127.0.0.1:{config['web']['port']}/__local/health")[0] != 200:
        print("error: the stack is not running (python localstack.py start)", file=sys.stderr)
        return 2
    api_checks(config, checks)
    try:
        browser_checks(config, checks)
    except Exception as exc:  # noqa: BLE001
        checks.record("B*", "browser checks aborted", False, repr(exc))
    passed = sum(1 for r in checks.results if r["ok"])
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "verifier_version": VERSION,
              "seconds": round(time.time() - started, 1), "passed": passed,
              "failed": len(checks.results) - passed, "checks": checks.results}
    path = Path(args.report) if args.report else OUT_DIR / "verify_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(checks.results)} checks passed in {report['seconds']}s; report: {path}")
    return 0 if passed == len(checks.results) else 1


OUT_DIR = PROJECT / "working" / "phase4"

if __name__ == "__main__":
    sys.exit(main())
