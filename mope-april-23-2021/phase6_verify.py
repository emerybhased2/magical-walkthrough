#!/usr/bin/env python3
"""
phase6_verify.py -- Phase 6 (task 33): complete integration test of the running local stack.

  C1  configuration validates
  S1  every service is up: master, account server, web gateway, and every enabled region
      process; the master sees every region online
  R1  region selection: /servers/get (through the gateway) lists exactly the enabled regions, and
      the client.js served to the browser carries every region in $config.gameServers
  G1  every region: a player joins, presses Play and spawns (17, 107, 18, 6, own animal)
  G2  multiplayer in every region: two players see each other with nicknames, and a leaving
      player is removed for the other (all regions in parallel)
  A1  account registration + login + @me through the gateway (local account server)
  W1  assets: the archived snapshot and the archived-image fallback are both served (X-Local-Source),
      the season-1 sprite path the client asks for exists, and path traversal is refused
  B1  the real client, one full session (load, Play, move): every failed local request is one of
      the known gaps (no archive holds them), and the regions log no unhandled client message
  V3/V4/V5  phase3_verify.py, phase4_verify.py and phase5_verify.py pass in full
  U1  unit tests: Python (unittest), Bun (decode_images), Ruby (fetch_live_assets)

Report: working/phase6/verify_report.json. Options: --quick skips V3-V5 and U1.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from services import common  # noqa: E402
from phase3_verify import Checks, sync_playwright  # noqa: E402
from phase5_verify import GameClient  # noqa: E402

VERSION = "1.0.0"

# Requests the archived client makes that no reachable archive can answer (see phase6/README.md).
KNOWN_GAPS = [
    (re.compile(r"^/audio/.+\.(mp3|ogg|wav)$"), "audio: no archive used holds mope.io audio"),
    (re.compile(r"^/img/howToPlay\d*\.png$"), "how-to-play images: not archived"),
    (re.compile(r"^/none$"), "client sets the logged-out avatar <img src='none'> (original behaviour)"),
]


def gateway(config: dict) -> str:
    return f"http://127.0.0.1:{int(config['web']['port'])}"


def enabled_regions(config: dict) -> list[dict]:
    return [r for r in config["regions"] if r.get("enabled", True)]


def run(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout after {timeout}s: {(exc.stdout or '')[-500:]}"
    except FileNotFoundError as exc:
        return 127, str(exc)


# -- services ---------------------------------------------------------------------------------------
def service_checks(config: dict, checks: Checks) -> None:
    base = gateway(config)
    web = common.http_json("GET", base + "/__local/health")[0] == 200
    master = common.http_json("GET", common.master_url(config) + "/master/health")[0] == 200
    account_cfg = config.get("account", {})
    account = (not account_cfg.get("enabled")) or common.http_json(
        "GET", f"http://127.0.0.1:{int(account_cfg['port'])}/auth/health")[0] == 200
    status, body = common.http_json("GET", common.master_url(config) + "/master/regions")
    view = {r["id"]: r for r in body.get("regions", [])} if status == 200 else {}
    regions = enabled_regions(config)
    offline = [r["id"] for r in regions if view.get(r["id"], {}).get("status") != "online"]
    checks.record("S1", "every service up (master, account, gateway, all regions online in the master)",
                  web and master and account and not offline,
                  f"web {web}, master {master}, account {account}, regions online {len(regions) - len(offline)}/"
                  f"{len(regions)}" + (f", offline {offline[:5]}" if offline else ""))


def region_selection(config: dict, checks: Checks) -> None:
    base = gateway(config)
    status, listed = common.http_json("GET", base + "/servers/get")
    ids = {e["id"] for e in listed} if status == 200 else set()
    want = {r["id"] for r in enabled_regions(config)}
    code, headers, body = common.http_raw("GET", base + "/client.js")
    head = body[:60000].decode("utf-8", "replace") if code == 200 else ""
    in_config = {rid for rid in want if f'"{rid}"' in head or f"'{rid}'" in head}
    checks.record("R1", "region selection: /servers/get and the served $config.gameServers list every enabled region",
                  ids == want and in_config == want,
                  f"/servers/get {len(ids)}/{len(want)}, $config.gameServers {len(in_config)}/{len(want)}"
                  + (f", missing {sorted(want - ids)[:4]}" if want - ids else ""))


# -- gameplay in every region ------------------------------------------------------------------------------
def one_region(config: dict, region: dict) -> dict:
    out = {"region": region["id"], "spawned": False, "multiplayer": False, "errors": []}
    a = b = None
    try:
        a = GameClient(region, "IntegA")
        a.pump(1.5, lambda: common.OP_CONNECTED in a.opcodes)
        a.play()
        a.pump(4, lambda: a.view.me is not None)
        order = [op for op in a.opcodes if op in (17, 107, 18, 6)][:4]
        out["spawned"] = order == [17, 107, 18, 6] and (a.view.me or {}).get("nick") == "IntegA"
        b = GameClient(region, "IntegB")
        b.pump(1.5, lambda: common.OP_CONNECTED in b.opcodes)
        b.play()
        b.pump(4, lambda: b.view.me is not None)
        cx, cy = config["gameplay"]["world_width"] / 2, config["gameplay"]["world_height"] / 2
        deadline = time.time() + 25
        seen = False
        while time.time() < deadline and not seen:
            a.steer(cx, cy)
            b.steer(cx + 50, cy)
            a.pump(0.1)
            b.pump(0.1)
            seen = b.view.my_id in a.view.objects and a.view.my_id in b.view.objects
        names = (a.view.objects.get(b.view.my_id, {}).get("nick"), b.view.objects.get(a.view.my_id, {}).get("nick"))
        a_id = a.view.my_id
        a.close()
        a = None
        gone = b.pump(4, lambda: a_id not in b.view.objects)
        out["multiplayer"] = seen and names == ("IntegB", "IntegA") and gone
        out["errors"] = b.errors
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(repr(exc))
    finally:
        for client in (a, b):
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
    return out


def gameplay_checks(config: dict, checks: Checks) -> None:
    regions = enabled_regions(config)
    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        results = list(pool.map(lambda r: one_region(config, r), regions))
    spawned = [r["region"] for r in results if r["spawned"]]
    multi = [r["region"] for r in results if r["multiplayer"]]
    failed = [f"{r['region']}: {r['errors'][:1]}" for r in results if not (r["spawned"] and r["multiplayer"])]
    checks.record("G1", "every region: join, Play, spawn (17, 107, 18, 6 + own animal)", len(spawned) == len(regions),
                  f"{len(spawned)}/{len(regions)} regions", data=results)
    checks.record("G2", "multiplayer in every region: two players see each other; a leaver is removed",
                  len(multi) == len(regions), f"{len(multi)}/{len(regions)} regions" + (f"; {failed[:3]}" if failed else ""))


# -- accounts and assets -----------------------------------------------------------------------------------------
def account_checks(config: dict, checks: Checks) -> None:
    base = gateway(config)
    name, password = f"i{secrets.token_hex(3)}", secrets.token_urlsafe(12)
    s1, reg = common.http_json("POST", base + "/auth/local/register", {"username": name, "password": password})
    s2, login = common.http_json("POST", base + "/auth/local/login", {"username": name, "password": password})
    data = (login or {}).get("data") or {}
    s3, me = common.http_json("POST", base + "/auth/@me", {"userId": data.get("userId"), "token": data.get("token")})
    s4, _ = common.http_json("POST", base + "/auth/local/login", {"username": name, "password": "wrong-password"})
    ok = (s1 == 200 and reg.get("success") and s2 == 200 and login.get("success") and s3 == 200
          and (me.get("data") or {}).get("userName") == name and s4 == 400)
    checks.record("A1", "account registration, login, @me and a refused wrong password (through the gateway)", ok,
                  f"register {s1}, login {s2}, @me {s3}, wrong password {s4}")


def asset_checks(config: dict, checks: Checks) -> None:
    base = gateway(config)
    results = {}
    for path in ("/index.html", "/img/logo.png", "/assets/s/1/skins/land/mouse/0/mouse.png", "/img/update/promo3.png",
                 "/skins/bear.png", "/img/%2e%2e/%2e%2e/config.json"):
        code, headers, body = common.http_raw("GET", base + path)
        results[path] = (code, {k.lower(): v for k, v in headers.items()}.get("x-local-source", ""), len(body))
    sources = {v[1].split(":")[0] for v in results.values() if v[0] == 200}
    images_fetched = (PROJECT / "archive" / "images" / "files").is_dir()
    ok = (results["/index.html"][0] == 200 and results["/assets/s/1/skins/land/mouse/0/mouse.png"][0] == 200
          and results["/img/%2e%2e/%2e%2e/config.json"][0] == 404
          and (not images_fetched or (results["/img/update/promo3.png"][0] == 200 and "fallback" in sources)))
    checks.record("W1", "assets: snapshot + archived-image fallback served, season-1 sprite present, traversal refused",
                  ok, ", ".join(f"{p} {c}{' ' + s if s else ''}" for p, (c, s, _) in results.items()))


# -- the real client --------------------------------------------------------------------------------------------------
def browser_check(config: dict, checks: Checks) -> None:
    from phase4_verify import ClientPage
    host = config["public_host"]
    region_logs = sorted((PROJECT / "logs" / "services").glob("region-*.log"))
    before = {p: p.stat().st_size for p in region_logs}
    failures: list[str] = []
    external: set[str] = set()
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[f"--host-resolver-rules=MAP {host} 127.0.0.1"])
        client = ClientPage(browser, config)

        def on_response(response):
            parts = urlsplit(response.url)
            if parts.hostname == host and response.status >= 400:
                failures.append(f"{response.status} {parts.path}")
        client.page.on("response", on_response)
        client.page.on("requestfailed", lambda r: external.add(urlsplit(r.url).hostname or "?")
                       if urlsplit(r.url).hostname != host else failures.append(f"FAILED {urlsplit(r.url).path}"))
        client.page.on("pageerror", lambda e: errors.append(str(e)[:160]))
        client.goto("/")
        client.pump(8, lambda: client.ev("getComputedStyle(document.getElementById('connecting')).display") == "none")
        client.page.fill("#nickInput", "Integ")
        client.page.click("#startButton")
        client.pump(4)
        for x, y in ((1000, 300), (300, 600), (900, 650)):
            client.page.mouse.move(x, y)
            client.pump(1.5)
        client.page.screenshot(path=str(OUT_DIR / "phase6_session.png"))
        browser.close()
        # the bridge's WebSocket connections live in this process: close them, or the session's player
        # stays connected while the earlier verifiers count players
        with client.bridge.lock:
            sockets = list(client.bridge.conns.values())
        for ws in sockets:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
    unhandled = []
    for path in region_logs:
        with path.open("rb") as handle:
            handle.seek(before.get(path, 0))
            unhandled += [line for line in handle.read().decode("utf-8", "replace").splitlines() if "not handled yet" in line]
    unexpected = [f for f in failures if not any(rx.match(f.split(" ", 1)[1]) for rx, _ in KNOWN_GAPS)]
    page_errors = [e for e in errors if "no supported source" not in e and "no supported sources" not in e]
    checks.record("B1", "real client session: only known gaps fail, no unhandled client messages, no page errors",
                  not unexpected and not unhandled and not page_errors,
                  f"known gaps {sorted(set(failures) - set(unexpected))}, unexpected {unexpected[:5]}, "
                  f"unhandled opcodes {len(unhandled)}, page errors {page_errors[:2]}, external hosts blocked "
                  f"{len(external)}", data={"failures": failures, "external_blocked": sorted(external)})


# -- earlier verifiers and unit tests ------------------------------------------------------------------------------
def wait_until_idle(config: dict, timeout: float = 30.0) -> int:
    """Wait until the master reports no players in any region (earlier verifiers count players exactly)."""
    deadline = time.time() + timeout
    players = -1
    while time.time() < deadline:
        status, body = common.http_json("GET", common.master_url(config) + "/master/regions")
        players = sum(int(r.get("players", 0)) for r in body.get("regions", [])) if status == 200 else -1
        if players == 0:
            return 0
        time.sleep(1)
    return players


def subverifier(check_id: str, script: str, checks: Checks, timeout: int = 600) -> None:
    code, output = run([sys.executable, script], timeout)
    summary = re.findall(r"(\d+)/(\d+) checks passed", output)
    detail = f"{summary[-1][0]}/{summary[-1][1]} checks" if summary else output.strip().splitlines()[-1:] or ["no output"]
    failed = [line.strip() for line in output.splitlines() if "[FAIL]" in line]
    checks.record(check_id, f"{script} passes in full", code == 0, f"{detail}" + (f"; {failed[:2]}" if failed else ""))


def unit_tests(checks: Checks) -> None:
    results = []
    code, out = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], 600)
    ran = re.search(r"Ran (\d+) tests", out)
    results.append(("python", code == 0, f"{ran.group(1) if ran else '?'} tests"))
    if shutil.which("bun") and (PROJECT / "node_modules").is_dir():
        code, out = run(["bun", "test", "tests/"], 300)
        passed = re.search(r"(\d+) pass", out)
        results.append(("bun", code == 0, f"{passed.group(1) if passed else '?'} tests"))
    if shutil.which("ruby"):
        total, ok = 0, True
        for test in ("tests/test_live_assets.rb", "tests/test_pumpkins.rb", "tests/test_live_skins.rb"):
            code, out = run(["ruby", test], 300)
            runs = re.search(r"(\d+) runs", out)
            total += int(runs.group(1)) if runs else 0
            ok = ok and code == 0
        results.append(("ruby", ok, f"{total} tests"))
    checks.record("U1", "unit tests: Python, Bun, Ruby", all(ok for _, ok, _ in results),
                  ", ".join(f"{name} {'ok' if ok else 'FAILED'} ({n})" for name, ok, n in results))


def main(argv=None) -> int:
    global OUT_DIR
    parser = argparse.ArgumentParser(description="Phase 6 integration test of the running local stack.")
    parser.add_argument("--config", default=str(common.DEFAULT_CONFIG))
    parser.add_argument("--quick", action="store_true", help="skip the Phase 3-5 verifiers and the unit tests")
    parser.add_argument("--no-browser", action="store_true", help="skip the real-client session (B1)")
    parser.add_argument("--report", help="report path (default working/phase6/verify_report.json)")
    args = parser.parse_args(argv)
    config = common.load_stack_config(args.config)
    OUT_DIR = common.project_path(config, "working/phase6")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    started = time.time()
    code, out = run([sys.executable, "localstack.py", "check-config"], 60)
    checks.record("C1", "configuration validates", code == 0, out.strip().splitlines()[-1] if out.strip() else "")
    if common.http_json("GET", gateway(config) + "/__local/health")[0] != 200:
        print("error: the stack is not running (./start-all.sh start)", file=sys.stderr)
        return 2
    print("Services and region selection")
    service_checks(config, checks)
    region_selection(config, checks)
    print("Gameplay in every region")
    gameplay_checks(config, checks)
    print("Accounts and assets")
    account_checks(config, checks)
    asset_checks(config, checks)
    if not args.no_browser:
        print("Real client session")
        try:
            browser_check(config, checks)
        except Exception as exc:  # noqa: BLE001
            checks.record("B1", "real client session", False, repr(exc))
    if not args.quick:
        print("Earlier phase verifiers")
        left = wait_until_idle(config)
        if left:
            print(f"  note: {left} player(s) still connected after 30 s; the earlier verifiers may count them")
        subverifier("V3", "phase3_verify.py", checks)
        subverifier("V4", "phase4_verify.py", checks)
        subverifier("V5", "phase5_verify.py", checks)
        print("Unit tests")
        unit_tests(checks)
    passed = sum(1 for r in checks.results if r["ok"])
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "verifier_version": VERSION,
              "seconds": round(time.time() - started, 1), "passed": passed, "failed": len(checks.results) - passed,
              "regions": len(enabled_regions(config)), "checks": checks.results}
    path = Path(args.report) if args.report else OUT_DIR / "verify_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(checks.results)} checks passed in {report['seconds']}s; report: {path}")
    return 0 if passed == len(checks.results) else 1


OUT_DIR = PROJECT / "working" / "phase6"

if __name__ == "__main__":
    sys.exit(main())
