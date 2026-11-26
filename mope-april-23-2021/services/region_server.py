#!/usr/bin/env python3
"""
region_server.py -- Phase 3 (task 15): one local gameplay-region server.

Run one process per region (localstack.py does this); each keeps the original region's
id, name and region label and listens on its own port. Connection protocol, as the
archived 2025 client expects it (phase2/README.md, task 12):

  ws://<host>:<port>/ping   client sends [255] (4 times); server echoes [255]
  ws://<host>:<port>/       game socket:
      C->S  1   join (canvasW, canvasH, innerWidth, sessionId)
      S->C  2   server info (sessionId, gameVersion, gameMode, season, serverId, alive, 0 objects)
      S->C  3   connected  -> the client hides "Connecting..." and shows the menu
      C->S  113 play (w, h, nickname)  -> S->C 16 notice, then the Phase 5 spawn:
                S->C 17 world size, 107 minimap, 18 your animal, 6 alive
      C->S  5   cursor world position -> the player's animal moves toward it
      S->C  4   world update every game.tick_seconds (services/game_world.py, game_protocol.py)
      S->C  18  again on evolution ("UPGRADED to ..." text in the client)
      C->S  11  clean leave
  shutdown: S->C 11 disconnect(code 0, reason) -> the client re-joins the best other server

Sessions: the session id from message 2 can be presented again in a join within
game.session_resume_seconds to resume it (the client does this after a connection loss).
The server registers with the master and sends heartbeats with its player count.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import signal
import sys
import time
from collections import Counter
from http import HTTPStatus
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common  # noqa: E402
from services import game_protocol as gp  # noqa: E402
from services.game_world import World  # noqa: E402

try:
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    serve = None

VERSION = "1.0.0"


class Connection:
    def __init__(self, ws, session_id: str, join: dict, resumed: bool):
        self.ws = ws
        self.session_id = session_id
        self.join = join
        self.resumed = resumed
        self.joined_at = time.time()
        self.nickname = None
        self.notified = False


class RegionServer:
    def __init__(self, config: dict, region: dict, log, token: str):
        self.config = config
        self.region = region
        self.log = log
        self.token = token
        game = config["game"]
        self.game_version = int(game.get("version", 296))
        self.season = int(game.get("season", 0))
        self.join_timeout = float(game.get("join_timeout_seconds", 10))
        self.resume_window = float(game.get("session_resume_seconds", 60))
        self.connections: dict[object, Connection] = {}
        self.resumable: dict[str, float] = {}
        self.opcodes = Counter()
        self.world = World(config)
        self.tick_seconds = float(config.get("gameplay", {}).get("tick_seconds", 0.1))
        self.started_at = time.time()
        self.stopping = False
        self.master_ok = None

    # -- bookkeeping -------------------------------------------------------------
    @property
    def players(self) -> int:
        return len(self.connections)

    def _new_session(self) -> str:
        return secrets.token_hex(8)

    def _claim_session(self, requested: str) -> tuple[str, bool]:
        now = time.time()
        self.resumable = {sid: exp for sid, exp in self.resumable.items() if exp > now}
        active = {c.session_id for c in self.connections.values()}
        if requested and requested in self.resumable and requested not in active:
            del self.resumable[requested]
            return requested, True
        return self._new_session(), False

    # -- websocket handlers --------------------------------------------------------
    def process_request(self, connection, request):
        path = request.path.split("?", 1)[0]
        if path not in ("/", "/ping"):
            return connection.respond(HTTPStatus.NOT_FOUND, "unknown path\n")
        if self.stopping:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "region shutting down\n")
        return None

    async def handler(self, ws):
        path = ws.request.path.split("?", 1)[0]
        if path == "/ping":
            await self._ping(ws)
        else:
            await self._game(ws)

    async def _ping(self, ws):
        try:
            async for message in ws:
                if isinstance(message, (bytes, bytearray)) and message[:1] == bytes([common.OP_PING]):
                    await ws.send(bytes([common.OP_PING]))
        except ConnectionClosed:
            pass

    async def _game(self, ws):
        remote = ws.remote_address[0] if ws.remote_address else "?"
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=self.join_timeout)
        except (asyncio.TimeoutError, ConnectionClosed):
            self.log.info("%s: no join within %.0fs; closing", remote, self.join_timeout)
            await ws.close(code=1008, reason="join expected")
            return
        if not isinstance(first, (bytes, bytearray)) or not first or first[0] != common.OP_JOIN:
            self.log.warning("%s: first packet was not a join (%s); closing", remote,
                             first[:16].hex() if isinstance(first, (bytes, bytearray)) else type(first).__name__)
            await ws.close(code=1002, reason="join expected")
            return
        try:
            join = common.parse_join(bytes(first))
        except ValueError as exc:
            self.log.warning("%s: malformed join (%s)", remote, exc)
            await ws.close(code=1002, reason="malformed join")
            return
        session_id, resumed = self._claim_session(join["session_id"])
        conn = Connection(ws, session_id, join, resumed)
        self.connections[ws] = conn
        await ws.send(common.msg_server_info(session_id, self.game_version, int(self.region.get("gm", 0)),
                                             self.season, self.region["id"], alive=False))
        await ws.send(common.msg_connected())
        self.log.info("join %s session=%s%s canvas=%dx%d players=%d", remote, session_id,
                      " (resumed)" if resumed else "", join["canvas_width"], join["canvas_height"], self.players)
        try:
            async for message in ws:
                if not isinstance(message, (bytes, bytearray)) or not message:
                    continue
                if await self._dispatch(conn, bytes(message)):
                    break
        except ConnectionClosed:
            pass
        finally:
            self.world.remove_player(ws)
            self.connections.pop(ws, None)
            self.resumable[session_id] = time.time() + self.resume_window
            self.log.info("leave session=%s after %.1fs players=%d", session_id,
                          time.time() - conn.joined_at, self.players)

    async def _dispatch(self, conn: Connection, data: bytes) -> bool:
        """Handle one client packet; True means the connection should end."""
        opcode = data[0]
        self.opcodes[opcode] += 1
        if opcode == common.OP_LEAVE:
            self.log.info("session=%s left cleanly (type 11)", conn.session_id)
            await conn.ws.close(code=1000, reason="bye")
            return True
        if opcode == common.OP_PLAY:
            try:
                play = common.parse_play(data)
            except ValueError as exc:
                self.log.warning("session=%s malformed play packet: %s", conn.session_id, exc)
                return False
            conn.nickname = play["nickname"]
            self.log.info("session=%s pressed Play as %r (%dx%d)", conn.session_id, play["nickname"],
                          play["width"], play["height"])
            if not conn.notified:
                conn.notified = True
                await conn.ws.send(common.msg_notification(
                    f"Local region {self.region['name']}: early gameplay - eat food to evolve (Phase 5)."))
            if conn.ws not in self.world.players:
                await self._spawn(conn, play)
            return False
        if opcode == gp.OP_INPUT_POS:
            try:
                x, y = gp.parse_input_pos(data)
            except ValueError:
                return False
            self.world.set_target(conn.ws, x, y)
            return False
        if opcode == common.OP_PING:
            await conn.ws.send(bytes([common.OP_PING]))
            return False
        if opcode == common.OP_CAPTCHA:
            self.log.info("session=%s sent a captcha token (ignored locally)", conn.session_id)
            return False
        level = self.log.info if self.opcodes[opcode] == 1 else self.log.debug
        level("session=%s opcode %d not handled yet (%d bytes): %s", conn.session_id, opcode, len(data),
              data[:48].hex(" "))
        return False

    # -- gameplay (Phase 5) ---------------------------------------------------------
    async def _spawn(self, conn: Connection, play: dict) -> None:
        canvas = (conn.join["canvas_width"], conn.join["canvas_height"])
        player = self.world.spawn_player(conn.ws, play["nickname"], canvas)
        ent = player.entity
        await conn.ws.send(gp.msg_game_size(self.world.width, self.world.height, ent.x, ent.y, self.world.zoom))
        await conn.ws.send(gp.msg_minimap(self.world.width, self.world.height))
        await conn.ws.send(self._animal_msg(player, change_kind=2))
        player.announce_animal = False
        await conn.ws.send(gp.msg_alive())
        self.log.info("session=%s spawned as animal type %d id=%d at (%.0f, %.0f)", conn.session_id,
                      player.animal_type, ent.id, ent.x, ent.y)

    def _animal_msg(self, player, change_kind: int) -> bytes:
        return gp.msg_your_animal(player.animal_type, player.entity.id, change_kind=change_kind,
                                  value=self.world.next_xp(player),
                                  edible_objects=self.world.edible_object_types())

    async def tick_loop(self):
        last = time.monotonic()
        while not self.stopping:
            await asyncio.sleep(self.tick_seconds)
            now = time.monotonic()
            self.world.tick(min(now - last, 0.5))
            last = now
            for ws, player in list(self.world.players.items()):
                if player.announce_animal:
                    player.announce_animal = False
                    self.log.info("player %s evolved to animal type %d (xp %d)", player.entity.nick or "?",
                                  player.animal_type, player.xp)
                    try:
                        await ws.send(self._animal_msg(player, change_kind=1))
                    except ConnectionClosed:
                        continue
                update = self.world.build_update(ws)
                if update is None:
                    continue
                try:
                    await ws.send(update)
                except ConnectionClosed:
                    pass
            self.world.end_tick()

    # -- master registration -----------------------------------------------------
    def _payload(self) -> dict:
        return {"id": self.region["id"], "port": int(self.region["port"]), "pid": os.getpid(),
                "version": VERSION, "players": self.players, "sessions": len(self.resumable) + self.players}

    async def _call_master(self, path: str, payload: dict) -> bool:
        url = common.master_url(self.config) + path
        try:
            status, body = await asyncio.to_thread(common.http_json, "POST", url, payload, self.token, 2.0)
        except OSError as exc:
            status, body = None, str(exc)
        ok = status == 200
        if ok != self.master_ok:
            if ok:
                self.log.info("master %s: %s", path.rsplit("/", 1)[-1], body.get("status") if isinstance(body, dict)
                              else body)
            else:
                self.log.warning("master unreachable or refused (%s %s); will keep retrying", status, body)
            self.master_ok = ok
        return ok

    async def heartbeat_loop(self):
        interval = float(self.config["master"].get("heartbeat_interval_seconds", 2))
        await self._call_master("/master/register", self._payload())
        while not self.stopping:
            await asyncio.sleep(interval)
            if not self.stopping:
                await self._call_master("/master/heartbeat", self._payload())

    async def shutdown(self, server):
        if self.stopping:
            return
        self.stopping = True
        reason = f"Local region '{self.region['name']}' is shutting down"
        self.log.info("shutting down: notifying %d player(s)", self.players)
        for conn in list(self.connections.values()):
            try:
                await conn.ws.send(common.msg_disconnect(reason, code=0))
                await conn.ws.close(code=1001, reason="region shutting down")
            except ConnectionClosed:
                pass
        await self._call_master("/master/deregister", {"id": self.region["id"], "reason": "shutdown"})
        server.close()


async def run(config: dict, region: dict, log, token: str) -> None:
    app = RegionServer(config, region, log, token)
    bind, port = config["bind_host"], int(region["port"])
    async with serve(app.handler, bind, port, process_request=app.process_request,
                     max_size=2 ** 20, server_header=f"mope-local-region/{VERSION}") as server:
        log.info("region %s (%s, %s) listening on ws://%s:%d  public url %s", region["id"], region["name"],
                 region["region"], bind, port, common.region_public_url(config, region))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(app.shutdown(server)))
        heartbeat = asyncio.create_task(app.heartbeat_loop())
        ticker = asyncio.create_task(app.tick_loop())
        await server.wait_closed()
        heartbeat.cancel()
        ticker.cancel()
    log.info("region %s stopped", region["id"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local mope.io region (gameplay) server")
    parser.add_argument("--region", required=True, help="region id from the stack config")
    parser.add_argument("--config", help="stack config (default config/local_stack.json)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if serve is None:
        print("error: pip install websockets", file=sys.stderr)
        return 2
    config = common.load_stack_config(args.config)
    region = common.region_by_id(config, args.region)
    log = common.setup_logging(config, f"region-{region['id']}", args.verbose)
    asyncio.run(run(config, region, log, common.stack_token(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
