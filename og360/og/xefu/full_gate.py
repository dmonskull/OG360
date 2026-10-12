import struct

SAVED_ESP, REQUEST, FX_AREA, TARGET = 0x00, 0x04, 0x08, 0x0C
EBX_IN, EBP_IN = 0x10, 0x14
XMM_IN = 0x18
XMM0_OUT, EDX_KEPT = 0x28, 0x2C
MAILBOX_SIZE = 0x40
SCAN = 0x10000
MIN_GAP = 14
_INSTALLED = {}


class NoRoom(RuntimeError):
    pass


def units(mailbox):
    m = lambda off: struct.pack("<I", mailbox + off)
    return [
        b"\x89\x1d" + m(REQUEST),
        b"\x8b\x43\x28",
        b"\xa3" + m(TARGET),
        b"\x89\x25" + m(SAVED_ESP),
        b"\x81\xec\x10\x02\x00\x00",
        b"\x83\xe4\xf0",
        b"\x0f\xae\x04\x24",
        b"\x89\x25" + m(FX_AREA),
        b"\xdb\xe3",
        b"\xd9\x2c\x24",
        b"\xf3\x0f\x10\x05" + m(XMM_IN),
        b"\xf3\x0f\x10\x0d" + m(XMM_IN + 4),
        b"\xf3\x0f\x10\x15" + m(XMM_IN + 8),
        b"\xf3\x0f\x10\x1d" + m(XMM_IN + 12),
        b"\x83\xec\x18",
        b"\x8b\xfc",
        b"\x8d\x73\x10",
        b"\x6a\x06", b"\x59",
        b"\xf3\xa5",
        b"\x8b\x43\x2c", b"\x8b\x4b\x30", b"\x8b\x53\x34", b"\x8b\x73\x38", b"\x8b\x7b\x3c",
        b"\x8b\x2d" + m(EBP_IN),
        b"\x8b\x1d" + m(EBX_IN),
        b"\xff\x15" + m(TARGET),
        b"\x89\x15" + m(EDX_KEPT),
        b"\xf3\x0f\x11\x05" + m(XMM0_OUT),
        b"\x8b\x1d" + m(REQUEST),
        b"\x89\x53\x68",
        b"\x92",
        b"\xdf\xe0",
        b"\x80\xe4\x38",
        b"\x74\x03\xdd\x5b\x70",
        b"\x92",
        b"\x8b\x25" + m(FX_AREA),
        b"\x0f\xae\x0c\x24",
        b"\x8b\x25" + m(SAVED_ESP),
        b"\xc3",
    ]


def main_code(info):
    code = [s for s in info["sections"] if s["executable"] and s["raw_size"]]
    if not code:
        raise NoRoom("the game has no code section")
    return max(code, key=lambda s: s["raw_size"])


def gaps(read, info, exclude=(), want=40):
    sec = main_code(info)
    top = sec["address"] + min(sec["raw_size"], sec["size"])
    out = []
    hi = top
    while hi > sec["address"] and len(out) < want:
        lo = max(sec["address"], hi - SCAN)
        data = read(lo, hi - lo)
        found, i = [], 0
        while i < len(data):
            v = data[i]
            if v not in (0xCC, 0x90):
                i += 1
                continue
            j = i
            while j < len(data) and data[j] == v:
                j += 1
            a, b = lo + i, lo + j
            after_ret = i >= 1 and data[i - 1] == 0xC3 or i >= 3 and data[i - 3] == 0xC2 \
                or i >= 5 and data[i - 5] == 0xE9 or i >= 2 and data[i - 2] == 0xEB
            thunk_pad = a % 0x20 == 0x14 and i >= 6 and data[i - 6] == 0xE8 and data[i - 1] == 0xC3
            one_page = a >> 12 == (b - 1) >> 12
            if b - a >= MIN_GAP and b % 16 == 0 and b < hi and after_ret and not thunk_pad and one_page \
                    and not any(x < b and a < y for x, y in exclude):
                found.append({"address": a, "original": bytes([v]).hex() * (b - a)})
            i = j
        out = found + out
        hi = lo
    return sorted(out, key=lambda g: g["address"])[-want:]


def build(mailbox, blocks):
    from .game_hook import layout
    pieces = layout(units(mailbox), blocks)
    return [(b["address"], raw) for b, raw in zip(blocks, pieces)]


def install(read, write, info, key, mailbox, exclude=()):
    cache = (key, info["title_id"], info["timestamp"], mailbox)
    pieces = _INSTALLED.get(cache)
    if pieces and all(read(a, len(raw)) == raw for a, raw in pieces):
        return pieces[0][0], pieces
    candidates = gaps(read, info, exclude=exclude)
    try:
        pieces = build(mailbox, candidates)
    except RuntimeError:
        raise NoRoom("not enough int3 gaps in the games code for the full gate") from None
    originals = {g["address"]: bytes.fromhex(g["original"]) for g in candidates}
    for a, raw in pieces:
        if read(a, len(raw)) != originals[a][:len(raw)]:
            raise NoRoom("a gap changed while the full gate was being put in")
    for a, raw in pieces:
        write(a, raw)
    _INSTALLED[cache] = pieces
    return pieces[0][0], pieces


def inputs(ebx=0, ebp=0, xmm=()):
    xmm = list(xmm) + [0] * (4 - len(xmm))
    if len(xmm) > 4:
        raise ValueError("xmm0 to xmm3 only")
    raw = bytearray(MAILBOX_SIZE)
    struct.pack_into("<2I4I", raw, EBX_IN, ebx & 0xFFFFFFFF, ebp & 0xFFFFFFFF, *[v & 0xFFFFFFFF for v in xmm])
    return bytes(raw)
