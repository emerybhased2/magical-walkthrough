"""Offline tests for Phase 5 gameplay: message encoders/decoders, the world simulation and a
region server driven by a protocol client over a real local WebSocket.

Run from the project directory:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import common, game_protocol as gp  # noqa: E402
from services.game_world import Entity, World  # noqa: E402

LOG = logging.getLogger("test-gameplay")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

CHAIN = [{"animal": 1, "xp": 0, "radius": 16, "speed": 200},
         {"animal": 2, "xp": 10, "radius": 20, "speed": 190},
         {"animal": 3, "xp": 20, "radius": 25, "speed": 180}]


def game_config(**gameplay) -> dict:
    base = {"tick_seconds": 0.05, "world_width": 2000, "world_height": 2000, "zoom": 1.4, "food_object_type": 3,
            "food_count": 30, "food_radius": 9, "food_xp": 5, "evolution": CHAIN}
    base.update(gameplay)
    return {"gameplay": base, "game": {"version": 296}}


class BitGroupTests(unittest.TestCase):
    def test_layout(self):
        self.assertEqual(gp.bit_group([]), b"\x00")
        self.assertEqual(gp.bit_group([False, False]), b"\x00")          # trailing false bits are dropped
        self.assertEqual(gp.bit_group([True]), bytes([0b10]))
        self.assertEqual(gp.bit_group([False, False, True]), bytes([0b1000]))
        # 8 bits: first byte carries 7 and the continuation bit, second byte the 8th
        self.assertEqual(gp.bit_group([False] * 7 + [True]), bytes([0b1, 0b10]))

    def test_round_trip(self):
        rng = random.Random(4)
        for _ in range(50):
            bits = [rng.random() < 0.3 for _ in range(rng.randrange(0, 60))]
            decoded = gp.read_bit_group(common.PacketReader(gp.bit_group(bits)))
            decoded += [False] * (len(bits) - len(decoded))     # the client reads missing bits as false
            self.assertEqual(decoded[:len(bits)], bits)
            self.assertFalse(any(decoded[len(bits):]))


class MessageTests(unittest.TestCase):
    def test_angles(self):
        self.assertEqual(gp.angle_u8(90), 45)
        self.assertEqual(gp.angle_u8(360), 0)
        self.assertEqual(gp.angle_u16(1), 182)
        self.assertAlmostEqual(gp.facing_degrees(0, -1), 0)     # moving up = 0 degrees on the wire
        self.assertAlmostEqual(gp.facing_degrees(1, 0), 90)

    def test_game_size_layout(self):
        data = gp.msg_game_size(3000, 2000, 100.5, 50, 1.4)
        self.assertEqual(data, bytes([17]) + (3000).to_bytes(2, "big") + (2000).to_bytes(2, "big")
                         + (402).to_bytes(2, "big") + (200).to_bytes(2, "big") + (1400).to_bytes(2, "big"))

    def test_your_animal_round_trip(self):
        data = gp.msg_your_animal(2, 77, change_kind=1, value=90, edible_objects=[3])
        message = gp.decode_your_animal(data)
        self.assertEqual((message["animal_type"], message["my_id"], message["change_kind"], message["value"]),
                         (2, 77, 1, 90))
        self.assertEqual(message["lists"], [[], [], [], [3]])
        self.assertEqual(len(data), 1 + 1 + 1 + 2 + 1 + 1 + 4 + 4 + 2 + 2 + 4 + 1)

    def test_minimap_layout(self):
        data = gp.msg_minimap(3000, 3000)
        self.assertEqual(data[:2], bytes([107, 1]))
        self.assertEqual(len(data), 2 + 2 * 7)

    def test_world_update_round_trip(self):
        me = Entity(5, gp.OTYPE_ANIMAL, 100, 200, 16, subtype=1, angle=90, nick="Pip")
        food = Entity(6, 3, 120.25, 210.5, 9, angle=40)
        data = gp.msg_world_update((100, 200), 1.4, True, [me, food], [(me, {gp.PROP_POS: (101, 202),
                                                                             gp.PROP_ANGLE: 180})],
                                   [(7, 5), (8, None)], xp=35, water=100)
        update = gp.decode_world_update(data)
        self.assertEqual(update["header"], {"zoom": 1.4, "camera": (100, 200), "xp": 35, "water": 100})
        animal, eaten = update["new"]
        self.assertEqual((animal["otype"], animal["subtype"], animal["id"], animal["nick"]), (2, 1, 5, "Pip"))
        self.assertEqual((animal["x"], animal["y"], animal["rad"], animal["angle"]), (100, 200, 16, 90))
        self.assertEqual((eaten["otype"], eaten["subtype"], eaten["x"], eaten["y"]), (3, 0, 120.25, 210.5))
        self.assertEqual(update["updates"][0]["props"][gp.PROP_POS], (101, 202))
        self.assertAlmostEqual(update["updates"][0]["props"][gp.PROP_ANGLE], 180, places=1)
        self.assertEqual(update["removals"], [(7, 5), (8, None)])

    def test_header_without_xp_has_two_fields(self):
        data = gp.msg_world_update((0, 0), 1.0, False, [], [], [])
        self.assertEqual(data[1], 2)
        self.assertEqual(gp.decode_world_update(data, alive=False)["header"], {"zoom": 1.0, "camera": (0, 0)})

    def test_animal_flags_bitgroup_is_long_enough_for_the_client(self):
        # 52 false flags compress to one byte; the client then reads false for the rest.
        me = Entity(5, gp.OTYPE_ANIMAL, 1, 1, 16, subtype=1, nick="")
        self.assertTrue(gp.encode_new_object(me).endswith(b"\x00" + b"\x00\x00" + b"\x00"))

    def test_client_view_rejects_unknown_objects(self):
        view = gp.ClientView()
        ghost = Entity(9, 3, 1, 1, 9)
        with self.assertRaises(ValueError):
            view.feed(gp.msg_world_update((0, 0), 1, True, [], [(ghost, {gp.PROP_POS: (2, 2)})], []))

    def test_season_bytes_select_the_asset_folder(self):
        me = Entity(5, gp.OTYPE_ANIMAL, 1, 1, 16, subtype=1, nick="")
        food = Entity(6, 3, 1, 1, 9)
        update = gp.decode_world_update(gp.msg_world_update((0, 0), 1, True, [me, food], [], []))
        self.assertEqual([o["season"] for o in update["new"]], [1, 1])

    def test_resize(self):
        data = common.PacketWriter(gp.OP_RESIZE).u16(1600).u16(900).u16(1600).bytes()
        self.assertEqual(gp.parse_resize(data), (1600, 900, 1600))
        world = World(game_config(), rng=random.Random(2))
        world.spawn_player("a", "A", (1280, 800))
        world.resize("a", 1600, 900)
        self.assertEqual((world.views["a"].width, world.views["a"].height), (1600, 900))

    def test_pumpkin_custom_data(self):
        pumpkin = Entity(9, gp.OTYPE_PUMPKIN, 300, 400, 30, angle=90, pumpkin_type=10, rolling=True)
        data = gp.encode_new_object(pumpkin)
        self.assertEqual(data[-2:], bytes([1, 10]))           # isRolling, pumpkinType (L24957)
        update = gp.decode_world_update(gp.msg_world_update((0, 0), 1, True, [pumpkin], [], []))
        obj = update["new"][0]
        self.assertEqual((obj["otype"], obj["pumpkin_type"], obj["rolling"], obj["season"]), (131, 10, True, 1))

    def test_input_pos(self):
        data = common.PacketWriter(gp.OP_INPUT_POS).i16(-5).i16(1200).bytes()
        self.assertEqual(gp.parse_input_pos(data), (-5, 1200))


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.world = World(game_config(), rng=random.Random(1))

    def food(self):
        return [e for e in self.world.entities.values() if e.otype == 3]

    def test_spawn_and_food(self):
        self.assertEqual(len(self.food()), 30)
        player = self.world.spawn_player("a", "Pip", (1280, 800))
        self.assertEqual((player.animal_type, player.entity.subtype, player.entity.rad), (1, 1, 16))
        self.assertIs(self.world.spawn_player("a", "Pip", (1280, 800)), player)   # Play twice: same animal
        self.assertEqual(self.world.next_xp(player), 10)

    def test_pumpkins_cycle_through_installed_types(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "client" / "assets" / "s" / "1" / "img" / "pumpkin"
            folder.mkdir(parents=True)
            types = [{"type": t, "frames": [{"file": f"{t}/0.png"}]} for t in (0, 3, 10)]
            types.append({"type": 11, "frames": [{"missing": "404"}]})       # incomplete: not spawned
            (folder / "pumpkins.json").write_text(json.dumps({"types": types}))
            config = game_config(pumpkins={"count": 6, "radius": 25})
            config.update({"client_root": "client", "_root": tmp, "game": {"season": 1}})
            world = World(config, rng=random.Random(5))
        pumpkins = [e for e in world.entities.values() if e.otype == gp.OTYPE_PUMPKIN]
        self.assertEqual(sorted(p.pumpkin_type for p in pumpkins), [0, 0, 3, 3, 10, 10])
        self.assertTrue(all(p.rolling and p.rad == 25 for p in pumpkins))
        self.assertEqual(World(game_config(pumpkins={"count": 5}), rng=random.Random(1)).pumpkin_types, [])

    def test_movement_speed_and_clamp(self):
        player = self.world.spawn_player("a", "Pip", (1280, 800))
        player.entity.x, player.entity.y = 500, 500
        self.world.entities = {player.entity.id: player.entity}      # no food in the way
        self.world.set_target("a", 900, 500)
        self.world.tick(0.5)
        self.assertAlmostEqual(player.entity.x, 600)
        self.assertAlmostEqual(player.entity.angle, 90)
        self.world.set_target("a", -400, 99999)
        self.assertEqual(player.target, (0, 2000))

    def test_eating_gives_xp_and_respawns_food(self):
        player = self.world.spawn_player("a", "Pip", (1280, 800))
        food = self.food()[0]
        player.entity.x, player.entity.y = food.x + 5, food.y
        player.target = (player.entity.x, player.entity.y)
        self.world.tick(0.05)
        self.assertNotIn(food.id, self.world.entities)
        self.assertIn((food.id, player.entity.id), self.world.pending_removals)
        self.assertEqual((player.xp, player.eaten), (5, 1))
        self.assertEqual(len(self.food()), 30)

    def test_evolution_chain(self):
        player = self.world.spawn_player("a", "Pip", (1280, 800))
        player.announce_animal = False
        old_id = player.entity.id
        player.xp = 10
        self.world.tick(0.0)
        self.assertEqual((player.animal_type, player.tier, player.entity.subtype), (2, 1, 2))
        self.assertNotEqual(player.entity.id, old_id)
        self.assertIn((old_id, None), self.world.pending_removals)
        self.assertTrue(player.announce_animal)
        self.assertEqual((self.world.next_xp(player), player.entity.rad, player.speed), (20, 20, 190))
        player.xp = 500
        self.world.tick(0.0)
        self.assertEqual(player.animal_type, 3)
        self.world.tick(0.0)                      # the end of the chain: no further evolution
        self.assertEqual((player.animal_type, self.world.next_xp(player)), (3, 0))

    def test_updates_are_per_view(self):
        a = self.world.spawn_player("a", "A", (1280, 800))
        b = self.world.spawn_player("b", "B", (1280, 800))
        a.entity.x, a.entity.y, b.entity.x, b.entity.y = 300, 300, 1700, 1700
        a.target, b.target = (300, 300), (1700, 1700)
        first = gp.decode_world_update(self.world.build_update("a"))
        ids = {o["id"] for o in first["new"]}
        self.assertIn(a.entity.id, ids)
        self.assertNotIn(b.entity.id, ids)               # out of view
        self.assertEqual(first["header"]["xp"], 0)
        a.target = (340, 300)
        self.world.tick(0.1)
        second = gp.decode_world_update(self.world.build_update("a"))
        self.assertEqual(second["new"], [])
        self.assertEqual([u["id"] for u in second["updates"]], [a.entity.id])
        b.entity.x, b.entity.y = a.entity.x + 50, a.entity.y
        third = gp.decode_world_update(self.world.build_update("a"))
        self.assertEqual([o["nick"] for o in third["new"]], ["B"])
        self.world.remove_player("b")
        fourth = gp.decode_world_update(self.world.build_update("a"))
        self.assertIn((b.entity.id, None), fourth["removals"])


class RegionServerTests(unittest.TestCase):
    """A region server on an ephemeral port, played by a protocol client (no master needed)."""

    def test_join_play_move_eat(self):
        try:
            from websockets.asyncio.client import connect
            from websockets.asyncio.server import serve
            from services.region_server import RegionServer
        except ImportError:  # pragma: no cover
            self.skipTest("websockets not installed")

        async def scenario():
            config = game_config(food_count=200, world_width=600, world_height=600)
            server = RegionServer(config, {"id": "t1", "name": "Test 1", "region": "Land", "port": 0}, LOG, "x")
            async with serve(server.handler, "127.0.0.1", 0) as ws_server:
                port = ws_server.sockets[0].getsockname()[1]
                ticker = asyncio.create_task(server.tick_loop())
                view = gp.ClientView()
                opcodes = []
                try:
                    async with connect(f"ws://127.0.0.1:{port}/") as ws:
                        await ws.send(common.build_join())
                        await ws.send(common.build_play("Pip"))

                        async def pump(until, seconds):
                            loop = asyncio.get_running_loop()
                            deadline = loop.time() + seconds
                            while loop.time() < deadline and not until():
                                try:
                                    data = await asyncio.wait_for(ws.recv(), 0.1)
                                except asyncio.TimeoutError:
                                    continue
                                opcodes.append(data[0])
                                view.feed(data)
                            return until()

                        self.assertTrue(await pump(lambda: view.me is not None, 3))
                        # (dense food can evolve the player on the first tick: a second 18 may follow)
                        self.assertEqual([op for op in opcodes if op in (2, 3, 16, 17, 107, 18, 6)][:7],
                                         [2, 3, 16, 17, 107, 18, 6])
                        self.assertEqual(view.me["nick"], "Pip")
                        evolved = False
                        for _ in range(100):
                            food = view.nearest(3)
                            if food:
                                await ws.send(common.PacketWriter(gp.OP_INPUT_POS).i16(int(food["x"]))
                                              .i16(int(food["y"])).bytes())
                            if await pump(lambda: view.animal_type == 2, 0.1):
                                evolved = True
                                break
                        self.assertTrue(evolved, f"xp {view.header.get('xp')}")
                        await pump(lambda: view.me is not None and view.me["subtype"] == 2, 1)
                        self.assertEqual(view.animal_messages[-1]["change_kind"], 1)
                        self.assertEqual(view.next_xp, 20)
                        self.assertGreaterEqual(view.header["xp"], 10)
                finally:
                    server.stopping = True
                    ticker.cancel()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
