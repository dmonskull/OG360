import os
import re
import struct

from PIL import Image

from ..og.guest import XEFU_BUILDS
from ..xbdm import Unreadable, XbdmError, XBDMClient
from .network import modules

XBDM_SCREENSHOT_OK = {"xefu7"}

GETTERS = {0x8008240C: 8}
LWZ_R3_R3, BLR = 0x80630000, 0x4E800020

D1GRPH_CONTROL = 0x7FC86104
D1GRPH_SURFACE = 0x7FC86110
D1GRPH_PITCH = 0x7FC86120
D1GRPH_X_START, D1GRPH_Y_START = 0x7FC8612C, 0x7FC86130
D1GRPH_X_END, D1GRPH_Y_END = 0x7FC86134, 0x7FC86138
WINDOWS = (0xC0000000, 0xA0000000, 0xDFFFF000)

_getters = {}
_untiler = XBDMClient.__new__(XBDMClient)


class CaptureError(RuntimeError):
    pass


def emulator(client):
    for name, base, _ in modules(client):
        m = re.fullmatch(r"(xefu(?!title)[0-9a-z_]*)\.xex", name.lower())
        if m:
            stamp, entry = _identity(client, base)
            return XEFU_BUILDS.get((stamp, entry)) or "%s.xex (unknown build%s)" % (
                m.group(1), " %08X" % stamp if stamp is not None else "")
    return None


def _identity(client, base):
    try:
        head = client.read(base, 0x400)
    except (Unreadable, XbdmError):
        return None, None
    pe = struct.unpack_from("<I", head, 0x3C)[0] if head[:2] == b"MZ" else 0
    if not 0 < pe <= len(head) - 0x2C:
        return None, None
    return struct.unpack_from("<I", head, pe + 8)[0], struct.unpack_from("<I", head, pe + 0x28)[0]


def xbdm_safe(build):
    return build is None or build in XBDM_SCREENSHOT_OK


def _getter(client):
    kernel = next(((base, size) for name, base, size in modules(client) if name.lower() == "xboxkrnl.exe"), None)
    if kernel in _getters:
        return _getters[kernel]
    for address, d in GETTERS.items():
        if struct.unpack(">II", client.read(address, 8)) == (LWZ_R3_R3 | d, BLR):
            _getters[kernel] = (address, d)
            return address, d
    if kernel:
        base, size = kernel
        carry = b""
        for off in range(0, size, 0x10000):
            try:
                piece = carry + client.read(base + off, min(0x10000, size - off))
            except (Unreadable, XbdmError):
                carry = b""
                continue
            start = base + off - len(carry)
            for i in range(0, len(piece) - 7, 4):
                w0, w1 = struct.unpack_from(">II", piece, i)
                if w1 == BLR and w0 & 0xFFFF0000 == LWZ_R3_R3:
                    _getters[kernel] = (start + i, w0 & 0xFFFF)
                    return _getters[kernel]
            carry = piece[-4:]
    raise CaptureError("cant find a kernel getter (lwz r3,d(r3); blr) to read the display registers")


def _peek(client, getter, address):
    at, d = getter
    return client.call_int32(at, [address - d]) & 0xFFFFFFFF


def _display(client):
    if not client.has_jrpc():
        raise CaptureError("the jrpc2 plugin isnt answering (it reads the display registers)")
    getter = _getter(client)
    regs = {name: _peek(client, getter, address) for name, address in (
        ("control", D1GRPH_CONTROL), ("pitch", D1GRPH_PITCH), ("x0", D1GRPH_X_START),
        ("y0", D1GRPH_Y_START), ("x1", D1GRPH_X_END), ("y1", D1GRPH_Y_END))}
    return getter, regs


def _window(console, surface, size):
    for base in WINDOWS:
        start = (surface + base) & 0xFFFFFFFF
        try:
            console.read(start, 4)
            console.read(start + size - 4, 4)
            return start
        except (Unreadable, XbdmError):
            continue
    raise CaptureError("cant read the screen buffer (physical 0x%08X)" % surface)


def capture(console, folder):
    getter, r = console.run(_display)
    if r["control"] & 3 != 2:
        raise CaptureError("screen isnt 32 bits per pixel (D1GRPH_CONTROL 0x%08X)" % r["control"])
    width, height = r["x1"] - r["x0"], r["y1"] - r["y0"]
    if not (0 < width <= 4096 and 0 < height <= 4096):
        raise CaptureError("screen size %d x %d looks wrong" % (width, height))
    pitch = r["pitch"] if width <= r["pitch"] <= 8192 else width
    size = ((pitch + 127) & ~127) * ((height + 127) & ~127) * 4
    with console.quiet(size, force=True):
        surface = console.run(lambda c: _peek(c, getter, D1GRPH_SURFACE))
        if surface >= 0x20000000:
            raise CaptureError("screen buffer address 0x%08X looks wrong" % surface)
        frame = console.read_many([(_window(console, surface, size), size)])[0]
    if frame is None:
        raise CaptureError("couldnt read the screen buffer")
    linear = _untiler.untile_xenos_rgba(frame, pitch, height, size)
    image = Image.frombuffer("RGBA", (pitch, height), bytes(linear), "raw", "BGRA", 0, 1).convert("RGB")
    if pitch > width:
        image = image.crop((0, 0, width, height))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, _untiler.screenshot_name())
    image.save(path)
    return path
