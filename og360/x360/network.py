import collections
import re
import struct

from ..xbdm import Unreadable, XbdmError

INTERCEPT = 109
TITLE = 1
SYSTEM = 2
TAKEN = 0x2735
EXECUTABLE_MODULE = 403
CODE_PAGES = 1
ROUTINE = (0x2C030000 | TITLE,
           0x4082000C,
           0x38600000,
           0x4E800020)
RETURN_0 = bytes.fromhex("386000004E800020")
WRAPPER = ((0x3D600000, 0xFFFF0000),
           (0x7CE83B78, 0xFFFFFFFF),
           (0x816B0000, 0xFFFF0000),
           (0x2B0B0000, 0xFFFFFFFF),
           (0x409A000C, 0xFFFFFFFF),
           (0x38600000, 0xFFFF0000),
           (0x4E800020, 0xFFFFFFFF),
           (0x7CC73378, 0xFFFFFFFF),
           (0x7CA62B78, 0xFFFFFFFF),
           (0x7C852378, 0xFFFFFFFF),
           (0x7C641B78, 0xFFFFFFFF),
           (0x7D635B78, 0xFFFFFFFF),
           (0x48000000, 0xFC000003))
HOLD_STORES = (0x93BF0108,
               0x939F00FC,
               0x93DF0100,
               0x937F0104)


def _u32(client, address):
    return struct.unpack(">I", client.read(address, 4))[0]


def modules(client):
    out = []
    for line in client.lines("modules"):
        name = re.search(r'name="([^"]+)"', line)
        base = re.search(r"base=(0x[0-9a-fA-F]+)", line)
        size = re.search(r"size=(0x[0-9a-fA-F]+)", line)
        if name and base and size:
            out.append((name.group(1), int(base.group(1), 16), int(size.group(1), 16)))
    return out


def _module(mods, name):
    found = [(base, size) for n, base, size in mods if n.lower() == name]
    if not found:
        raise LookupError("%s isnt in the module list" % name)
    return found[0]


class Exports:
    def __init__(self, client, header):
        self.security = _u32(client, header + 0x10)
        self.address = _u32(client, self.security + 0x160)
        head = client.read(self.address + 0x20, 12)
        self.base = (struct.unpack_from(">I", head)[0] & 0xFFFF) << 16
        self.count, self.first = struct.unpack_from(">II", head, 4)

    def slot(self, ordinal):
        return self.address + 0x2C + (ordinal - self.first) * 4

    def target(self, client, ordinal):
        return self.base + _u32(client, self.slot(ordinal))

    @property
    def end(self):
        return self.address + 0x2C + self.count * 4


def _kernel_exports(client, kernel_base):
    head = client.read(kernel_base, 0x2000)
    for at in range(0, len(head) - 4, 4):
        if head[at:at + 4] == b"XEX2":
            try:
                exports = Exports(client, kernel_base + at)
            except (Unreadable, XbdmError):
                continue
            if exports.base == kernel_base and exports.count > EXECUTABLE_MODULE:
                return exports
    raise LookupError("cant find the kernels export table")


def _loader_entry(client, kernel, image_base):
    first = entry = _u32(client, kernel.target(client, EXECUTABLE_MODULE))
    for _ in range(64):
        if not entry:
            break
        head = client.read(entry, 0x20)
        if struct.unpack_from(">I", head, 0x1C)[0] == image_base:
            return entry
        entry = struct.unpack_from(">I", head)[0]
        if entry == first:
            break
    raise LookupError("xam.xex isnt in the module list")


def _code(client, exports, address):
    flags, load = struct.unpack(">II", client.read(exports.security + 0x10C, 8))
    page = 0x1000 if flags & 0x10000000 else 0x10000
    count = _u32(client, exports.security + 0x180)
    pages = client.read(exports.security + 0x184, count * 0x18)
    start = load
    for i in range(count):
        info = struct.unpack_from(">I", pages, i * 0x18)[0]
        end = start + (info >> 4) * page
        if start <= address < end:
            return info & 0xF == CODE_PAGES
        start = end
    return False


def routine(at, real):
    return struct.pack(">5I", *ROUTINE, 0x48000000 | ((real - (at + 16)) & 0x03FFFFFC))


def _routine_target(code, at):
    if len(code) < 20 or struct.unpack_from(">4I", code) != ROUTINE:
        return None
    branch = struct.unpack_from(">I", code, 16)[0]
    if branch & 0xFC000003 != 0x48000000:
        return None
    offset = branch & 0x03FFFFFC
    return at + 16 + (offset - 0x04000000 if offset & 0x02000000 else offset)


class State:
    def __init__(self, kind, slot, base, real=None, spot=None, owner=""):
        self.kind, self.slot, self.base = kind, slot, base
        self.real, self.spot, self.owner = real, spot, owner


def look(client):
    mods = modules(client)
    xam_base, xam_size = _module(mods, "xam.xex")
    kernel = _kernel_exports(client, _module(mods, "xboxkrnl.exe")[0])
    xam = Exports(client, _u32(client, _loader_entry(client, kernel, xam_base) + 0x58))
    if xam.base != xam_base or xam.count < INTERCEPT:
        raise LookupError("xams export table looks wrong")
    slot = xam.slot(INTERCEPT)
    target = xam.target(client, INTERCEPT)
    if not xam_base <= target < xam_base + xam_size:
        owner = next((name for name, base, size in mods if base <= target < base + size), "")
        return State("elsewhere", slot, xam.base, owner=owner)
    real = _routine_target(client.read(target, 20), target)
    if real is not None:
        return State("kept", slot, xam.base, real, target)
    spot = ((xam.end + 0xFFFF) & ~0xFFFF) - 0x20
    if spot < xam.end or not _code(client, xam, spot):
        raise LookupError("no room for the routine after xams export table")
    here = client.read(spot, 20)
    if any(here) and _routine_target(here, spot) != target:
        raise LookupError("the space after xams export table isnt empty")
    return State("open", slot, xam.base, target, spot)


def install(client, state):
    if _u32(client, state.slot) != state.real - state.base:
        raise RuntimeError("xam export #109 changed while OG360 was checking it")
    code = routine(state.spot, state.real)
    client.write(state.spot, code)
    if client.read(state.spot, len(code)) != code:
        raise RuntimeError("the routine didnt stick at 0x%08X" % state.spot)
    client.write(state.slot, struct.pack(">I", state.spot - state.base))
    if _u32(client, state.slot) != state.spot - state.base:
        raise RuntimeError("xam export #109 didnt stick")


def release(client, state):
    client.write(state.slot, struct.pack(">I", state.real - state.base))


class Card:
    def __init__(self, kind, real=None, state=None, stub=None, owner=0, module=""):
        self.kind, self.real, self.state, self.stub = kind, real, state, stub
        self.owner, self.module = owner, module


def _net_state(client, real):
    words = struct.unpack(">%dI" % len(WRAPPER), client.read(real, len(WRAPPER) * 4))
    if any(word & mask != want for word, (want, mask) in zip(words, WRAPPER)):
        raise LookupError("xam export #109 isnt the one OG360 knows")
    low = words[2] & 0xFFFF
    state_global = (((words[0] & 0xFFFF) << 16) + (low - 0x10000 if low & 0x8000 else low)) & 0xFFFFFFFF
    offset = words[-1] & 0x03FFFFFC
    worker = real + (len(WRAPPER) - 1) * 4 + (offset - 0x04000000 if offset & 0x02000000 else offset)
    body = struct.unpack(">64I", client.read(worker, 0x100))
    if not all(store in body for store in HOLD_STORES):
        raise LookupError("xam export #109 isnt the one OG360 knows")
    return _u32(client, state_global)


def _return_0(client, xam):
    slots = struct.unpack(">%dI" % xam.count, client.read(xam.slot(xam.first), xam.count * 4))
    for target, _ in collections.Counter(xam.base + s for s in slots if s).most_common(8):
        if client.read(target, len(RETURN_0)) == RETURN_0:
            return target
    raise LookupError("cant find a li r3,0 in xam")


def card(client):
    mods = modules(client)
    xam_base, xam_size = _module(mods, "xam.xex")
    kernel = _kernel_exports(client, _module(mods, "xboxkrnl.exe")[0])
    xam = Exports(client, _u32(client, _loader_entry(client, kernel, xam_base) + 0x58))
    if xam.base != xam_base or xam.count < INTERCEPT:
        raise LookupError("xams export table looks wrong")
    target = xam.target(client, INTERCEPT)
    if not xam_base <= target < xam_base + xam_size:
        module = next((name for name, base, size in mods if base <= target < base + size), "")
        return Card("elsewhere", module=module)
    real = _routine_target(client.read(target, 20), target) or target
    state = _net_state(client, real)
    if not state:
        raise LookupError("xams network isnt up yet")
    send, receive, _, owner = struct.unpack(">4I", client.read(state + 0xFC, 16))
    stub = _return_0(client, xam)
    if owner == SYSTEM and receive == stub and not send:
        return Card("ours", real, state, stub, owner)
    return Card("taken" if owner else "free", real, state, stub, owner)


def claim(client, card):
    answer = client.call_int32(card.real, [SYSTEM, 0, card.stub, 0, 0]) & 0xFFFFFFFF
    if answer == TAKEN:
        raise RuntimeError("something else took the network card first")
    if answer:
        raise RuntimeError("xam answered 0x%X" % answer)
    send, receive, _, owner = struct.unpack(">4I", client.read(card.state + 0xFC, 16))
    if (owner, receive, send) != (SYSTEM, card.stub, 0):
        raise RuntimeError("the hold on the network card didnt stick")
