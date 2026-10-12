import math
import struct
import threading
import time

from ..log import describe
from . import tagnames
from .guest import Empty, Unmapped

DRIVERS = {}


def register(name):
    def add(cls):
        cls.key = name
        DRIVERS[name] = cls
        return cls
    return add


class Driver:
    rate = 20.0
    group = ""
    switch = None
    names = None
    caller = None

    def __init__(self, guest, log, name=""):
        self.guest = guest
        self.log = log
        self.name = name
        self.status = "on"
        self._stop = threading.Event()
        self._thread = None

    def begin(self):
        pass

    def tick(self, dt):
        pass

    def end(self):
        pass

    def repick(self):
        pass

    def start(self):
        self.status = self.begin() or "on"
        self._thread = threading.Thread(target=self._run, name="og360-driver", daemon=True)
        self._thread.start()

    def stop(self, put_back=True):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        if put_back:
            self.end()

    def _run(self):
        last = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            dt, last = min(now - last, 0.25), now
            try:
                self.status = self.tick(dt) or "on"
            except Empty as err:
                self.status = "waiting: %s" % err
            except Exception as err:
                self.status = "problem: %s" % describe(err)
                self.log.warn("%s: %s" % (self.name, describe(err)), key=("driver", self.name))
                self._stop.wait(1.0)
            self._stop.wait(max(0.0, 1.0 / self.rate - (time.monotonic() - now)))


DIRECTOR_MODE = 0x4E8C3C
CAMERA_REINIT = 0x4E8C40
CAMERA_CONTROL = 0x4E8C34
CAMERA_PROC = 0x4E8C50
CAMERA_DATA = 0x4E8C54
CAMERA_RESET = (0x4E8D4C, 0x4E8D50)
DEBUG_SUBMODE = 0x51EC3C
DEBUG_SAVED_SET, DEBUG_SAVED = 0x51EC13, 0x51EC14
OBSERVER = 0x4E9C8C
PADS, PAD_SIZE = 0x4E61DC, 0x48
GAME_TIME = 0x510C54
PLAYER_CAMERA, SCRIPTED_CAMERA, DEBUG_CAMERA = 0x23C110, 0x16C840, 0x23D090
A, B, LEFT_TRIGGER, RIGHT_TRIGGER = 0, 1, 6, 7


class Halo2Camera(Driver):
    group = "halo2.camera"

    def _u32(self, va):
        return struct.unpack("<I", self.guest.read(va, 4))[0]

    def _debug_camera_on(self, submode):
        mode = struct.unpack("<i", self.guest.read(DIRECTOR_MODE, 4))[0]
        if mode not in (0, 2):
            raise ValueError("camera is in director mode %d, needs to be 0" % mode)
        self.guest.write(DEBUG_SUBMODE, struct.pack("<h", submode))
        self.guest.write(CAMERA_REINIT, b"\x01")
        self.guest.write(DIRECTOR_MODE, struct.pack("<i", 2))

    def _camera_back(self):
        g = self.guest
        g.write(DIRECTOR_MODE, struct.pack("<i", 0))
        g.write(DEBUG_SUBMODE, struct.pack("<h", 0))
        scripted = g.read(self._u32(CAMERA_CONTROL), 1)[0]
        g.write(CAMERA_PROC, struct.pack("<I", SCRIPTED_CAMERA if scripted else PLAYER_CAMERA))
        g.write(CAMERA_DATA, bytes(8))
        g.write(CAMERA_RESET[0], b"\0")
        g.write(CAMERA_RESET[1], struct.pack("<f", 1.0))


@register("halo2.free_camera")
class Halo2FreeCamera(Halo2Camera):
    speed = 2.5
    back, up = 1.0, 0.3
    turn = 2.2
    pitch_limit = 1.45

    def begin(self):
        g = self.guest
        pos = struct.unpack("<3f", g.read(OBSERVER, 12))
        fwd = struct.unpack("<3f", g.read(OBSERVER + 0x20, 12))
        if not all(map(math.isfinite, pos + fwd)) or not 0.5 < math.hypot(*fwd) < 1.5:
            raise ValueError("no camera yet, is the game loaded?")
        self.yaw = math.atan2(fwd[1], fwd[0])
        self.pitch = math.asin(max(-1.0, min(1.0, fwd[2])))
        self.pos = [pos[0] - self.back * math.cos(self.yaw), pos[1] - self.back * math.sin(self.yaw), pos[2] + self.up]
        self.time = self._u32(GAME_TIME)
        self.speed_before = g.read(self.time + 0xC, 4)
        state = struct.pack("<5f", *self.pos, self.yaw, self.pitch)
        g.write(DEBUG_SAVED, state)
        g.write(DEBUG_SAVED_SET, b"\x01")
        g.write(CAMERA_DATA, state)
        self._debug_camera_on(0)
        g.write(self.time + 0xC, struct.pack("<f", 0.0))
        self.writes = 0

    def tick(self, dt):
        raw = self.guest.read(PADS, 4 * PAD_SIZE)
        pads = [raw[i * PAD_SIZE:(i + 1) * PAD_SIZE] for i in range(4)]
        pad = max(pads, key=lambda p: sum(abs(v) for v in struct.unpack_from("<4h", p, 0x40)) + sum(p[:8]))
        lx, ly, rx, ry = (v / 32767.0 for v in struct.unpack_from("<4h", pad, 0x40))
        boost = 4.0 if pad[A] > 0x20 else 0.25 if pad[B] > 0x20 else 1.0
        rise = (pad[RIGHT_TRIGGER] - pad[LEFT_TRIGGER]) / 255.0
        self.yaw -= rx * self.turn * dt
        self.pitch = max(-self.pitch_limit, min(self.pitch_limit, self.pitch + ry * self.turn * 0.75 * dt))
        cp, sp, cy, sy = math.cos(self.pitch), math.sin(self.pitch), math.cos(self.yaw), math.sin(self.yaw)
        step = self.speed * boost * dt
        self.pos[0] += (cp * cy * ly + sy * lx) * step
        self.pos[1] += (cp * sy * ly - cy * lx) * step
        self.pos[2] += (sp * ly + rise) * step
        self.guest.write(CAMERA_DATA, struct.pack("<5f", *self.pos, self.yaw, self.pitch))
        self.writes += 1
        if self.writes % 20 == 0:
            self.guest.write(DIRECTOR_MODE, struct.pack("<i", 2))
            self.guest.write(self.time + 0xC, struct.pack("<f", 0.0))

    def end(self):
        self.guest.write(self.time + 0xC, self.speed_before)
        self._camera_back()


@register("halo2.third_person")
class Halo2ThirdPerson(Halo2Camera):
    distance = 3.0
    pitch = -0.25

    def begin(self):
        self.chief, self.found = None, 0.0
        self.facing()
        self._debug_camera_on(1)
        self.extra = self.guest.read(CAMERA_DATA + 8, 4)

    def facing(self):
        now = time.monotonic()
        if self.chief is None or now - self.found > 1.0:
            self.chief, self.found = self.guest.resolve("chief"), now
        fwd = struct.unpack("<3f", self.guest.read(self.chief + 0x70, 12))
        return math.atan2(fwd[1], fwd[0])

    def tick(self, dt):
        yaw = self.facing()
        self.guest.write(CAMERA_DATA + 4, struct.pack("<f", self.distance) + self.extra +
                         struct.pack("<2f", yaw, self.pitch))

    def end(self):
        self._camera_back()


OBJECTS = 0x4E0300
PLAYERS = 0x4E8C24
ACTORS, ACTOR_SIZE = 0x4F55F0, 0x888
AI_TEAMS = 0x4F55EC
PLAYER_CONTROL = 0x4ED284
TAG_TABLE = 0x4E3B44
MAP_HEADER = 0x547700
NONE = 0xFFFFFFFF
POSITION, SCALE, DAMAGE_FLAGS, ACTOR, TEAM, PLAYER, LAST_PLAYER, AIM = 0x64, 0xA0, 0x10A, 0x12C, 0x138, 0x13C, 0x140, 0x150
DEAD = 0x04
ACTOR_THINKS = 0x84
ACTOR_TEAM = 0x24
KNOCKBACK = 0x40


def _u32(g, va):
    return struct.unpack("<I", g.read(va, 4))[0]


def _fresh_text(g, at):
    if not at:
        return ""
    for attempt in (0, 1):
        try:
            return g.read(at, min(48, 0x1000 - (at & 0xFFF))).split(b"\0")[0].decode("latin-1")
        except Unmapped:
            if attempt:
                raise
            g.refresh_tables(force=True)


def halo2_level(g):
    try:
        return tagnames.map_header(g.read, MAP_HEADER)
    except ValueError:
        raise Empty("level is loading")


def halo2_bipeds(g):
    array = _u32(g, OBJECTS)
    head = g.read(array, 0x48)
    most = struct.unpack_from("<H", head, 0x20)[0]
    used = struct.unpack_from("<i", head, 0x38)[0]
    count = used if 0 < used <= most else most
    raw = g.read(struct.unpack_from("<I", head, 0x44)[0], count * 12)
    out = {}
    for k in range(count):
        salt = struct.unpack_from("<H", raw, k * 12)[0]
        addr = struct.unpack_from("<I", raw, k * 12 + 8)[0]
        if salt and addr and raw[k * 12 + 3] == 0:
            out[(salt << 16) | k] = addr
    return out


def halo2_snapshots(g, bipeds):
    items = list(bipeds.items())
    raws = g.read_small([(addr, 0x160) for _, addr in items])
    return {h: (addr, raw) for (h, addr), raw in zip(items, raws) if raw is not None}


def halo2_player(g):
    entry = _u32(g, _u32(g, PLAYERS) + 0x44)
    return entry, struct.unpack("<H", g.read(entry, 2))[0] << 16


def halo2_allies(g, team):
    if not 0 <= team < 16:
        return 0
    return struct.unpack("<H", g.read(_u32(g, AI_TEAMS) + 0xC4 + team * 2, 2))[0]


def _team(raw):
    return struct.unpack_from("<h", raw, TEAM)[0]


def _is_ally(allies, team):
    return 0 <= team < 16 and bool(allies >> team & 1)


@register("halo2.play_as")
class Halo2PlayAs(Driver):
    rate = 4.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state = None
        self._again = False

    @classmethod
    def choices(cls, g, names):
        snaps = halo2_snapshots(g, halo2_bipeds(g))
        entry, _ = halo2_player(g)
        me = _u32(g, entry + 0x2C)
        here = struct.unpack_from("<3f", snaps[me][1], POSITION) if me in snaps else None
        allies = halo2_allies(g, _team(snaps[me][1]) if me in snaps else 1)
        out = []
        for handle, (addr, raw) in snaps.items():
            if handle == me or raw[DAMAGE_FLAGS] & DEAD or struct.unpack_from("<I", raw, ACTOR)[0] == NONE:
                continue
            tag = struct.unpack_from("<I", raw, 0)[0] & 0xFFFF
            kind = (names[tag].rsplit("\\", 1)[-1] if tag < len(names) and names[tag] else "someone").replace("_", " ")
            metres = math.dist(here, struct.unpack_from("<3f", raw, POSITION)) * 3.048 if here else 0.0
            side = "ally" if _is_ally(allies, _team(raw)) else "enemy"
            out.append({"datum": handle, "path": "", "name": "%s (%s, %d m)" % (kind.capitalize(), side, round(metres)),
                        "metres": metres})
        out.sort(key=lambda c: c["metres"])
        return out

    def begin(self):
        self._take(self.switch.get("picked") if self.switch else None)
        return "on: youre the %s" % self.state["name"]

    def repick(self):
        self._again = True

    def tick(self, dt):
        if self._again:
            self._again = False
            self._release()
            self._take(self.switch.get("picked") if self.switch else None)
        if not self.state:
            return "waiting: pick someone in the list"
        entry, _ = halo2_player(self.guest)
        if _u32(self.guest, entry + 0x2C) != self.state["target"]:
            name = self.state["name"]
            self._release()
            return "waiting: youre back to yourself (the %s died or the game reloaded)" % name
        return "on: youre the %s" % self.state["name"]

    def end(self):
        self._release()

    def _take(self, wanted):
        g = self.guest
        options = self.choices(g, self.names() if self.names else [])
        choice = next((c for c in options if c["datum"] == wanted), None) or (options[0] if options else None)
        if choice is None:
            raise ValueError("nobody here to play as")
        bipeds = halo2_bipeds(g)
        entry, player = halo2_player(g)
        own, target = _u32(g, entry + 0x2C), choice["datum"]
        if own not in bipeds:
            raise Empty("you dont have a body right now")
        own_at, target_at = bipeds[own], bipeds[target]
        actor = _u32(g, target_at + ACTOR)
        controller = struct.unpack("<h", g.read(entry + 0x28, 2))[0]
        if controller < 0:
            raise ValueError("player 1 has no controller")
        aim = struct.unpack("<3f", g.read(target_at + AIM, 12))
        flags = g.read(own_at + DAMAGE_FLAGS, 2)
        guard = bytes([flags[0] | 0x80, flags[1] | 0x40])
        base = {"actor": _u32(g, _u32(g, ACTORS) + 0x44) + (actor & 0xFFFF) * ACTOR_SIZE, "own": own_at,
                "target": target_at, "control": _u32(g, PLAYER_CONTROL) + 0x14 + controller * 0x94, "player": entry}
        writes = [("actor", ACTOR_THINKS, struct.pack("<h", 0)),
                  ("own", DAMAGE_FLAGS, guard),
                  ("target", PLAYER, struct.pack("<I", player)),
                  ("target", LAST_PLAYER, struct.pack("<I", NONE)),
                  ("own", PLAYER, struct.pack("<I", NONE)),
                  ("own", LAST_PLAYER, struct.pack("<I", player)),
                  ("control", 0x10, struct.pack("<2f", math.atan2(aim[1], aim[0]) % (2 * math.pi),
                                                math.atan2(aim[2], math.hypot(aim[0], aim[1])))),
                  ("control", 0, struct.pack("<I", target)),
                  ("player", 0x30, struct.pack("<I", NONE)),
                  ("player", 0x2C, struct.pack("<I", target))]
        before = [(kind, off, g.read(base[kind] + off, len(data))) for kind, off, data in writes]
        self.state = {"target": target, "own": own, "actor": actor, "name": choice["name"].split(" (")[0].lower(),
                      "guarded": bytes(a ^ b for a, b in zip(flags, guard)), "before": before}
        for kind, off, data in writes:
            g.write(base[kind] + off, data)
        self.log.note("%s: youre the %s" % (self.name, self.state["name"]))

    def _release(self):
        st, self.state = self.state, None
        if not st:
            return
        g = self.guest
        bipeds = halo2_bipeds(g)
        entry, _ = halo2_player(g)
        own_at, target_at = bipeds.get(st["own"]), bipeds.get(st["target"])
        still = _u32(g, entry + 0x2C) == st["target"]
        controller = struct.unpack("<h", g.read(entry + 0x28, 2))[0]
        base = {"own": own_at, "target": target_at, "player": entry,
                "control": _u32(g, PLAYER_CONTROL) + 0x14 + controller * 0x94 if controller >= 0 else None,
                "actor": _u32(g, _u32(g, ACTORS) + 0x44) + (st["actor"] & 0xFFFF) * ACTOR_SIZE}
        for kind, off, old in reversed(st["before"]):
            at = base.get(kind)
            if at is None:
                continue
            if kind == "own" and off == DAMAGE_FLAGS:
                now = g.read(at + off, 2)
                g.write(at + off, bytes(n & ~m & 0xFF for n, m in zip(now, st["guarded"])))
            elif kind == "actor":
                if target_at is not None and _u32(g, target_at + ACTOR) == st["actor"]:
                    g.write(at + off, old)
            elif still:
                g.write(at + off, old)
        self.log.note("%s: back to yourself" % self.name)


@register("halo2.force_push")
class Halo2ForcePush(Driver):
    rate = 1.0
    factor = 20.0

    def begin(self):
        self.level, self.pushed = None, {}
        return self.tick(0)

    def tick(self, dt):
        g = self.guest
        level = halo2_level(g)
        sample = list(self.pushed.items())[:4]
        if level != self.level or (sample and all(g.read(at, 4) == old for at, (old, _) in sample)):
            self.level, self.pushed = level, {}
            effects = tagnames.loaded_tags(g.read, _u32(g, TAG_TABLE), ("jpt!",))
            spots = [addr + KNOCKBACK for _, _, addr in effects]
            for at, raw in zip(spots, g.read_small([(at, 4) for at in spots])):
                value = struct.unpack("<f", raw)[0] if raw else 0.0
                if value > 0 and math.isfinite(value):
                    new = struct.pack("<f", value * self.factor)
                    g.write(at, new)
                    self.pushed[at] = (raw, new)
        return "on: %d damage effects" % len(self.pushed)

    def end(self):
        g = self.guest
        spots = list(self.pushed.items())
        for (at, (old, new)), now in zip(spots, g.read_small([(at, 4) for at, _ in spots])):
            if now == new:
                g.write(at, old)


class _Halo2Enemies(Driver):
    rate = 2.0

    def begin(self):
        self.level, self.changed = None, {}
        return self.tick(0)

    def _scan(self):
        g = self.guest
        level = halo2_level(g)
        if level != self.level:
            self.level, self.changed = level, {}
        snaps = halo2_snapshots(g, halo2_bipeds(g))
        entry, _ = halo2_player(g)
        me = _u32(g, entry + 0x2C)
        allies = halo2_allies(g, _team(snaps[me][1]) if me in snaps else 1)
        return snaps, me, allies

    def _put_back(self, offset, mine):
        g = self.guest
        snaps = halo2_snapshots(g, halo2_bipeds(g))
        for handle, old in self.changed.items():
            if handle in snaps:
                addr, raw = snaps[handle]
                if raw[offset:offset + len(old)] == mine:
                    g.write(addr + offset, old)


@register("halo2.giant_enemies")
class Halo2GiantEnemies(_Halo2Enemies):
    scale = 2.0

    def tick(self, dt):
        snaps, me, allies = self._scan()
        big = struct.pack("<f", self.scale)
        for handle, (addr, raw) in snaps.items():
            now = raw[SCALE:SCALE + 4]
            if handle == me:
                if handle in self.changed and now == big:
                    self.guest.write(addr + SCALE, self.changed.pop(handle))
            elif not _is_ally(allies, _team(raw)) and not raw[DAMAGE_FLAGS] & DEAD and now != big:
                self.changed.setdefault(handle, now)
                self.guest.write(addr + SCALE, big)
        return "on: %d enemies" % len(self.changed)

    def end(self):
        self._put_back(SCALE, struct.pack("<f", self.scale))


@register("halo2.civil_war")
class Halo2CivilWar(_Halo2Enemies):
    def begin(self):
        self.team = None
        self.seen = set()
        return super().begin()

    def _outcast(self, snaps):
        rows = struct.unpack("<16H", self.guest.read(_u32(self.guest, AI_TEAMS) + 0xC4, 32))
        used = {_team(raw) for _, raw in snaps.values()}
        for team in (6, 8, 9, 10, 11, 12, 13, 14, 15):
            alone = rows[team] == 1 << team and not any(rows[t] >> team & 1 for t in range(16) if t != team)
            if alone and team not in used:
                return team
        raise ValueError("no free team for a war")

    def _actor_team(self, actor):
        return _u32(self.guest, _u32(self.guest, ACTORS) + 0x44) + (actor & 0xFFFF) * ACTOR_SIZE + ACTOR_TEAM

    def tick(self, dt):
        level = self.level
        snaps, me, allies = self._scan()
        if self.level != level or self.team is None:
            self.team, self.seen = self._outcast(snaps), set()
        outcast = struct.pack("<h", self.team)
        for handle in sorted(snaps):
            addr, raw = snaps[handle]
            actor = struct.unpack_from("<I", raw, ACTOR)[0]
            if handle in self.changed:
                was, old = self.changed[handle]
                if actor == was:
                    at = self._actor_team(actor)
                    if self.guest.read(at, 2) == old:
                        self.guest.write(at, outcast)
                continue
            if (handle in self.seen or handle == me or raw[DAMAGE_FLAGS] & DEAD or actor == NONE
                    or _is_ally(allies, _team(raw))):
                continue
            self.seen.add(handle)
            if len(self.seen) % 2 == 0:
                at = self._actor_team(actor)
                self.changed[handle] = (actor, self.guest.read(at, 2))
                self.guest.write(at, outcast)
        return "on: %d enemies switched sides (team %d)" % (len(self.changed), self.team)

    def end(self):
        if self.team is None:
            return
        outcast = struct.pack("<h", self.team)
        snaps = halo2_snapshots(self.guest, halo2_bipeds(self.guest))
        for handle, (actor, old) in self.changed.items():
            if handle in snaps and struct.unpack_from("<I", snaps[handle][1], ACTOR)[0] == actor:
                at = self._actor_team(actor)
                if self.guest.read(at, 2) == outcast:
                    self.guest.write(at, old)


HCE_DIRECTOR_MODE = 0x2710F4
HCE_INIT_CAMERA = 0x2710F6
HCE_DEBUG_CONTROLS = 0x2711B8
HCE_SAVED_SET, HCE_SAVED_POSE = 0x2F9146, 0x2F9148
HCE_OBSERVER = 0x2714E0


@register("halo1.free_camera")
class HaloFreeCamera(Driver):
    rate = 4.0

    def begin(self):
        g = self.guest
        pos = struct.unpack("<3f", g.read(HCE_OBSERVER + 8, 12))
        fwd = struct.unpack("<3f", g.read(HCE_OBSERVER + 0x28, 12))
        if not all(map(math.isfinite, pos + fwd)) or not 0.5 < math.hypot(*fwd) < 1.5:
            raise ValueError("no camera yet, is the game loaded?")
        mode = struct.unpack("<h", g.read(HCE_DIRECTOR_MODE, 2))[0]
        if mode not in (0, 2):
            raise ValueError("camera is in director mode %d, needs to be 0" % mode)
        yaw, pitch = math.atan2(fwd[1], fwd[0]), math.asin(max(-1.0, min(1.0, fwd[2])))
        self.before = [(a, g.read(a, n)) for a, n in ((HCE_SAVED_SET, 1), (HCE_SAVED_POSE, 20))]
        g.write(HCE_SAVED_POSE, struct.pack("<5f", *pos, yaw, pitch))
        g.write(HCE_SAVED_SET, b"\x01")
        g.write(HCE_DIRECTOR_MODE, struct.pack("<h", 2))
        g.write(HCE_DEBUG_CONTROLS, b"\x01")

    def tick(self, dt):
        g = self.guest
        g.write(HCE_DEBUG_CONTROLS, b"\x01")
        g.write(HCE_DIRECTOR_MODE, struct.pack("<h", 2))

    def end(self):
        g = self.guest
        g.write(HCE_DIRECTOR_MODE, struct.pack("<h", 0))
        g.write(HCE_INIT_CAMERA, b"\x01")
        g.write(HCE_DEBUG_CONTROLS, b"\x00")
        for address, data in self.before:
            g.write(address, data)


TC_NICK = 0x2A2DB0
TC_CAMERA = 0x2A2DAC
TC_SPARE_CAMERA = 0x2A2E80
TC_PAD_INDEX = 0x290B18
TC_PADS, TC_PAD_SIZE = 0x2C7890, 0x7C
TC_NO_CONTROL = 0xDD4
TC_ENTITY_LIST = 0x2AD980
TC_CHARACTER, TC_VEHICLE = 1, 2
TC_RAM = (0x10000, 0x4000000)
TC_THUNK = bytes.fromhex("8b44240c8b4c240850518b4c240c")
TC_SKIP_UPDATE = bytes.fromhex("837c24080075e531c0c3")


@register("truecrime.free_camera")
class TrueCrimeFreeCamera(Driver):
    speed = 8.0
    turn = 2.0
    pitch_limit = 1.45
    dead_zone = 0.25

    def begin(self):
        g = self.guest
        cam, nick = _u32(g, TC_CAMERA), _u32(g, TC_NICK)
        if cam in (0, TC_SPARE_CAMERA) or not nick:
            raise ValueError("no game camera yet, is a game loaded?")
        rows = struct.unpack("<16f", g.read(cam + 0x10, 0x40))
        fwd = rows[8:11]
        if not all(map(math.isfinite, rows)) or not 0.5 < math.hypot(*fwd) < 1.5:
            raise ValueError("camera has no position yet")
        self.yaw = math.atan2(fwd[0], fwd[2])
        self.pitch = math.asin(max(-1.0, min(1.0, fwd[1])))
        self.pos = list(rows[12:15])
        self.game_camera, self.nick = cam, nick
        self.spare_before = [(a, g.read(a, n)) for a, n in ((TC_SPARE_CAMERA + 0x10, 0x40), (TC_SPARE_CAMERA + 0xB0, 4),
                                                            (TC_SPARE_CAMERA + 0x198, 8))]
        self.control_before = g.read(nick + TC_NO_CONTROL, 1)
        g.write(TC_SPARE_CAMERA + 0x198, g.read(cam + 0x198, 8))
        g.write(TC_SPARE_CAMERA + 0xB0, struct.pack("<I", nick))
        self._place()
        self.pad = TC_PADS
        self._keep()
        self.writes = 0

    def _place(self):
        cp, sp, cy, sy = math.cos(self.pitch), math.sin(self.pitch), math.cos(self.yaw), math.sin(self.yaw)
        right, up, fwd = (cy, 0.0, -sy), (-sp * sy, cp, -sp * cy), (cp * sy, sp, cp * cy)
        self.guest.write(TC_SPARE_CAMERA + 0x10, struct.pack("<16f", *right, 0.0, *up, 0.0, *fwd, 0.0, *self.pos, 1.0))

    def _keep(self):
        g = self.guest
        cam = _u32(g, TC_CAMERA)
        if cam != TC_SPARE_CAMERA:
            if cam:
                self.game_camera = cam
            g.write(TC_CAMERA, struct.pack("<I", TC_SPARE_CAMERA))
        if _u32(g, TC_NICK) == self.nick:
            g.write(self.nick + TC_NO_CONTROL, b"\x01")
        port = struct.unpack("<i", g.read(TC_PAD_INDEX, 4))[0]
        self.pad = TC_PADS + max(0, min(3, port)) * TC_PAD_SIZE

    def _axis(self, raw):
        v = max(-1.0, min(1.0, raw / 127.0))
        return 0.0 if abs(v) < self.dead_zone else math.copysign((abs(v) - self.dead_zone) / (1.0 - self.dead_zone), v)

    def tick(self, dt):
        pad = self.guest.read(self.pad + 0x6C, 0x10)
        rx, ry, lx, ly = struct.unpack_from("<4b", pad, 4)
        look_x, look_y, move_x, move_y = self._axis(rx), self._axis(-1 - ry), self._axis(lx), self._axis(-1 - ly)
        analog = pad[8:]
        boost = 4.0 if analog[0] > 0x20 else 0.25 if analog[1] > 0x20 else 1.0
        rise = (analog[7] - analog[6]) / 255.0
        self.yaw += look_x * self.turn * dt
        self.pitch = max(-self.pitch_limit, min(self.pitch_limit, self.pitch + look_y * self.turn * 0.75 * dt))
        cp, sp, cy, sy = math.cos(self.pitch), math.sin(self.pitch), math.cos(self.yaw), math.sin(self.yaw)
        step = self.speed * boost * dt
        self.pos[0] += (cp * sy * move_y + cy * move_x) * step
        self.pos[1] += (sp * move_y + rise) * step
        self.pos[2] += (cp * cy * move_y - sy * move_x) * step
        self._place()
        self.writes += 1
        if self.writes % 10 == 0:
            self._keep()

    def _usable(self, cam):
        if not cam or cam == TC_SPARE_CAMERA:
            return 0
        try:
            head = self.guest.read(cam, 12)
            handler = struct.unpack_from("<I", head)[0]
            return cam if head[8] == 10 and handler and self.guest.read(handler, len(TC_THUNK)) == TC_THUNK else 0
        except Unmapped:
            return 0

    def end(self):
        g = self.guest
        if _u32(g, TC_CAMERA) == TC_SPARE_CAMERA:
            nick = _u32(g, TC_NICK)
            back = self._usable(self.game_camera) or (nick and self._usable(_u32(g, nick + 0xE18)))
            if back:
                g.write(TC_CAMERA, struct.pack("<I", back))
        for address, data in self.spare_before:
            g.write(address, data)
        if _u32(g, TC_NICK) == self.nick:
            g.write(self.nick + TC_NO_CONTROL, self.control_before)


@register("truecrime.freeze_ai")
class TrueCrimeFreezeAI(Driver):
    rate = 1.0
    reread = 15

    def begin(self):
        if not _u32(self.guest, TC_NICK):
            raise ValueError("no game running (nick isnt there)")
        self.thunks = {}
        self.seen = {}
        self.pool = None
        self.passes = 0
        return self.tick(0)

    def _entities(self):
        g = self.guest
        lo, blob = 0, b""
        if self.pool:
            try:
                lo, blob = self.pool[0], g.read(*self.pool)
            except Unmapped:
                self.pool = None
        node, out, nodes = _u32(g, TC_ENTITY_LIST), [], set()
        while node and node not in nodes and len(out) < 2000:
            if lo <= node and node + 12 <= lo + len(blob):
                nxt, _, ent = struct.unpack_from("<3I", blob, node - lo)
            else:
                try:
                    nxt, _, ent = struct.unpack("<3I", g.read(node, 12))
                except Unmapped:
                    break
            if not nxt:
                break
            nodes.add(node)
            if TC_RAM[0] <= ent < TC_RAM[1]:
                out.append(ent)
            node = nxt
        if nodes:
            a, b = min(nodes) & ~0xF, (max(nodes) + 12 + 0xF) & ~0xF
            self.pool = (a, b - a) if b - a <= 0x2000 else None
        return out

    def _head(self, ent):
        try:
            head = self.guest.read(ent, 12)
        except Unmapped:
            return None
        return list(struct.unpack_from("<2I", head)) + [head[8]]

    def _look(self, thunk):
        if thunk not in self.thunks:
            try:
                code = self.guest.read(thunk, 0x20)
            except Unmapped:
                code = b""
            state = None
            if thunk % 0x10 == 0 and code[:14] == TC_THUNK and code[14] == 0xE8 and code[19] == 0xC3:
                state = True if code[20:].startswith(TC_SKIP_UPDATE) else False if code[20:] == b"\xcc" * 12 else None
            self.thunks[thunk] = state
        return self.thunks[thunk]

    def _skip_ready(self, thunk):
        if self._look(thunk) is False:
            self.guest.write(thunk + 0x14, TC_SKIP_UPDATE)
            self.thunks[thunk] = True
        return self.thunks[thunk] is True

    def _thunk(self, handler):
        return handler - 0x14 if handler % 0x10 == 4 and self._look(handler - 0x14) is True else handler

    def tick(self, dt):
        g = self.guest
        self.passes += 1
        if self.passes % self.reread == 0:
            self.seen.clear()
        mine, node = set(), _u32(g, TC_NICK)
        while node and len(mine) < 6:
            mine.add(node)
            node = _u32(g, node + 0x58)
        frozen, fresh = 0, set()
        for ent in self._entities():
            if ent not in self.seen:
                self.seen[ent] = self._head(ent)
                fresh.add(ent)
            if self.seen[ent] is None:
                continue
            handler, flags, kind = self.seen[ent]
            thunk = self._thunk(handler)
            wanted = thunk if ent in mine else thunk + 0x14
            if ent not in mine and (kind not in (TC_CHARACTER, TC_VEHICLE) or flags & 0x80010000 != 0x10000):
                continue
            if handler != wanted and (ent in mine or self._skip_ready(thunk)):
                if ent not in fresh and self._head(ent) != self.seen[ent]:
                    del self.seen[ent]
                    continue
                g.write(ent, struct.pack("<I", wanted))
                self.seen[ent][0] = handler = wanted
            frozen += handler != thunk
        return "on: %d frozen" % frozen

    def end(self):
        g = self.guest
        for ent in self._entities():
            head = self._head(ent)
            if head and self._thunk(head[0]) != head[0]:
                g.write(ent, struct.pack("<I", head[0] - 0x14))


TC_GUNS = (0x270, 0x490, 0x908, 0x9F8)


@register("truecrime.infinite_ammo")
class TrueCrimeInfiniteAmmo(Driver):
    rate = 4.0

    def tick(self, dt):
        g = self.guest
        nick = _u32(g, TC_NICK)
        if not nick:
            raise Empty("nick isnt there")
        first = TC_GUNS[0]
        data = g.read(nick + first, TC_GUNS[-1] + 8 - first)
        for at in TC_GUNS:
            clip, rounds = struct.unpack_from("<ii", data, at - first)
            full = max(clip, min(2 * clip, 32))
            if 0 < clip <= 999 and rounds < full:
                g.write(nick + at + 4, struct.pack("<i", full))


MAFIA_GAME = 0x31FC5C
MAFIA_CAMERA_CONTROL = 0x70
MAFIA_HARD_LOCK = 0xA5
MAFIA_PLAYER = 0x150
MAFIA_LOCK_CONTROLS = 0xC6A
MAFIA_PADS, MAFIA_PAD_SIZE = 0x3CCF40, 0x16
MAFIA_PAD_PORT = 0x3CD160


@register("mafia.free_camera")
class MafiaFreeCamera(Driver):
    speed = 10.0
    turn = 2.0
    pitch_limit = 1.45
    dead_zone = 0.25
    eye_height = 1.6
    teleported = False

    def _scene(self):
        game = _u32(self.guest, MAFIA_GAME)
        scene = game and _u32(self.guest, game + 0x20)
        if not scene:
            raise Empty("no game loaded")
        return scene

    def begin(self):
        g = self.guest
        scene = self._scene()
        self.control = scene + MAFIA_CAMERA_CONTROL
        frame = _u32(g, self.control + 4)
        if not frame:
            raise ValueError("no camera yet, is a mission loaded?")
        rows = struct.unpack("<16f", g.read(frame + 0x40, 0x40))
        fwd = rows[8:11]
        if not all(map(math.isfinite, rows)) or not 0.5 < math.hypot(*fwd) < 1.5:
            raise ValueError("camera has no position yet")
        self.yaw = math.atan2(fwd[0], fwd[2])
        self.pitch = math.asin(max(-1.0, min(1.0, fwd[1] / math.hypot(*fwd))))
        self.pos = list(rows[12:15])
        self.before = g.read(self.control + MAFIA_HARD_LOCK, 1), g.read(self.control + 0xB0, 0x20)
        self.player = _u32(g, scene + MAFIA_PLAYER)
        self.control_before = g.read(self.player + MAFIA_LOCK_CONTROLS, 1) if self.player else None
        self._place()
        self._keep()
        self.writes = 0

    def _place(self):
        cp, sp, cy, sy = math.cos(self.pitch), math.sin(self.pitch), math.cos(self.yaw), math.sin(self.yaw)
        self.guest.write(self.control + 0xB0, struct.pack("<8f", *self.pos, 1.0, cp * sy, sp, cp * cy, 0.0))

    def _keep(self):
        g = self.guest
        g.write(self.control + MAFIA_HARD_LOCK, b"\x01")
        if self.player and _u32(g, self._scene() + MAFIA_PLAYER) == self.player:
            g.write(self.player + MAFIA_LOCK_CONTROLS, b"\x01")
        port = struct.unpack("<i", g.read(MAFIA_PAD_PORT, 4))[0]
        self.pad = MAFIA_PADS + max(0, min(3, port)) * MAFIA_PAD_SIZE

    def _axis(self, raw):
        v = max(-1.0, min(1.0, raw / 32767.0))
        return 0.0 if abs(v) < self.dead_zone else math.copysign((abs(v) - self.dead_zone) / (1.0 - self.dead_zone), v)

    def tick(self, dt):
        pad = self.guest.read(self.pad, MAFIA_PAD_SIZE)
        analog = pad[6:14]
        lx, ly, rx, ry = struct.unpack_from("<4h", pad, 0xE)
        look_x, look_y, move_x, move_y = self._axis(rx), self._axis(ry), self._axis(lx), self._axis(ly)
        boost = 4.0 if analog[0] > 0x20 else 0.25 if analog[1] > 0x20 else 1.0
        rise = (analog[7] - analog[6]) / 255.0
        self.yaw += look_x * self.turn * dt
        self.pitch = max(-self.pitch_limit, min(self.pitch_limit, self.pitch + look_y * self.turn * 0.75 * dt))
        cp, sp, cy, sy = math.cos(self.pitch), math.sin(self.pitch), math.cos(self.yaw), math.sin(self.yaw)
        step = self.speed * boost * dt
        self.pos[0] += (cp * sy * move_y + cy * move_x) * step
        self.pos[1] += (sp * move_y + rise) * step
        self.pos[2] += (cp * cy * move_y - sy * move_x) * step
        self._place()
        self.writes += 1
        if self.writes % 10 == 0:
            self._keep()
        x_down = analog[2] > 0x20
        if x_down and not self.teleported:
            self._teleport()
        self.teleported = x_down
        return "on: press X to put tommy here"

    def _teleport(self):
        g = self.guest
        if not self.player or _u32(g, self._scene() + MAFIA_PLAYER) != self.player or _u32(g, self.player + 0x234):
            return
        frame = _u32(g, self.player + 0x80)
        if not frame:
            return
        x, y, z = self.pos[0], self.pos[1] - self.eye_height, self.pos[2]
        g.write(self.player + 0x10, struct.pack("<3f", x, y, z))
        g.write(frame + 0xB0, struct.pack("<3f", x, y, z))
        g.write(frame + 0xE0, struct.pack("<I", (_u32(g, frame + 0xE0) & 0xFFFFFEDF) | 0x40000000))

    def end(self):
        g = self.guest
        lock, kept = self.before
        g.write(self.control + 0xB0, kept)
        g.write(self.control + MAFIA_HARD_LOCK, lock)
        try:
            if self.player and self.control_before is not None and _u32(g, self._scene() + MAFIA_PLAYER) == self.player:
                g.write(self.player + MAFIA_LOCK_CONTROLS, self.control_before)
        except Empty:
            pass


MAFIA_MODEL_SWAP = 0x00069940
MAFIA_MODELS = (
    ('Tommy', 'Tommy'),
    ('Paulie', 'Paulie'),
    ('PaulieCOATHAT', 'Paulie in a coat and hat'),
    ('PaulieCorpse', 'Paulie (dead)'),
    ('Sam', 'Sam'),
    ('SamCOATHAT', 'Sam in a coat and hat'),
    ('Samblood1', 'Sam (bloodied)'),
    ('Salieri2', 'Don Salieri'),
    ('SalieriLOW', 'Don Salieri (low detail)'),
    ('MorelloLOW', 'Don Morello'),
    ('frank', 'Frank'),
    ('VincenzoLOW', 'Vincenzo'),
    ('Ralph', 'Ralph'),
    ('Luigi', 'Luigi'),
    ('Lucas', 'Lucas Bertone'),
    ('lucasvr', 'Lucas Bertone (2)'),
    ('Sergio', 'Sergio Morello'),
    ('SergioBLOOD', 'Sergio Morello (bloodied)'),
    ('Sara', 'Sarah'),
    ('Sarah2LOW', 'Sarah (low detail)'),
    ('hen', 'Henry'),
    ('henCREATURE', 'Henry (henCREATURE)'),
    ('Joe', 'Joe'),
    ('Biff', 'Biff'),
    ('Tony', 'Tony'),
    ('Carlo', 'Carlo'),
    ('David', 'David'),
    ('Emeth', 'Emeth'),
    ('Vrabec', 'Vrabec'),
    ('Alice1', 'Alice'),
    ('Berta', 'Berta'),
    ('Kasar', 'Kasar'),
    ('TommyHAT', 'Tommy in a hat'),
    ('TommyCOATHAT', 'Tommy in a coat and hat'),
    ('TommyRACER', 'Tommy the racer'),
    ('TommyRACER2', 'Tommy the racer (2)'),
    ('TommySAILOR', 'Tommy the sailor'),
    ('TommyTAXIDRIVER', 'Tommy the taxi driver'),
    ('TommyDELNIK', 'Tommy the worker'),
    ('TommyFREERIDER', 'Tommy (Free Ride)'),
    ('TommyPYTEL', 'Tommy with a sack'),
    ('AsisPZ1', 'Assistant (AsisPZ1)'),
    ('Bclerk01', 'Bank clerk (Bclerk01)'),
    ('Bclerk02', 'Bank clerk (Bclerk02)'),
    ('BnkO01', 'Bank officer (BnkO01)'),
    ('BnkO02', 'Bank officer (BnkO02)'),
    ('BnkO03', 'Bank officer (BnkO03)'),
    ('Barman01', 'Barman (Barman01)'),
    ('BobAut01', 'Bob (BobAut01)'),
    ('Bguard01', 'Bodyguard (Bguard01)'),
    ('Bguard01M', 'Bodyguard (Bguard01M)'),
    ('Bguard02', 'Bodyguard (Bguard02)'),
    ('Bguard03', 'Bodyguard (Bguard03)'),
    ('Bguard03M', 'Bodyguard (Bguard03M)'),
    ('Bookmaker01', 'Bookmaker (Bookmaker01)'),
    ('Bookmaker02', 'Bookmaker (Bookmaker02)'),
    ('Boxer01', 'Boxer (Boxer01)'),
    ('Boxer02', 'Boxer (Boxer02)'),
    ('CarlZen1', "Carlo's wife (CarlZen1)"),
    ('China1', 'Chinese man (China1)'),
    ('civil02', 'Civilian (civil02)'),
    ('civil03', 'Civilian (civil03)'),
    ('civil04', 'Civilian (civil04)'),
    ('civil05', 'Civilian (civil05)'),
    ('civil06', 'Civilian (civil06)'),
    ('civil11', 'Civilian (civil11)'),
    ('civil11M', 'Civilian (civil11M)'),
    ('civil12', 'Civilian (civil12)'),
    ('civil13', 'Civilian (civil13)'),
    ('civil13bez', 'Civilian (civil13bez)'),
    ('civil14', 'Civilian (civil14)'),
    ('civil15', 'Civilian (civil15)'),
    ('civil16', 'Civilian (civil16)'),
    ('civil16M', 'Civilian (civil16M)'),
    ('civil17', 'Civilian (civil17)'),
    ('civil18', 'Civilian (civil18)'),
    ('civil19', 'Civilian (civil19)'),
    ('civil19M', 'Civilian (civil19M)'),
    ('civil21', 'Civilian (civil21)'),
    ('civil21N', 'Civilian (civil21N)'),
    ('civil22', 'Civilian (civil22)'),
    ('civil31', 'Civilian (civil31)'),
    ('civil32', 'Civilian (civil32)'),
    ('civil33', 'Civilian (civil33)'),
    ('civil34', 'Civilian (civil34)'),
    ('civil35', 'Civilian (civil35)'),
    ('civil36', 'Civilian (civil36)'),
    ('civil36M', 'Civilian (civil36M)'),
    ('civil37', 'Civilian (civil37)'),
    ('civil38', 'Civilian (civil38)'),
    ('civil39', 'Civilian (civil39)'),
    ('civil40', 'Civilian (civil40)'),
    ('civil41', 'Civilian (civil41)'),
    ('civil42', 'Civilian (civil42)'),
    ('civil42M', 'Civilian (civil42M)'),
    ('civil43', 'Civilian (civil43)'),
    ('civil44', 'Civilian (civil44)'),
    ('civil51', 'Civilian (civil51)'),
    ('civil51M', 'Civilian (civil51M)'),
    ('civil52', 'Civilian (civil52)'),
    ('civil53', 'Civilian (civil53)'),
    ('civil54', 'Civilian (civil54)'),
    ('civil54M', 'Civilian (civil54M)'),
    ('civil55', 'Civilian (civil55)'),
    ('civil55M', 'Civilian (civil55M)'),
    ('civil56', 'Civilian (civil56)'),
    ('civil56M', 'Civilian (civil56M)'),
    ('civil57', 'Civilian (civil57)'),
    ('civil57M', 'Civilian (civil57M)'),
    ('civil60', 'Civilian (civil60)'),
    ('civil61', 'Civilian (civil61)'),
    ('civil62', 'Civilian (civil62)'),
    ('civil63', 'Civilian (civil63)'),
    ('civil70', 'Civilian (civil70)'),
    ('civil70M', 'Civilian (civil70M)'),
    ('civil71', 'Civilian (civil71)'),
    ('civil72', 'Civilian (civil72)'),
    ('Radni01', 'Councillor (Radni01)'),
    ('Radni02', 'Councillor (Radni02)'),
    ('SynRad1', "Councillor's son (SynRad1)"),
    ('SynRad1DEAD', "Councillor's son (SynRad1DEAD)"),
    ('ZenaRad01', "Councillor's wife (ZenaRad01)"),
    ('Detektiv01', 'Detective (Detektiv01)'),
    ('Detektiv01M', 'Detective (Detektiv01M)'),
    ('Detektiv02', 'Detective (Detektiv02)'),
    ('Detektiv03', 'Detective (Detektiv03)'),
    ('ReditelB', 'Director (ReditelB)'),
    ('ReditelH', 'Director (ReditelH)'),
    ('Vratny1', 'Doorman (Vratny1)'),
    ('Enemy04', 'Enemy (Enemy04)'),
    ('Enemy04K', 'Enemy (Enemy04K)'),
    ('Enemy08', 'Enemy (Enemy08)'),
    ('Enemy08K', 'Enemy (Enemy08K)'),
    ('Enemy09', 'Enemy (Enemy09)'),
    ('Enemy09K', 'Enemy (Enemy09K)'),
    ('Enemy10', 'Enemy (Enemy10)'),
    ('Enemy10K', 'Enemy (Enemy10K)'),
    ('Enemy11K', 'Enemy (Enemy11K)'),
    ('Enemy12', 'Enemy (Enemy12)'),
    ('Enemy12K', 'Enemy (Enemy12K)'),
    ('Enemy13C', 'Enemy (Enemy13C)'),
    ('Hasic01', 'Firefighter (Hasic01)'),
    ('FREEgang01', 'Free Ride gangster (FREEgang01)'),
    ('FREEgang02', 'Free Ride gangster (FREEgang02)'),
    ('Friend1', 'Friend (Friend1)'),
    ('Friend2', 'Friend (Friend2)'),
    ('Gangster03', 'Gangster (Gangster03)'),
    ('vratar1', 'Gatekeeper (vratar1)'),
    ('vratar1s', 'Gatekeeper (vratar1s)'),
    ('GodzMan1', 'GodzMan (GodzMan1)'),
    ('Guard01', 'Guard (Guard01)'),
    ('Guard02', 'Guard (Guard02)'),
    ('Homeless01', 'Homeless man (Homeless01)'),
    ('Chulig1', 'Hooligan (Chulig1)'),
    ('Chulig1b', 'Hooligan (Chulig1b)'),
    ('Hoolig01', 'Hooligan (Hoolig01)'),
    ('Hoolig02', 'Hooligan (Hoolig02)'),
    ('Hoolig03', 'Hooligan (Hoolig03)'),
    ('Hoolig04', 'Hooligan (Hoolig04)'),
    ('Hoolig05', 'Hooligan (Hoolig05)'),
    ('Hoolig06', 'Hooligan (Hoolig06)'),
    ('LifeG01', 'Lifeguard (LifeG01)'),
    ('Muz ZAM', 'Man (Muz ZAM)'),
    ('March1', 'March (March1)'),
    ('Milenka1', 'Mistress (Milenka1)'),
    ('Organizator01', 'Organiser (Organizator01)'),
    ('pianist1', 'Pianist (pianist1)'),
    ('pol01', 'Police officer (pol01)'),
    ('pol02', 'Police officer (pol02)'),
    ('pol03', 'Police officer (pol03)'),
    ('pol11', 'Police officer (pol11)'),
    ('pol12', 'Police officer (pol12)'),
    ('pol13', 'Police officer (pol13)'),
    ('PoliceMan01', 'Police officer (PoliceMan01)'),
    ('PoliceMan02', 'Police officer (PoliceMan02)'),
    ('Politik', 'Police officer (Politik)'),
    ('PortGuard01', 'Port guard (PortGuard01)'),
    ('PortGuard02', 'Port guard (PortGuard02)'),
    ('Knez', 'Priest (Knez)'),
    ('Prokur', 'Prosecutor (Prokur)'),
    ('Bitch01', 'Prostitute (Bitch01)'),
    ('Bitch02', 'Prostitute (Bitch02)'),
    ('Bitch02Mask', 'Prostitute (Bitch02Mask)'),
    ('Bitch03M', 'Prostitute (Bitch03M)'),
    ('recep', 'Receptionist (recep)'),
    ('sailor01', 'Sailor (sailor01)'),
    ('sailor01M', 'Sailor (sailor01M)'),
    ('sailor02', 'Sailor (sailor02)'),
    ('sailor02M', 'Sailor (sailor02M)'),
    ('sailor03', 'Sailor (sailor03)'),
    ('SalMan01K', "Salieri's man (SalMan01K)"),
    ('SalMan02K', "Salieri's man (SalMan02K)"),
    ('SalMan03K', "Salieri's man (SalMan03K)"),
    ('SalMan04', "Salieri's man (SalMan04)"),
    ('SalMan05K', "Salieri's man (SalMan05K)"),
    ('Serv01', 'Servant (Serv01)'),
    ('ShadowGirl', 'Shadow girl (ShadowGirl)'),
    ('ShadowGirlHIGH', 'Shadow girl (ShadowGirlHIGH)'),
    ('ShadowMan', 'Shadow man (ShadowMan)'),
    ('ShadowManHIGH', 'Shadow man (ShadowManHIGH)'),
    ('ShadowManLOW2', 'Shadow man (ShadowManLOW2)'),
    ('ProdZ1', 'Shop assistant (ProdZ1)'),
    ('RidicNakladaku', 'Truck driver (RidicNakladaku)'),
    ('UfoPanak', 'UFO man (UfoPanak)'),
    ('waiter01', 'Waiter (waiter01)'),
    ('waiter01M', 'Waiter (waiter01M)'),
    ('waiter02', 'Waiter (waiter02)'),
    ('waiter02M', 'Waiter (waiter02M)'),
    ('waiter03', 'Waiter (waiter03)'),
    ('HighCivil', 'Wealthy civilian (HighCivil)'),
    ('WillG2', "Will's girl (WillG2)"),
    ('WillMan01', "Will's man (WillMan01)"),
    ('WillMan02', "Will's man (WillMan02)"),
    ('Czena01', 'Woman (Czena01)'),
    ('Czena02', 'Woman (Czena02)'),
    ('Czena03', 'Woman (Czena03)'),
    ('Czena04', 'Woman (Czena04)'),
    ('Czena05', 'Woman (Czena05)'),
    ('Czena06', 'Woman (Czena06)'),
    ('Czena07', 'Woman (Czena07)'),
    ('Czena07M', 'Woman (Czena07M)'),
    ('Czena08', 'Woman (Czena08)'),
    ('Czena09', 'Woman (Czena09)'),
    ('Czena09M', 'Woman (Czena09M)'),
    ('Czena10', 'Woman (Czena10)'),
    ('Czena10M', 'Woman (Czena10M)'),
    ('Czena11', 'Woman (Czena11)'),
    ('Czena11M', 'Woman (Czena11M)'),
    ('Czena12', 'Woman (Czena12)'),
    ('Czena13', 'Woman (Czena13)'),
    ('Malticka1', 'Woman (Malticka1)'),
    ('Delnik01', 'Worker (Delnik01)'),
    ('Delnik02', 'Worker (Delnik02)'),
    ('Delnik03', 'Worker (Delnik03)'),
)


@register("mafia.play_as")
class MafiaPlayAs(Driver):
    rate = 1.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.player = None
        self.original = None
        self.applied = None
        self._again = True

    @classmethod
    def choices(cls, g, names):
        return [{"datum": i, "name": label, "path": ""} for i, (_, label) in enumerate(MAFIA_MODELS)]

    def _person(self):
        game = _u32(self.guest, MAFIA_GAME)
        scene = game and _u32(self.guest, game + 0x20)
        return scene and _u32(self.guest, scene + MAFIA_PLAYER)

    def _text(self, at):
        return _fresh_text(self.guest, at)

    def _model(self, person):
        frame = _u32(self.guest, person + 0x80)
        return self._text(_u32(self.guest, frame + 0x134)) if frame else ""

    def _wanted(self):
        picked = self.switch.get("picked") if self.switch else None
        return MAFIA_MODELS[picked if isinstance(picked, int) and 0 <= picked < len(MAFIA_MODELS) else 0]

    def _swap(self, person, name):
        self.caller.call(MAFIA_MODEL_SWAP, person, "@string", 0, convention="thiscall", string=name + ".i3d",
                         title_id=0x54540009)

    def begin(self):
        if self.caller is None:
            raise ValueError("cant call the game from here")
        if not MAFIA_MODELS:
            raise ValueError("no models to pick from")
        if not self._person():
            raise ValueError("nobody to play as right now, is a mission loaded?")
        return self.tick(0.0)

    def repick(self):
        self._again = True

    def tick(self, dt):
        g = self.guest
        person = self._person()
        if not person:
            return "waiting: nobody to play as right now"
        if person != self.player:
            self.player, self._again = person, True
            self.original = self._text(_u32(g, person + 0x66C)) or self._model(person)
        name, label = self._wanted()
        changed = self.applied is None or self.applied[0] != person or self._model(person).lower() != self.applied[1].lower()
        if self._again or changed:
            if _u32(g, person + 0x234):
                return "waiting: get out of the car to become %s" % label
            self._swap(person, name)
            self.applied = (person, self._model(person))
            self._again = False
        return "on: youre %s" % label

    def end(self):
        person = self._person()
        if not (person and person == self.player and self.original):
            return
        if _u32(self.guest, person + 0x234):
            self.log.warn("%s: tommy is in a car so he keeps this model. turn it on and off again on foot to get him back" % self.name, key=("driver", self.name))
            return
        if self._model(person).lower() != self.original.lower():
            self._swap(person, self.original)


MAFIA_CHANGE_PENDING = 0x3CD5E0
MAFIA_NEXT_MISSION = 0x3CD614
MAFIA_ENTRY_POINT = 0x3CD5C0
MAFIA_CARRIED = 0x3CD644
MAFIA_SCENE_ENDING = 0x46AD
MAFIA_MISSION_NUMBER = 0xCA0
MAFIA_CHANGE_HOLD = 0x3CD3CC
MAFIA_FREERIDE_SAVE = 0x3BC9D0
MAFIA_MISSION_NAME = 0x5C
MAFIA_SCENE_CLOCK = 0x3AB4
MAFIA_MISSIONS = (
    ('mise01', "01 An Offer You Can't Refuse"),
    ('mise02a-taxi', '02 Taxi Driver'),
    ('mise02-ulicka', '03 The Running Man'),
    ('mise03-morello', '04 Molotov Party'),
    ('mise04-saliery', "05 Ordinary Routine - Salieri's bar"),
    ('mise05-saliery', "06 Fairplay - Salieri's bar"),
    ('mise06-saliery', "07 Race Day - Salieri's bar"),
    ('mise06-mesto', '07 Race Day - the city'),
    ('mise06-autodrom', '07 Race Day - the race track'),
    ('mise07-sara', '08 Sarah'),
    ('mise07b-saliery', "09 Better Get Used To It - Salieri's bar"),
    ('mise08-mesto', '10 The Whore - the city'),
    ('mise08-hotel', '10 The Whore - the hotel'),
    ('mise08-kostel', '11 The Priest - the church'),
    ('mise09-saliery', "12 A Trip To The Country - Salieri's bar"),
    ('mise09-prejimka', '12 A Trip To The Country - the handover'),
    ('mise10-saliery', "13 Omerta - Salieri's bar"),
    ('mise11-saliery', "14 Visiting Rich People - Salieri's bar"),
    ('mise12-garage', '15 Great Deal! - the garage'),
    ('mise13-mesto', '16 Buon Appetito! - the city'),
    ('mise13-zradce', '16 Buon Appetito! - the traitor'),
    ('mise14-saliery', "17 Happy Birthday! - Salieri's bar"),
    ('mise14-parnik', '17 Happy Birthday! - the steamboat'),
    ('mise15-saliery', "18 You Lucky Bastard! - Salieri's bar"),
    ('mise15-mestohonicka', '18 You Lucky Bastard! - the city, the chase'),
    ('mise15-mestorainbow', '18 You Lucky Bastard! - the city, Rainbow'),
    ('mise15-pristav', '18 You Lucky Bastard! - the harbour'),
    ('mise16-saliery', "19 Crème De La Crème - Salieri's bar"),
    ('mise17-saliery', "20 Election Campaign - Salieri's bar"),
    ('mise18-saliery', "21 Just For Relaxation - Salieri's bar"),
    ('mise19-banka', '22 Moonlighting - the bank'),
    ('mise20-mesto', '23 The Death of Art - the city'),
    ('mise20-galery', '23 The Death of Art - the gallery'),
    ('aa', "Dev test map AA (roadside diner copy)"),
)


@register("mafia.mission_select")
class MafiaMissionSelect(Driver):
    rate = 2.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.wanted = None
        self.loading = None
        self.loaded = None
        self._clock = None

    @classmethod
    def choices(cls, g, names):
        return [{"datum": i, "name": label, "path": ""} for i, (_, label) in enumerate(MAFIA_MISSIONS)]

    def _want(self):
        picked = self.switch.get("picked") if self.switch else None
        if not isinstance(picked, int) or not 0 <= picked < len(MAFIA_MISSIONS):
            raise ValueError("pick a mission first")
        self.wanted = MAFIA_MISSIONS[picked]

    def _ask(self):
        folder, label = self.wanted
        g = self.guest
        if g.read(MAFIA_CHANGE_PENDING, 1) != b"\0":
            return "on: waiting, already changing mission"
        game = _u32(g, MAFIA_GAME)
        scene = _u32(g, game + 0x20) if game else 0
        if not scene:
            return "on: waiting for the game"
        if _u32(g, scene + MAFIA_PLAYER) and not _u32(g, MAFIA_CHANGE_HOLD):
            return "on: waiting for the game"
        g.write(scene + MAFIA_SCENE_ENDING, b"\x01")
        g.write(game + MAFIA_MISSION_NUMBER + 4, g.read(game + MAFIA_MISSION_NUMBER, 4))
        g.write(game + MAFIA_MISSION_NUMBER, bytes(4))
        g.write(MAFIA_FREERIDE_SAVE, b"\0")
        g.write(MAFIA_CARRIED, bytes(8))
        g.write(MAFIA_ENTRY_POINT, b"\0")
        g.write(MAFIA_NEXT_MISSION, folder.encode("ascii")[:47] + b"\0")
        g.write(MAFIA_CHANGE_PENDING, b"\x01")
        self.wanted, self.loading, self._clock = None, (folder, label), None
        return "on: loading %s" % label

    def _check(self):
        folder, label = self.loading
        g = self.guest
        if g.read(MAFIA_CHANGE_PENDING, 1) != b"\0":
            return "on: %s is next, unpause the game if its paused" % label
        game = _u32(g, MAFIA_GAME)
        scene = _u32(g, game + 0x20) if game else 0
        clock = _u32(g, scene + MAFIA_SCENE_CLOCK) if scene else None
        moving = clock is not None and self._clock is not None and clock != self._clock
        self._clock = clock
        if not moving:
            return "on: loading %s" % label
        try:
            name = _fresh_text(g, _u32(g, game + MAFIA_MISSION_NAME)).lower()
        except Unmapped:
            return "on: loading %s" % label
        if name.split("\\")[0] != folder:
            return "on: loading %s" % label
        self.loading, self.loaded = None, label
        return "on: %s loaded" % label

    def begin(self):
        if not MAFIA_MISSIONS:
            raise ValueError("no missions to pick from")
        self._want()
        return self._ask()

    def repick(self):
        self._want()

    def tick(self, dt):
        if self.loading:
            status = self._check()
            if self.loading:
                return status if not self.wanted else status + ", %s is next" % self.wanted[1]
        if self.wanted:
            return self._ask()
        return "on: %s loaded" % self.loaded
