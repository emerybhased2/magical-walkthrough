"""
Gameplay messages of the archived 2025-12-10 client (Phase 5, tasks 29 and 31).

Every layout here was read from the client's own reader code; line numbers refer to the
readable rebuild (working/client-js/20251210042757_arquivo/client.js). See
phase5/README.md for the full derivation.

  S->C 17  world size + camera   (_0x276a1c.handle, L2956)
  S->C 18  "your animal changed" (dispatcher case 18, L3981)  -> sets MY object id
  S->C 6   alive in game         (case 6, L3892)               -> hides the menu
  S->C 4   world update          (_0x7394b3, L4542):
             header  UInt8 n + n x (UInt8 field, data)          (_0x51aed3, L4423)
                     + if alive: BitGroup, UInt8 0, UInt8 0     (_0x2f5263, _0x33daed, _0xa095b)
             UInt16 n x newly-visible object                     (worldUpd_readMsgNewlyVisible, L8276)
             UInt16 n x (UInt32 id + object update)              (worldUpd_readMsgUpdate, L8316)
             UInt16 n x (UInt32 id + UInt8 hasKiller [+ UInt32 killer])   (L8364)
  C->S 5   cursor world position (Int16 x, Int16 y)          (_0x443ad0, L29035)

Encodings: positions are UInt16 world*4, radii UInt32 world*4 on creation and UInt16 world*10
in updates, creation angles UInt8 degrees/2, update angles UInt16 degrees*182.
"""

from __future__ import annotations

from services.common import PacketReader, PacketWriter

OP_ALIVE = 6
OP_WORLD_UPDATE = 4
OP_GAME_SIZE = 17
OP_YOUR_ANIMAL = 18
OP_INPUT_POS = 5

OTYPE_ANIMAL = 2
# Object types that read an extra UInt16 sub-type right after the type (_0x174e3c, L690).
CUSTOM_SUBTYPE_OTYPES = {2, 14, 117, 128}
INNER_RAD_OTYPES = {5, 9, 13}          # base readCustomData_onNewlyVisible (L8267)

# World-update header fields (_0x51aed3, L4423)
HDR_ZOOM = 1        # UInt16 zoom*1000
HDR_CAMERA = 3      # UInt16 x*4, UInt16 y*4
HDR_XP = 11         # UInt32 current xp (bar text + "+xp" popup)
HDR_WATER = 16      # UInt8 water percent (<= 25 shows LOW WATER)

# Object update property ids (readUpdatedPropertyItem, L8325)
PROP_POS = 1        # UInt16 x*4, UInt16 y*4
PROP_RAD = 2        # UInt16 rad*10
PROP_ANGLE = 3      # UInt16 degrees*182
PROP_HP = 4         # UInt8 percent
PROP_BIOME = 66     # UInt8

ANIMAL_FLAG_BITS = 52   # base (hurt, flying) + 50 animal flags; +1 only when isDevMode (L10594)
BASE_FLAG_BITS = 2


def bit_group(bools: list[bool]) -> bytes:
    """Client BitGroup (_0x3fa214, L1606): 7 data bits per byte in bits 1..7, bit 0 = 'more follows'.

    Bits the client reads past the end are false, so trailing false values are dropped."""
    bools = list(bools)
    while bools and not bools[-1]:
        bools.pop()
    chunks = [bools[i:i + 7] for i in range(0, len(bools), 7)] or [[]]
    out = bytearray()
    for index, chunk in enumerate(chunks):
        byte = 1 if index < len(chunks) - 1 else 0
        for bit, value in enumerate(chunk):
            if value:
                byte |= 1 << (bit + 1)
        out.append(byte)
    return bytes(out)


def _u16(value: float) -> int:
    return max(0, min(0xFFFF, int(round(value))))


def angle_u8(degrees: float) -> int:
    return int(round((degrees % 360) / 2)) % 180


def angle_u16(degrees: float) -> int:
    return _u16((degrees % 360) * 182)


def facing_degrees(dx: float, dy: float) -> float:
    """Wire angle for an animal moving along (dx, dy): the animal class draws at (deg - 90)."""
    import math
    return (math.degrees(math.atan2(dy, dx)) + 90) % 360


# -- S->C messages --------------------------------------------------------------------

def msg_game_size(width: int, height: int, cam_x: float, cam_y: float, zoom: float) -> bytes:
    return (PacketWriter(OP_GAME_SIZE).u16(width).u16(height).u16(_u16(cam_x * 4)).u16(_u16(cam_y * 4))
            .u16(_u16(zoom * 1000)).bytes())


def msg_your_animal(animal_type: int, my_id: int, species: int = 0, sub_species: int = 0, change_kind: int = 2,
                    value: int = 0, ability_w: int = 0, ability_s: int = 0, dangerous_animals=(),
                    edible_animals=(), third_list=(), edible_objects=()) -> bytes:
    """Dispatcher case 18. change_kind: 0 downgrade text, 1 upgrade text, 2 no text."""
    w = (PacketWriter(OP_YOUR_ANIMAL).u8(animal_type).u8(species).u16(sub_species).u8(change_kind).u8(0)
         .u32(my_id).u32(value).u16(ability_w).u16(ability_s))
    for values in (dangerous_animals, edible_animals, third_list, edible_objects):
        values = list(values)
        w.u8(len(values))
        for value_type in values:
            w.u8(value_type)
    return w.bytes()


def msg_alive() -> bytes:
    return bytes([OP_ALIVE])


OP_MINIMAP = 107


def msg_minimap(game_width: int, game_height: int, season: int = 1, groups: list | None = None) -> bytes:
    """Case 107 -> _0xfe88af.create (L2708): UInt8 season (1|2) + loadMiniMap (L2725).

    The client's HUD dereferences the minimap every frame, so it must exist before playing.
    groups: list of lists of shapes {"type", "biome", "x", "y", "r"} (circles only here)."""
    groups = groups or []
    w = (PacketWriter(OP_MINIMAP).u8(season).u16(1).u16(1).u16(game_width).u16(game_height)
         .u16(game_width).u16(game_height).u16(len(groups)))
    for group in groups:
        w.u16(len(group))
        for shape in group:
            w.u16(shape["type"]).u16(shape.get("biome", 0)).u8(0)
            w.u16(int(shape["r"])).u16(int(shape["x"])).u16(int(shape["y"]))
    return w.bytes()


# -- world update ----------------------------------------------------------------------

def encode_new_object(obj) -> bytes:
    """worldUpd_readMsgNewlyVisible for the base class and the animal class."""
    w = PacketWriter().u16(obj.otype)
    if obj.otype in CUSTOM_SUBTYPE_OTYPES:
        w.u16(obj.subtype)
    w.u32(obj.id).u32(int(round(obj.rad * 4))).u16(_u16(obj.x * 4)).u16(_u16(obj.y * 4)).u8(obj.biome)
    if obj.otype != OTYPE_ANIMAL:
        w.u8(0).u16(0).u16(0).u16(0)           # season, animalType, speciesType, speciesSubType
    gets_angle = obj.otype == OTYPE_ANIMAL
    w.buf += bit_group([False, False, gets_angle, False])   # spawnedBy, isRect, getsAngleUpdate, batchDraw
    w.u8(angle_u8(obj.angle))
    w.u8(0).u8(0)                              # specType, specType2 (properties 6 and 7)
    if obj.otype in INNER_RAD_OTYPES:
        w.u16(_u16(obj.rad * 4))
    if obj.otype == OTYPE_ANIMAL:
        w.string_s2c(obj.nick or "")
        w.u16(obj.species).u16(obj.sub_species).u8(0).u8(0).u8(0).u8(0)   # premium skin, theme, season, vip
        w.buf += bit_group([False] * ANIMAL_FLAG_BITS)
        w.u16(0)                               # property 63: wins1v1
        w.u8(0)                                # property 108: teamID
    return w.bytes()


def encode_update(obj, props: dict) -> bytes:
    """worldUpd_readMsgUpdate: flags BitGroup + UInt8 n x (UInt16 property, value)."""
    w = PacketWriter()
    flags = ANIMAL_FLAG_BITS if obj.otype == OTYPE_ANIMAL else BASE_FLAG_BITS
    w.buf += bit_group([bool(getattr(obj, "hurt", False))] + [False] * (flags - 1))
    w.u8(len(props))
    for prop, value in props.items():
        w.u16(prop)
        if prop == PROP_POS:
            w.u16(_u16(value[0] * 4)).u16(_u16(value[1] * 4))
        elif prop == PROP_RAD:
            w.u16(_u16(value * 10))
        elif prop == PROP_ANGLE:
            w.u16(angle_u16(value))
        elif prop in (PROP_HP, PROP_BIOME):
            w.u8(int(value))
        else:
            raise ValueError(f"unsupported property {prop}")
    return w.bytes()


def msg_world_update(camera: tuple[float, float], zoom: float, alive: bool, new_objects: list,
                     updates: list[tuple[object, dict]], removals: list[tuple[int, int | None]],
                     xp: int | None = None, water: int | None = None) -> bytes:
    w = PacketWriter(OP_WORLD_UPDATE)
    fields = [(HDR_ZOOM, lambda: w.u16(_u16(zoom * 1000))),
              (HDR_CAMERA, lambda: w.u16(_u16(camera[0] * 4)).u16(_u16(camera[1] * 4)))]
    if xp is not None:
        fields.append((HDR_XP, lambda: w.u32(max(0, int(xp)))))
    if water is not None:
        fields.append((HDR_WATER, lambda: w.u8(max(0, min(100, int(water))))))
    w.u8(len(fields))
    for field_id, write in fields:
        w.u8(field_id)
        write()
    if alive:
        w.buf += bit_group([])                 # developer flags (_0x2f5263): none
        w.u8(0).u8(0)                          # no tree list (_0x33daed), no id list (_0xa095b)
    w.u16(len(new_objects))
    for obj in new_objects:
        w.buf += encode_new_object(obj)
    w.u16(len(updates))
    for obj, props in updates:
        w.u32(obj.id)
        w.buf += encode_update(obj, props)
    w.u16(len(removals))
    for object_id, killer in removals:
        w.u32(object_id)
        if killer:
            w.u8(1).u32(killer)
        else:
            w.u8(0)
    return w.bytes()


# -- C->S -------------------------------------------------------------------------------

def parse_input_pos(data: bytes) -> tuple[int, int]:
    reader = PacketReader(data)
    if reader.u8() != OP_INPUT_POS:
        raise ValueError("not an input position packet")
    return reader.i16(), reader.i16()


# -- decoders (mirror the client's readers; used by tests, phase5_verify.py and probes) ---

def read_bit_group(reader: PacketReader) -> list[bool]:
    bits: list[bool] = []
    while True:
        byte = reader.u8()
        bits.extend(bool(byte & (1 << (bit + 1))) for bit in range(7))
        if not byte & 1:
            return bits


def decode_your_animal(data: bytes) -> dict:
    r = PacketReader(data)
    if r.u8() != OP_YOUR_ANIMAL:
        raise ValueError("not message 18")
    out = {"animal_type": r.u8(), "species": r.u8(), "sub_species": r.u16(), "change_kind": r.u8()}
    r.u8()
    out.update(my_id=r.u32(), value=r.u32(), ability_w=r.u16(), ability_s=r.u16())
    out["lists"] = [[r.u8() for _ in range(r.u8())] for _ in range(4)]
    return out


def _decode_new_object(r: PacketReader) -> dict:
    obj = {"otype": r.u16()}
    obj["subtype"] = r.u16() if obj["otype"] in CUSTOM_SUBTYPE_OTYPES else 0
    obj.update(id=r.u32(), rad=r.u32() / 4, x=r.u16() / 4, y=r.u16() / 4, biome=r.u8())
    if obj["otype"] != OTYPE_ANIMAL:
        r.u8(), r.u16(), r.u16(), r.u16()
    read_bit_group(r)
    obj["angle"] = r.u8() * 2
    r.u8(), r.u8()
    if obj["otype"] in INNER_RAD_OTYPES:
        r.u16()
    if obj["otype"] == OTYPE_ANIMAL:
        obj["nick"] = r.string_s2c()
        obj["species"], obj["sub_species"] = r.u16(), r.u16()
        r.u8(), r.u8(), r.u8(), r.u8()
        read_bit_group(r)
        r.u16(), r.u8()
    return obj


def decode_world_update(data: bytes, alive: bool = True) -> dict:
    """Decode a message 4 built by msg_world_update (only the fields this server sends)."""
    r = PacketReader(data)
    if r.u8() != OP_WORLD_UPDATE:
        raise ValueError("not message 4")
    header = {}
    for _ in range(r.u8()):
        field_id = r.u8()
        if field_id == HDR_ZOOM:
            header["zoom"] = r.u16() / 1000
        elif field_id == HDR_CAMERA:
            header["camera"] = (r.u16() / 4, r.u16() / 4)
        elif field_id == HDR_XP:
            header["xp"] = r.u32()
        elif field_id == HDR_WATER:
            header["water"] = r.u8()
        else:
            raise ValueError(f"decoder does not know header field {field_id}")
    if alive:
        read_bit_group(r)
        if r.u8() or r.u8():
            raise ValueError("decoder does not read tree/id lists")
    new_objects = [_decode_new_object(r) for _ in range(r.u16())]
    updates = []
    for _ in range(r.u16()):
        object_id = r.u32()
        flags = read_bit_group(r)
        props = {}
        for _ in range(r.u8()):
            prop = r.u16()
            if prop == PROP_POS:
                props[prop] = (r.u16() / 4, r.u16() / 4)
            elif prop == PROP_RAD:
                props[prop] = r.u16() / 10
            elif prop == PROP_ANGLE:
                props[prop] = r.u16() / 182
            elif prop in (PROP_HP, PROP_BIOME):
                props[prop] = r.u8()
            else:
                raise ValueError(f"decoder does not know property {prop}")
        updates.append({"id": object_id, "hurt": bool(flags and flags[0]), "props": props})
    removals = []
    for _ in range(r.u16()):
        object_id = r.u32()
        removals.append((object_id, r.u32() if r.u8() else None))
    if r.remaining:
        raise ValueError(f"{r.remaining} trailing bytes")
    return {"header": header, "new": new_objects, "updates": updates, "removals": removals}


class ClientView:
    """What a client knows after applying S->C 18 and 4: used by tests and the verifier."""

    def __init__(self):
        self.my_id = None
        self.animal_type = None
        self.next_xp = None
        self.animal_messages: list[dict] = []
        self.objects: dict[int, dict] = {}
        self.header: dict = {}
        self.eaten_by: list[tuple[int, int]] = []

    def feed(self, data: bytes) -> None:
        if not data:
            return
        if data[0] == OP_YOUR_ANIMAL:
            message = decode_your_animal(data)
            self.animal_messages.append(message)
            self.my_id, self.animal_type, self.next_xp = message["my_id"], message["animal_type"], message["value"]
        elif data[0] == OP_WORLD_UPDATE:
            update = decode_world_update(data)
            self.header.update(update["header"])
            for obj in update["new"]:
                self.objects[obj["id"]] = obj
            for item in update["updates"]:
                obj = self.objects.get(item["id"])
                if obj is None:
                    raise ValueError(f"update for unknown object {item['id']}")
                if PROP_POS in item["props"]:
                    obj["x"], obj["y"] = item["props"][PROP_POS]
                if PROP_RAD in item["props"]:
                    obj["rad"] = item["props"][PROP_RAD]
                if PROP_ANGLE in item["props"]:
                    obj["angle"] = item["props"][PROP_ANGLE]
            for object_id, killer in update["removals"]:
                if self.objects.pop(object_id, None) is None:
                    raise ValueError(f"removal of unknown object {object_id}")
                if killer is not None:
                    self.eaten_by.append((object_id, killer))

    @property
    def me(self) -> dict | None:
        return self.objects.get(self.my_id) if self.my_id is not None else None

    def nearest(self, otype: int) -> dict | None:
        me = self.me
        if me is None:
            return None
        candidates = [o for o in self.objects.values() if o["otype"] == otype]
        return min(candidates, key=lambda o: (o["x"] - me["x"]) ** 2 + (o["y"] - me["y"]) ** 2, default=None)
