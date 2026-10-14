import struct
import threading
import time

from ..xbdm import Unreadable, XbdmError
from . import address

RAM_SIZE = 0x04000000
DATA_ARRAY_HEAD = 0x48
PAGE_DIR = 0x0000F000
XBE_BASE = 0x00010000
EMU_BASE = 0x82000000

XEFU_BUILDS = {
    (0x437ACD68, 0x000B4328): "xefu",
    (0x4537DDE5, 0x000B4570): "xefu1_1",
    (0x43E94E5E, 0x000B51A0): "xefu2",
    (0x442C87BD, 0x000AF3F8): "xefu3",
    (0x44F633D3, 0x000AF058): "xefu5",
    (0x457DD568, 0x000C37D0): "xefu6",
    (0x474B3577, 0x000C3DA0): "xefu7",
    (0x474B3577, 0x000C1C48): "xefu7b",
}
RAM_BASE_GLOBAL = {
    "xefu": 0x8217200C,
    "xefu1_1": 0x8217200C,
    "xefu2": 0x8217298C,
    "xefu3": 0x82197E8C,
    "xefu5": 0x821A818C,
    "xefu6": 0x821B900C,
    "xefu7": 0x821DE210,
    "xefu7b": 0x821CE210,
}


class NotRunning(Exception):
    pass


class Unmapped(Exception):
    pass


class Empty(LookupError):
    pass


class Guest:
    TABLE_TTL = 20.0

    def __init__(self, console):
        self.console = console
        self.ram_base = None
        self.emulator = None
        self.xbe = None
        self.symbols = {}
        self._pd = None
        self._pt = {}
        self._tables_at = 0.0
        self._checked = {}
        self._xbe_at = None
        self._lock = threading.RLock()

    def detect(self):
        with self._lock:
            self.ram_base = self.emulator = self.xbe = self._pd = self._xbe_at = None
            try:
                head = self.console.read(EMU_BASE, 0x400)
            except (Unreadable, XbdmError):
                raise NotRunning("no game loaded")
            if head[:2] != b"MZ":
                raise NotRunning("whats running isnt the og xbox emulator")
            pe = struct.unpack_from("<I", head, 0x3C)[0]
            stamp = struct.unpack_from("<I", head, pe + 8)[0]
            entry = struct.unpack_from("<I", head, pe + 0x28)[0]
            self.emulator = XEFU_BUILDS.get((stamp, entry))
            if not self.emulator:
                raise NotRunning("whats running isnt the og xbox emulator")
            self.ram_base = self._find_ram()
            if self.ram_base is None:
                raise NotRunning("%s is running but cant find the games memory yet" % self.emulator)
            self.refresh_tables(force=True)
            self.xbe = self._read_xbe()
            if self.xbe:
                self._xbe_at = self.host(XBE_BASE)

    def still_there(self):
        at = self._xbe_at
        if at is None:
            return False
        try:
            return self.console.read(at, 4) == b"XBEH"
        except (Unreadable, XbdmError):
            return False

    def _is_ram(self, base):
        try:
            if self.console.read(base + 0x10000, 2) != b"MZ":
                return False
            pde = struct.unpack("<I", self.console.read(base + PAGE_DIR + 0x300 * 4, 4))[0]
            return (pde & 0xFFFFF001) == (PAGE_DIR | 1)
        except (Unreadable, XbdmError):
            return False

    def _find_ram(self):
        known = RAM_BASE_GLOBAL.get(self.emulator)
        if known:
            try:
                base, end = struct.unpack(">II", self.console.read(known, 8))
                if base and end - base == RAM_SIZE and self._is_ram(base):
                    return base
            except (Unreadable, XbdmError):
                pass
        regions = self.console.regions()
        for base in range(0xC0000000, 0xE0000000, 0x01000000):
            if any(lo <= base and base + RAM_SIZE <= lo + size for lo, size in regions) and self._is_ram(base):
                return base
        return None

    def refresh_tables(self, force=False):
        with self._lock:
            if not force and self._pd is not None and time.time() - self._tables_at < self.TABLE_TTL:
                return
            if self.ram_base is None:
                raise NotRunning("no game detected")
            pd = struct.unpack("<1024I", self.console.read(self.ram_base + PAGE_DIR, 0x1000))
            tables = sorted({e & 0xFFFFF000 for e in pd if e & 1 and not e & 0x80 and (e & 0xFFFFF000) < RAM_SIZE})
            runs = []
            for t in tables:
                if runs and runs[-1][0] + runs[-1][1] == t:
                    runs[-1][1] += 0x1000
                else:
                    runs.append([t, 0x1000])
            pt = {}
            for (start, size), raw in zip(runs, self.console.read_many([(self.ram_base + a, n) for a, n in runs])):
                for off in range(0, size if raw else 0, 0x1000):
                    pt[start + off] = struct.unpack("<1024I", raw[off:off + 0x1000])
            self._pt = pt
            self._pd = pd
            self._checked = {}
            self._tables_at = time.time()

    def translate(self, va):
        pd = self._pd
        if pd is None:
            return None
        pde = pd[(va >> 22) & 0x3FF]
        if not pde & 1:
            return None
        if pde & 0x80:
            phys = (pde & 0xFFC00000) | (va & 0x3FFFFF)
        else:
            table = self._pt.get(pde & 0xFFFFF000)
            if table is None:
                return None
            pte = table[(va >> 12) & 0x3FF]
            if not pte & 1:
                return None
            phys = (pte & 0xFFFFF000) | (va & 0xFFF)
        return phys if phys < RAM_SIZE else None

    def host(self, va):
        phys = self.translate(va)
        return None if phys is None else self.ram_base + phys

    def spans(self, va, size):
        out = []
        end = va + size
        while va < end:
            n = min(0x1000 - (va & 0xFFF), end - va)
            h = self.host(va)
            last = out[-1] if out else None
            if last and ((h is None and last[1] is None) or
                         (h is not None and last[1] is not None and last[1] + last[2] == h)):
                last[2] += n
            else:
                out.append([va, h, n])
            va += n
        return out

    def mapped(self, lo=0x00010000, hi=0x80000000):
        self.refresh_tables()
        out = []
        va = lo & ~0xFFF
        while va < hi:
            pde = self._pd[va >> 22]
            if not pde & 1:
                va = ((va >> 22) + 1) << 22
                continue
            step = 0x400000 - (va & 0x3FFFFF) if pde & 0x80 else 0x1000
            if self.translate(va) is not None:
                if out and out[-1][1] == va:
                    out[-1][1] = va + step
                else:
                    out.append([va, va + step])
            va += step
        return [(a, min(b, hi)) for a, b in out]

    def read(self, va, size, fill=None):
        self.refresh_tables()
        parts = self.spans(va, size)
        if fill is None and any(h is None for _, h, _ in parts):
            raise Unmapped("game address %08X isnt mapped" % next(v for v, h, _ in parts if h is None))
        out = bytearray()
        for _, h, n in parts:
            out += fill * n if h is None else self.console.read(h, n)
        return bytes(out)

    def read_ranges(self, ranges, progress=None):
        with self.console.quiet(sum(size for _, size in ranges)):
            self.refresh_tables(force=True)
            spans, layout = [], []
            for va, size in ranges:
                pieces = self.spans(va, size)
                layout.append(pieces)
                spans += [(h, n) for _, h, n in pieces if h is not None]
            data = iter(self.console.read_many(spans, progress))
        out = []
        for pieces in layout:
            buf = bytearray()
            for _, h, n in pieces:
                buf += bytes(n) if h is None else (next(data) or bytes(n))
            out.append(bytes(buf))
        return out

    def read_small(self, items):
        self.refresh_tables()
        out = [None] * len(items)
        wanted = []
        for i, (va, size) in enumerate(items):
            parts = self.spans(va, size)
            if len(parts) == 1 and parts[0][1] is not None:
                wanted.append((parts[0][1], size, i))
            elif all(h is not None for _, h, _ in parts):
                try:
                    out[i] = self.read(va, size)
                except (Unmapped, Unreadable, XbdmError):
                    pass
        wanted.sort()
        groups = []
        for h, size, i in wanted:
            if groups and h + size - groups[-1][0] <= 0x400:
                groups[-1][1] = max(groups[-1][1], h + size)
                groups[-1][2].append((h, size, i))
            else:
                groups.append([h, h + size, [(h, size, i)]])
        for (start, _, members), raw in zip(groups, self.console.read_many([(g[0], g[1] - g[0]) for g in groups])):
            if raw is not None:
                for h, size, i in members:
                    out[i] = raw[h - start:h - start + size]
        return out

    def _page_now(self, va):
        page = va >> 12
        seen = self._checked.get(page)
        if seen and time.time() - seen[1] < 2.0:
            return seen[0]
        base = self.ram_base
        phys = None
        pde = struct.unpack("<I", self.console.read(base + PAGE_DIR + (va >> 22) * 4, 4))[0]
        if pde & 1 and pde & 0x80:
            phys = (pde & 0xFFC00000) | (va & 0x3FF000)
        elif pde & 1 and (pde & 0xFFFFF000) < RAM_SIZE:
            pte = struct.unpack("<I", self.console.read(base + (pde & 0xFFFFF000) + ((va >> 12) & 0x3FF) * 4, 4))[0]
            if pte & 1:
                phys = pte & 0xFFFFF000
        if phys is not None and phys >= RAM_SIZE:
            phys = None
        if len(self._checked) > 4096:
            self._checked.clear()
        self._checked[page] = (phys, time.time())
        return phys

    def write(self, va, data):
        if self.ram_base is None:
            raise NotRunning("no game detected")
        parts = []
        pos, end = va, va + len(data)
        while pos < end:
            n = min(0x1000 - (pos & 0xFFF), end - pos)
            phys = self._page_now(pos)
            if phys is None:
                raise Unmapped("game address %08X isnt mapped, nothing written" % pos)
            parts.append((self.ram_base + phys + (pos & 0xFFF), pos - va, n))
            pos += n
        for host, off, n in parts:
            self.console.write(host, data[off:off + n])

    def read_u32(self, va):
        return struct.unpack("<I", self.read(va, 4))[0]

    def _read_xbe(self):
        try:
            head = self.read(XBE_BASE, 0x1000)
        except (Unmapped, Unreadable, XbdmError):
            return None
        if head[:4] != b"XBEH":
            return None
        base, hdr_size, image_size, _, _, cert, nsec, sec = struct.unpack_from("<8I", head, 0x104)
        if hdr_size > 0x1000:
            head = self.read(base, min(hdr_size, 0x8000), fill=b"\0")

        def at(addr, n):
            off = addr - base
            return head[off:off + n] if 0 <= off and off + n <= len(head) else b""

        cert_raw = at(cert, 0x60)
        title_id = struct.unpack_from("<I", cert_raw, 8)[0] if len(cert_raw) >= 12 else 0
        title = cert_raw[0xC:0xC + 80].decode("utf-16le", "replace").split("\0")[0] if cert_raw else ""
        sections = []
        for i in range(min(nsec, 64)):
            raw = at(sec + i * 0x38, 0x38)
            if len(raw) < 24:
                break
            flags, va, vsize, _, rsize, name_addr = struct.unpack_from("<6I", raw)
            name = at(name_addr, 16).split(b"\0")[0].decode("latin-1")
            sections.append({"name": name, "va": va, "size": vsize, "writable": bool(flags & 1)})
        return {"base": base, "image_size": image_size, "title": title or "Unknown game",
                "title_id": "%08X" % title_id, "sections": sections}

    def section_of(self, va):
        for s in (self.xbe or {}).get("sections", []):
            if s["va"] <= va < s["va"] + s["size"]:
                return s["name"]
        if self.xbe and self.xbe["base"] <= va < self.xbe["base"] + 0x1000:
            return "XBE header"
        if 0x80000000 <= va < 0x84000000:
            return "kernel / contiguous memory"
        return "heap" if va < 0x80000000 else ""

    def regions(self, preset, custom=None):
        x = self.xbe
        image_end = (x["base"] + x["image_size"] + 0xFFF) & ~0xFFF if x else XBE_BASE
        if preset == "data":
            return [(s["va"], s["size"]) for s in x["sections"] if s["writable"] and not s["name"].startswith("$$")] if x else []
        if preset == "heap":
            return [(a, b - a) for a, b in self.mapped(image_end, 0x80000000)]
        if preset == "all":
            return self.regions("data") + self.regions("heap")
        if preset == "image":
            return [(x["base"], image_end - x["base"])] if x else []
        if preset == "custom" and custom:
            lo, hi = custom
            if hi <= lo:
                raise ValueError("end address has to be after the start")
            return [(lo, hi - lo)]
        raise ValueError("unknown memory area: %r" % preset)

    def check(self, expr):
        for word in address.words(address.parse(expr)):
            if word not in self.symbols and not address.is_hex(word):
                raise ValueError("unknown name or number %r" % word)

    def resolve(self, expr, memo=None):
        if isinstance(expr, int):
            return expr
        memo = {} if memo is None else memo
        stack = memo.setdefault("_names", [])

        def word(name):
            if name in self.symbols:
                if name in memo:
                    return memo[name]
                if name in stack:
                    raise ValueError("%s points back to itself" % name)
                stack.append(name)
                try:
                    memo[name] = self.resolve(self.symbols[name], memo)
                finally:
                    stack.pop()
                return memo[name]
            try:
                return int(name, 16)
            except ValueError:
                raise ValueError("unknown name or number %r" % name)

        def call(name, args):
            if name == "datum":
                return self.datum(*args, memo=memo)
            if name == "block":
                return self.block(*args)
            if name == "object":
                return self.object(*args)
            raise ValueError("unknown function %s()" % name)

        return address.evaluate(address.parse(expr), word, self.read_u32, call) & 0xFFFFFFFF

    def block(self, header, index, stride):
        count, elements = struct.unpack("<iI", self.read(header, 8))
        if not 0 <= index < count or count > 0x10000 or not elements:
            raise Empty("nothing there right now (block %08X has %d)" % (header, count))
        return (elements + index * stride) & 0xFFFFFFFF

    def object(self, where, vtable):
        try:
            obj = self.read_u32(where)
            if obj and self.read_u32(obj) == vtable:
                return obj
        except (Unmapped, Unreadable):
            pass
        raise Empty("nothing there right now")

    def datum(self, array, handle, memo=None):
        memo = {} if memo is None else memo
        key = ("datum", array)
        if key not in memo:
            head = self.read(array, DATA_ARRAY_HEAD)
            if head[0x2C:0x30] == b"@t@d":
                memo[key] = (struct.unpack_from("<H", head, 0x20)[0], struct.unpack_from("<I", head, 0x24)[0],
                             struct.unpack_from("<I", head, 0x44)[0])
            elif head[0x28:0x2C] == b"@t@d":
                memo[key] = (struct.unpack_from("<H", head, 0x20)[0], struct.unpack_from("<H", head, 0x22)[0],
                             struct.unpack_from("<I", head, 0x34)[0])
            else:
                raise ValueError("%08X isnt a data array (no d@t@ signature)" % array)
        count, size, data = memo[key]
        index, salt = handle & 0xFFFF, handle >> 16
        if handle == 0xFFFFFFFF or salt == 0:
            raise Empty("nothing there right now (empty handle)")
        if index >= count or not 0 < size <= 0x10000:
            raise Empty("handle %08X is outside its data array" % handle)
        entry = (data + index * size) & 0xFFFFFFFF
        if struct.unpack("<H", self.read(entry, 2))[0] != salt:
            raise Empty("handle %08X is stale (its slot has something else now)" % handle)
        return entry
