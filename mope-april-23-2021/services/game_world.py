"""
Authoritative gameplay simulation for one region (Phase 5, task 30) -- first milestone.

What is simulated now:
  * players spawn as the first animal of the evolution chain (Mouse, animal type 1) on Play
  * movement toward the cursor position the client sends (C->S 5), clamped to the world
  * food objects (configurable object type) that players eat on contact; eaten food is
    removed with the eater as "killer" (the client animates it into the mouth) and respawns
  * XP per food eaten, sent in world-update header field 11; water (field 16) stays full
  * evolution along a configurable chain (default Mouse -> Rabbit -> Pig): the old animal
    object is removed, a new one with the new animal type is created, and S->C 18 tells
    the client its new animal and the XP needed for the next one

Per-connection visibility: each client receives only objects inside its view rectangle
(derived from the canvas size in its join packet and the zoom). The world update for a
client lists objects that became visible, changed properties of visible objects, and
objects that left view or were removed (phase5/README.md).

Not reconstructed (documented as open work): the original evolution order/thresholds/
speeds (server-side data, not in the client), predators, combat and death, biomes, water
sources and drinking, hills and hiding holes, abilities, the leaderboard and player count.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from services import game_protocol as gp


@dataclass
class Entity:
    id: int
    otype: int
    x: float
    y: float
    rad: float
    subtype: int = 0
    angle: float = 0.0
    biome: int = 0
    nick: str = ""
    species: int = 0
    sub_species: int = 0
    hurt: bool = False
    season: int = 1            # mopeSeasonID: the client loads this object's images from ./assets/s/<season>/


@dataclass
class Player:
    entity: Entity
    conn_key: object
    animal_type: int
    target: tuple[float, float]
    speed: float
    xp: int = 0
    eaten: int = 0
    tier: int = 0
    announce_animal: bool = True     # send S->C 18 before the next world update


@dataclass
class View:
    """What one client currently has on screen: id -> (x, y, rad, angle) last sent."""
    known: dict = field(default_factory=dict)
    width: float = 1280
    height: float = 800


class World:
    def __init__(self, config: dict, rng: random.Random | None = None):
        game = config.get("gameplay", {})
        self.width = int(game.get("world_width", 3000))
        self.height = int(game.get("world_height", 3000))
        self.zoom = float(game.get("zoom", 1.4))
        self.food_type = int(game.get("food_object_type", 3))
        self.food_count = int(game.get("food_count", 220))
        self.food_rad = float(game.get("food_radius", 9))
        self.food_xp = int(game.get("food_xp", 5))
        # Evolution chain: the original order and thresholds were server-side and are not in the
        # client, so this is a configurable approximation (phase5/README.md).
        self.evolution = game.get("evolution") or [
            {"animal": 1, "xp": 0, "radius": 16, "speed": 220},
            {"animal": 2, "xp": 30, "radius": 20, "speed": 215},
            {"animal": 3, "xp": 90, "radius": 25, "speed": 205},
        ]
        self.rng = rng or random.Random()
        self.entities: dict[int, Entity] = {}
        self.players: dict[object, Player] = {}
        self.views: dict[object, View] = {}
        self.pending_removals: list[tuple[int, int | None]] = []
        self._next_id = 1
        for _ in range(self.food_count):
            self.spawn_food()

    # -- entities -----------------------------------------------------------------------
    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def random_point(self, margin: float = 40) -> tuple[float, float]:
        return (self.rng.uniform(margin, self.width - margin), self.rng.uniform(margin, self.height - margin))

    def spawn_food(self) -> Entity:
        x, y = self.random_point()
        food = Entity(self.new_id(), self.food_type, x, y, self.food_rad, angle=self.rng.uniform(0, 360))
        self.entities[food.id] = food
        return food

    def remove(self, entity_id: int, killer: int | None = None) -> None:
        if self.entities.pop(entity_id, None) is not None:
            self.pending_removals.append((entity_id, killer))

    # -- players --------------------------------------------------------------------------
    def spawn_player(self, conn_key, nick: str, canvas: tuple[int, int]) -> Player:
        if conn_key in self.players:
            return self.players[conn_key]
        x, y = self.random_point(200)
        tier = self.evolution[0]
        ent = Entity(self.new_id(), gp.OTYPE_ANIMAL, x, y, float(tier["radius"]), subtype=int(tier["animal"]),
                     angle=90, nick=(nick or "")[:15])
        self.entities[ent.id] = ent
        player = Player(ent, conn_key, int(tier["animal"]), (x, y), float(tier["speed"]))
        self.players[conn_key] = player
        self.views[conn_key] = View(width=canvas[0] or 1280, height=canvas[1] or 800)
        return player

    def remove_player(self, conn_key) -> None:
        player = self.players.pop(conn_key, None)
        self.views.pop(conn_key, None)
        if player is not None:
            self.remove(player.entity.id)

    def resize(self, conn_key, width: int, height: int) -> None:
        """C->S 17: the client's canvas changed size, so its view rectangle does too."""
        view = self.views.get(conn_key)
        if view is not None and width > 0 and height > 0:
            view.width, view.height = min(width, 8192), min(height, 8192)

    def set_target(self, conn_key, x: float, y: float) -> None:
        player = self.players.get(conn_key)
        if player is not None:
            player.target = (min(max(x, 0), self.width), min(max(y, 0), self.height))

    def edible_object_types(self) -> list[int]:
        return [self.food_type]

    # -- simulation ------------------------------------------------------------------------
    def tick(self, dt: float) -> None:
        for player in list(self.players.values()):
            ent = player.entity
            dx, dy = player.target[0] - ent.x, player.target[1] - ent.y
            distance = math.hypot(dx, dy)
            if distance > 1.0:
                step = min(distance, player.speed * dt)
                ent.x = min(max(ent.x + dx / distance * step, ent.rad), self.width - ent.rad)
                ent.y = min(max(ent.y + dy / distance * step, ent.rad), self.height - ent.rad)
                ent.angle = gp.facing_degrees(dx, dy)
            self._eat(player)

    def _eat(self, player: Player) -> None:
        ent = player.entity
        reach = ent.rad
        for food in [e for e in self.entities.values() if e.otype == self.food_type]:
            if (food.x - ent.x) ** 2 + (food.y - ent.y) ** 2 <= (reach + food.rad * 0.4) ** 2:
                self.remove(food.id, killer=ent.id)
                player.xp += self.food_xp
                player.eaten += 1
                self.spawn_food()
        self._maybe_evolve(player)

    def next_xp(self, player: Player) -> int:
        nxt = player.tier + 1
        return int(self.evolution[nxt]["xp"]) if nxt < len(self.evolution) else 0

    def _maybe_evolve(self, player: Player) -> None:
        nxt = player.tier + 1
        if nxt >= len(self.evolution) or player.xp < int(self.evolution[nxt]["xp"]):
            return
        tier = self.evolution[nxt]
        old = player.entity
        self.remove(old.id)
        new = Entity(self.new_id(), gp.OTYPE_ANIMAL, old.x, old.y, float(tier["radius"]),
                     subtype=int(tier["animal"]), angle=old.angle, nick=old.nick)
        self.entities[new.id] = new
        player.entity, player.tier = new, nxt
        player.animal_type, player.speed = int(tier["animal"]), float(tier["speed"])
        player.announce_animal = True

    # -- per-client updates ---------------------------------------------------------------------
    def build_update(self, conn_key) -> bytes | None:
        player = self.players.get(conn_key)
        view = self.views.get(conn_key)
        if player is None or view is None:
            return None
        cx, cy = player.entity.x, player.entity.y
        half_w = view.width / 2 / self.zoom + 120
        half_h = view.height / 2 / self.zoom + 120
        visible = {eid: e for eid, e in self.entities.items()
                   if abs(e.x - cx) <= half_w + e.rad and abs(e.y - cy) <= half_h + e.rad}
        new_objects, updates, removals = [], [], []
        for eid, ent in visible.items():
            state = (round(ent.x, 2), round(ent.y, 2), ent.rad, round(ent.angle, 1))
            if eid not in view.known:
                new_objects.append(ent)
            elif view.known[eid] != state:
                before = view.known[eid]
                props = {}
                if before[:2] != state[:2]:
                    props[gp.PROP_POS] = (ent.x, ent.y)
                if before[2] != state[2]:
                    props[gp.PROP_RAD] = ent.rad
                if before[3] != state[3] and ent.otype == gp.OTYPE_ANIMAL:
                    props[gp.PROP_ANGLE] = ent.angle
                if props:
                    updates.append((ent, props))
            view.known[eid] = state
        killers = dict(self.pending_removals)
        for eid in [k for k in view.known if k not in visible]:
            del view.known[eid]
            removals.append((eid, killers.get(eid)))
        return gp.msg_world_update((cx, cy), self.zoom, True, new_objects, updates, removals,
                                   xp=player.xp, water=100)

    def end_tick(self) -> None:
        self.pending_removals.clear()
