#!/usr/bin/env python3
"""
phase5_verify.py -- Phase 5 checks: local gameplay in a running region server, with protocol
clients and with the REAL archived client.

  C1  configuration validates (python localstack.py check-config)
  P1  Play spawns the player: S->C 17 world size, 107 minimap, 18 your animal, 6 alive, then
      a world update that contains the player's own animal (type 1, nickname)
  P2  movement: C->S 5 moves the animal toward the cursor at the configured speed
  P3  eating: food touched is removed with the player as killer and header XP rises
  P4  evolution: at the threshold the client gets S->C 18 (upgrade text, new animal type, new
      object id, next threshold) and the old animal object is removed
  P5  two players in one region see each other (nicknames) and a leaving player disappears
  P6  every S->C frame decodes with the client's reader layouts, with no unknown objects
  B1  the real client spawns, eats and evolves (steered by the decoded frames); the only page
      errors are the missing audio files

Browser WebSocket traffic is bridged exactly as in phase3_verify.py. Screenshots go to
working/phase5/.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

from services import common, game_protocol as gp  # noqa: E402
from phase3_verify import Checks, connect, sync_playwright  # noqa: E402

VERSION = "1.0.0"
AUDIO_ERRORS = ("no supported source", "The element has no supported sources")


class GameClient:
    """A protocol-level player: joins, plays, steers and keeps a decoded view of the world."""

    def __init__(self, region: dict, nickname: str):
        self.ws = connect(f"ws://127.0.0.1:{int(region['port'])}/", proxy=None, open_timeout=3,
                          max_size=2 ** 22).__enter__()
        self.nickname = nickname
        self.view = gp.ClientView()
        self.opcodes: list[int] = []
        self.frames = 0
        self.errors: list[str] = []
        self.ws.send(common.build_join())

    def play(self):
        self.ws.send(common.build_play(self.nickname))

    def steer(self, x: float, y: float):
        self.ws.send(gp.PacketWriter(gp.OP_INPUT_POS).i16(int(x)).i16(int(y)).bytes())

    def pump(self, seconds: float, until=None) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                data = self.ws.recv(timeout=0.05)
            except TimeoutError:
                data = None
            if isinstance(data, (bytes, bytearray)) and data:
                self.opcodes.append(data[0])
                self.frames += 1
                try:
                    self.view.feed(bytes(data))
                except ValueError as exc:
                    self.errors.append(f"opcode {data[0]}: {exc}")
            if until is not None and until():
                return True
        return bool(until()) if until is not None else True

    def close(self):
        try:
            self.ws.send(bytes([common.OP_LEAVE]))
        except Exception:  # noqa: BLE001
            pass
        self.ws.close()


def steer_to_food(client: GameClient, food_type: int, seconds: float, until) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        food = client.view.nearest(food_type)
        if food is not None:
            client.steer(food["x"], food["y"])
        if client.pump(0.12, until):
            return True
    return False


def protocol_checks(config: dict, region: dict, checks: Checks) -> None:
    game = config.get("gameplay", {})
    chain = game.get("evolution") or []
    food_type, food_xp = int(game.get("food_object_type", 3)), int(game.get("food_xp", 5))
    a = GameClient(region, "VerifyA")
    b = None
    try:
        a.pump(2, lambda: gp.OP_YOUR_ANIMAL in a.opcodes or common.OP_CONNECTED in a.opcodes)
        a.play()
        a.pump(4, lambda: a.view.me is not None)
        order = [op for op in a.opcodes if op in (gp.OP_GAME_SIZE, gp.OP_MINIMAP, gp.OP_YOUR_ANIMAL, gp.OP_ALIVE)]
        me = a.view.me or {}
        checks.record("P1", "Play spawns the player (17, 107, 18, 6, then its own animal in a world update)",
                      order[:4] == [17, 107, 18, 6] and me.get("otype") == gp.OTYPE_ANIMAL
                      and me.get("subtype") == chain[0]["animal"] and me.get("nick") == "VerifyA",
                      f"region {region['name']}: order {order[:4]}, me {({k: me.get(k) for k in ('id', 'subtype', 'nick')})}")

        start = (me.get("x", 0), me.get("y", 0))
        target_x = start[0] + 400 if start[0] < config["gameplay"]["world_width"] / 2 else start[0] - 400
        a.steer(target_x, start[1])
        a.pump(1.0)
        moved = abs(a.view.me["x"] - start[0]) if a.view.me else 0
        speed = float(chain[0]["speed"])
        checks.record("P2", "C->S 5 moves the animal toward the cursor at the configured speed",
                      0.6 * speed <= moved <= 1.3 * speed and abs(a.view.me["y"] - start[1]) < 2,
                      f"moved {moved:.0f} units in 1.0s (speed {speed:.0f})")

        xp_before, eaten_before = a.view.header.get("xp", 0), len(a.view.eaten_by)
        my_id = a.view.my_id
        ate = steer_to_food(a, food_type, 30, lambda: any(k == my_id for _, k in a.view.eaten_by[eaten_before:]))
        a.pump(0.3)
        xp_after = a.view.header.get("xp", 0)
        checks.record("P3", "touching food removes it with the player as killer and raises the header XP",
                      ate and xp_after >= xp_before + food_xp and a.view.header.get("water") == 100,
                      f"xp {xp_before} -> {xp_after}, water {a.view.header.get('water')}")

        if len(chain) > 1:
            old_id = a.view.my_id
            evolved = steer_to_food(a, food_type, 60, lambda: a.view.animal_type == chain[1]["animal"])
            a.pump(0.5)
            message = a.view.animal_messages[-1]
            nxt = chain[2]["xp"] if len(chain) > 2 else 0
            checks.record("P4", "evolution: S->C 18 with upgrade text, new animal type/id and next threshold; old "
                          "animal removed",
                          evolved and message["change_kind"] == 1 and message["my_id"] != old_id
                          and message["value"] == nxt and old_id not in a.view.objects
                          and (a.view.me or {}).get("subtype") == chain[1]["animal"],
                          f"animal {message['animal_type']} id {old_id}->{message['my_id']} next {message['value']} "
                          f"at xp {a.view.header.get('xp')}")
        else:
            checks.record("P4", "evolution", True, "skipped: the configured chain has one animal")

        b = GameClient(region, "VerifyB")
        b.pump(1)
        b.play()
        b.pump(3, lambda: b.view.me is not None)
        cx, cy = config["gameplay"]["world_width"] / 2, config["gameplay"]["world_height"] / 2
        deadline = time.time() + 25
        seen = False
        while time.time() < deadline and not seen:
            a.steer(cx, cy)
            b.steer(cx + 60, cy)
            a.pump(0.1)
            b.pump(0.1)
            seen = (b.view.my_id in a.view.objects and a.view.my_id in b.view.objects)
        names = (a.view.objects.get(b.view.my_id, {}).get("nick"), b.view.objects.get(a.view.my_id, {}).get("nick"))
        a_id = a.view.my_id
        a.close()
        gone = b.pump(3, lambda: a_id not in b.view.objects)
        checks.record("P5", "two players see each other (with nicknames); a leaving player disappears",
                      seen and names == ("VerifyB", "VerifyA") and gone, f"A sees {names[0]!r}, B sees {names[1]!r}, "
                      f"A removed for B: {gone}")
        errors = a.errors + b.errors
        checks.record("P6", "every S->C frame decodes with the client's reader layouts",
                      not errors and a.frames > 20 and b.frames > 20,
                      f"{a.frames + b.frames} frames" + (f"; {errors[:2]}" if errors else ""))
    finally:
        for client in (a, b):
            if client is not None:
                try:
                    client.ws.close()
                except Exception:  # noqa: BLE001
                    pass


def browser_check(config: dict, checks: Checks) -> None:
    from phase4_verify import ClientPage
    chain = config["gameplay"].get("evolution") or []
    food_type = int(config["gameplay"].get("food_object_type", 3))
    view = gp.ClientView()
    errors, decode_errors, logs = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[f"--host-resolver-rules=MAP {config['public_host']} 127.0.0.1"])
        client = ClientPage(browser, config)
        put = client.bridge.events.put

        def tap(item):
            if item[1] == "message":
                try:
                    view.feed(bytes.fromhex(item[2]))
                except ValueError as exc:
                    decode_errors.append(str(exc))
            put(item)
        client.bridge.events.put = tap
        client.page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        client.page.on("console", lambda m: logs.append(m.text[:120]))
        client.goto("/")
        client.pump(6, lambda: client.ev("getComputedStyle(document.getElementById('connecting')).display") == "none")
        client.page.fill("#nickInput", "Pip")
        client.page.click("#startButton")
        client.pump(4, lambda: view.me is not None)
        target = chain[1]["animal"] if len(chain) > 1 else chain[0]["animal"]
        deadline = time.time() + 60
        while time.time() < deadline and view.animal_type != target:
            me, food = view.me, view.nearest(food_type)
            if me and food:
                dx, dy = food["x"] - me["x"], food["y"] - me["y"]
                d = math.hypot(dx, dy) or 1
                client.page.mouse.move(640 + dx / d * 250, 400 + dy / d * 250)
            client.pump(0.15)
        client.pump(1.2)
        client.page.screenshot(path=str(OUT_DIR / "phase5_ingame.png"))
        menu_hidden = client.ev("getComputedStyle(document.getElementById('startMenuWrapper')).display") == "none"
        browser.close()
    spawned = any("spawned alive ingame" in line for line in logs)
    upgrades = sum(1 for line in logs if "MsgYourAnimalChanged" in line)
    other = [e for e in errors if not any(s in e for s in AUDIO_ERRORS)]
    checks.record("B1", "the real client spawns, eats and evolves; no page errors besides missing audio",
                  spawned and menu_hidden and view.animal_type == target and upgrades >= 2 and not other
                  and not decode_errors,
                  f"spawn logged {spawned}, menu hidden {menu_hidden}, "
                  f"animal {view.animal_type}, xp {view.header.get('xp')}, 18 handled {upgrades}x, "
                  f"other errors {other[:2]}; screenshot working/phase5/phase5_ingame.png")


def main(argv=None) -> int:
    global OUT_DIR
    parser = argparse.ArgumentParser(description="Verify Phase 5 (local gameplay).")
    parser.add_argument("--config", default=str(common.DEFAULT_CONFIG))
    parser.add_argument("--region", help="region id for the protocol checks (default: the first region)")
    parser.add_argument("--no-browser", action="store_true", help="skip the real-client check")
    parser.add_argument("--report", help="report path (default working/phase5/verify_report.json)")
    args = parser.parse_args(argv)
    config = common.load_stack_config(args.config)
    OUT_DIR = common.project_path(config, "working/phase5")
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
    regions = config["regions"]
    region = next((r for r in regions if r["id"] == args.region), None) if args.region else regions[0]
    if region is None:
        print(f"error: unknown region {args.region!r}", file=sys.stderr)
        return 2
    print("Protocol checks")
    try:
        protocol_checks(config, region, checks)
    except Exception as exc:  # noqa: BLE001
        checks.record("P*", "protocol checks aborted", False, repr(exc))
    if not args.no_browser:
        print("Browser check (archived client)")
        try:
            browser_check(config, checks)
        except Exception as exc:  # noqa: BLE001
            checks.record("B1", "browser check aborted", False, repr(exc))
    passed = sum(1 for r in checks.results if r["ok"])
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "verifier_version": VERSION,
              "region": region["id"], "seconds": round(time.time() - started, 1), "passed": passed,
              "failed": len(checks.results) - passed, "checks": checks.results}
    path = Path(args.report) if args.report else OUT_DIR / "verify_report.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\n{passed}/{len(checks.results)} checks passed in {report['seconds']}s; report: {path}")
    return 0 if passed == len(checks.results) else 1


OUT_DIR = PROJECT / "working" / "phase5"

if __name__ == "__main__":
    sys.exit(main())
