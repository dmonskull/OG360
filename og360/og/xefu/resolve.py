#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from typing import Callable, Iterable


RAM_SIZE = 0x04000000
PAGE_DIR = 0xF000
RAM_ANCHOR_RVA = 0x1DE210
PROFILE_BASE = 0x82000000
PROFILE_STAMP = 0x474B3577
PROFILE_ENTRY = 0xC3DA0
DEFAULT_ADDRESSES = (0x10000, 0x136250, 0x13627C, 0x134AB8, 0x12BB94, 0x10A60)
SUPPORTED_BUILDS = {
    (PROFILE_STAMP, PROFILE_ENTRY): ("xefu7", RAM_ANCHOR_RVA),
    (0x4537DDE5, 0x000B4570): ("xefu1_1", 0x17200C),
    (0x44F633D3, 0x000AF058): ("xefu5", 0x1A818C),
    (0x437ACD68, 0x000B4328): ("xefu", 0x17200C),
    (0x43E94E5E, 0x000B51A0): ("xefu2", 0x17298C),
    (0x442C87BD, 0x000AF3F8): ("xefu3", 0x197E8C),
    (0x457DD568, 0x000C37D0): ("xefu6", 0x1B900C),
    (0x474B3577, 0x000C1C48): ("xefu7b", 0x1CE210),
}


class ResolverError(RuntimeError):
    pass


class IdentityError(ResolverError):
    pass


class MappingError(ResolverError):
    pass


class ReloadDetected(ResolverError):
    pass


@dataclass(frozen=True)
class Identity:
    emulator: str
    image_base: int
    image_size: int
    timestamp: int
    entry_rva: int
    header_sha256: str
    ram_anchor_host: int
    ram_base: int
    ram_end_observed: int
    ram_limit: int
    page_directory_physical: int
    selfmap_pde: int
    source: str

    def stable_key(self):
        return (self.emulator, self.image_base, self.image_size, self.timestamp,
                self.entry_rva, self.header_sha256, self.ram_base,
                self.ram_end_observed, self.selfmap_pde & 0xFFFFF081)


@dataclass(frozen=True)
class Translation:
    guest: int
    pde_host: int
    pde: int
    pte_host: int | None
    pte: int | None
    physical: int
    host: int
    page_size: int

    def mapping_key(self):
        return (self.guest, self.pde_host, self.pde & 0xFFFFF081,
                self.pte_host, None if self.pte is None else self.pte & 0xFFFFF001,
                self.physical, self.host, self.page_size)


def parse_modules(lines: Iterable[str]) -> list[dict]:
    modules = []
    seen = set()
    for line in lines:
        fields = {m.group(1).lower(): (m.group(2) or m.group(3))
                  for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^\s]+))', line)}
        try:
            base = int(fields["base"], 16)
            size = int(fields["size"], 16) if "size" in fields else None
        except (KeyError, ValueError):
            continue
        if not 0 < base < 0x100000000 or base in seen:
            continue
        seen.add(base)
        modules.append({"base": base, "size": size, "name": fields.get("name", "")})
    modules.sort(key=lambda m: (not any(s in m["name"].lower() for s in ("xefu", "xbox")), m["base"]))
    return modules


class Resolver:
    def __init__(self, read: Callable[[int, int], bytes],
                 module_lines: Callable[[], Iterable[str]] | None = None,
                 *, allow_profile_fallback: bool = True, cache: bool = False):
        self._read_adapter = read
        self._module_lines = module_lines
        self.allow_profile_fallback = allow_profile_fallback
        self._pages = {} if cache else None

    def forget(self):
        if self._pages is not None:
            self._pages.clear()

    @classmethod
    def from_client(cls, client, **kwargs):
        return cls(client.read, lambda: client.lines("modules"), **kwargs)

    def read_host(self, address: int, size: int) -> bytes:
        if not isinstance(address, int) or not isinstance(size, int):
            raise ValueError("address and size have to be numbers")
        if size < 0 or size > RAM_SIZE or address < 0 or address + size > 0x100000000:
            raise ValueError("host read is past the 32 bit range or 64 mb limit")
        if size == 0:
            return b""
        try:
            raw = self._read_adapter(address, size)
        except Exception as exc:
            raise ResolverError(f"read failed at host 0x{address:08X}: {exc}") from exc
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != size:
            raise ResolverError(f"bad read at host 0x{address:08X}, wanted {size} bytes")
        return bytes(raw)

    def _u32(self, address: int) -> int:
        return struct.unpack("<I", self.read_host(address, 4))[0]

    def _image(self, base: int, module_size: int | None) -> dict:
        dos = self.read_host(base, 0x40)
        if dos[:2] != b"MZ":
            raise IdentityError("no mz signature")
        pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
        if pe_offset < 0x40 or pe_offset > 0x10000:
            raise IdentityError("pe offset is outside the header")
        if module_size is not None and (module_size <= 0 or pe_offset + 0x60 > module_size):
            raise IdentityError("pe header is outside the module")
        pe = self.read_host(base + pe_offset, 0x60)
        if pe[:4] != b"PE\0\0":
            raise IdentityError("no pe signature")
        machine, = struct.unpack_from("<H", pe, 4)
        opt_size, = struct.unpack_from("<H", pe, 20)
        magic, = struct.unpack_from("<H", pe, 24)
        timestamp, = struct.unpack_from("<I", pe, 8)
        entry, = struct.unpack_from("<I", pe, 0x28)
        size, = struct.unpack_from("<I", pe, 0x50)
        headers_size, = struct.unpack_from("<I", pe, 0x54)
        build = SUPPORTED_BUILDS.get((timestamp, entry))
        if build is None:
            raise IdentityError(f"unknown emulator build {timestamp:08X}/{entry:08X}")
        name, anchor_rva = build
        if machine != 0x1F2 or magic != 0x10B or opt_size < 0x60:
            raise IdentityError(f"weird {name} pe header")
        if not anchor_rva + 8 <= size <= 0x10000000 or base + size > 0x100000000:
            raise IdentityError("ram anchor is outside the image")
        if not pe_offset + 24 + opt_size <= headers_size <= size or entry >= size:
            raise IdentityError("pe headers or entry go past the image")
        return {"image_base": base, "image_size": size, "timestamp": timestamp,
                "entry_rva": entry, "header_sha256": hashlib.sha256(dos + pe).hexdigest(),
                "emulator": name, "anchor_rva": anchor_rva}

    def detect(self, known: Identity | None = None, guess: int | None = None) -> Identity:
        if guess is not None and known is None:
            try:
                return self.detect(known=Identity(emulator="", image_base=guess, image_size=None, timestamp=0,
                                                  entry_rva=0, header_sha256="", ram_anchor_host=0, ram_base=0,
                                                  ram_end_observed=0, ram_limit=0, page_directory_physical=0,
                                                  selfmap_pde=0, source="usual image base"))
            except ResolverError:
                pass
        modules = []
        module_problem = "no module-list adapter"
        if known is not None:
            modules = [{"base": known.image_base, "size": known.image_size, "name": known.source}]
        elif self._module_lines is not None:
            try:
                modules = parse_modules(self._module_lines())
                module_problem = "module list had no usable bases"
            except Exception as exc:
                module_problem = f"module query didnt work: {exc}"
        if known is not None:
            candidates = [(known.image_base, known.image_size, known.source)]
        elif modules:
            candidates = [(m["base"], m["size"], "XBDM modules: " + m["name"]) for m in modules]
        elif self.allow_profile_fallback:
            candidates = [(PROFILE_BASE, None, "verified profile fallback, " + module_problem)]
        else:
            raise IdentityError(module_problem)
        valid = []
        errors = []
        for base, module_size, source in candidates:
            try:
                image = self._image(base, module_size)
                emulator = image.pop("emulator")
                anchor = base + image.pop("anchor_rva")
                anchor_bytes = self.read_host(anchor, 8)
                ram_base, ram_end = struct.unpack(">II", anchor_bytes)
                if not ram_base or ram_base & 0xFFF or ram_base + RAM_SIZE > 0x100000000:
                    raise IdentityError("guest ram base is bad")
                if self.read_host(ram_base + 0x10000, 2) != b"MZ":
                    raise IdentityError("guest ram has no kernel signature")
                selfmap = self._u32(ram_base + PAGE_DIR + 0x300 * 4)
                if selfmap & 0xFFFFF081 != PAGE_DIR | 1:
                    raise IdentityError("page directory self map doesnt point to 0xF000")
                if anchor_bytes != self.read_host(anchor, 8):
                    raise ReloadDetected("guest ram moved while detecting")
                valid.append(Identity(emulator=emulator, **image, ram_anchor_host=anchor,
                                      ram_base=ram_base, ram_end_observed=ram_end,
                                      ram_limit=RAM_SIZE, page_directory_physical=PAGE_DIR,
                                      selfmap_pde=selfmap, source=source))
            except ResolverError as exc:
                errors.append(f"0x{base:08X}: {exc}")
        if len(valid) != 1:
            if valid:
                raise IdentityError("found more than one emulator, not guessing")
            raise IdentityError("no xefu found: " + "; ".join(errors))
        return valid[0]

    def resolve(self, va: int, identity: Identity | None = None) -> Translation:
        if not isinstance(va, int) or not 0 <= va <= 0xFFFFFFFF:
            raise ValueError("guest address has to be a 32 bit number")
        ident = identity if identity is not None else self.detect()
        if self._pages is not None:
            page = self._pages.get(va >> 12) or self._pages.get(("large", va >> 22))
            if page is not None and page[0] == ident.ram_base:
                t = page[1]
                offset = va & (t.page_size - 1)
                return Translation(va, t.pde_host, t.pde, t.pte_host, t.pte, t.physical + offset,
                                   t.host + offset, t.page_size)
        pde_host = ident.ram_base + PAGE_DIR + (va >> 22) * 4
        pde = self._u32(pde_host)
        if not pde & 1:
            raise MappingError(f"guest 0x{va:08X}: PDE not present (0x{pde:08X})")
        pte_host = pte = None
        if pde & 0x80:
            if pde & 0x003FE000:
                raise MappingError("large page pde has weird bits")
            physical = (pde & 0xFFC00000) | (va & 0x3FFFFF)
            page_size = 0x400000
        else:
            table = pde & 0xFFFFF000
            if table + 0x1000 > RAM_SIZE:
                raise MappingError(f"page table 0x{table:08X} is outside guest ram")
            pte_host = ident.ram_base + table + ((va >> 12) & 0x3FF) * 4
            pte = self._u32(pte_host)
            if not pte & 1:
                raise MappingError(f"guest 0x{va:08X}: PTE not present (0x{pte:08X})")
            physical = (pte & 0xFFFFF000) | (va & 0xFFF)
            page_size = 0x1000
        if physical >= RAM_SIZE:
            raise MappingError(f"physical 0x{physical:08X} is outside the 64 mb guest ram")
        if self._pages is not None:
            mask = page_size - 1
            base = physical & ~mask
            key = ("large", va >> 22) if page_size == 0x400000 else va >> 12
            self._pages[key] = (ident.ram_base, Translation(va & ~mask, pde_host, pde, pte_host, pte, base,
                                                            ident.ram_base + base, page_size))
        return Translation(va, pde_host, pde, pte_host, pte, physical,
                           ident.ram_base + physical, page_size)

    def _assert_mapping(self, translation: Translation, identity: Identity):
        try:
            now = self.resolve(translation.guest, identity)
        except MappingError as exc:
            raise ReloadDetected("guest mapping went away during the read") from exc
        if now.mapping_key() != translation.mapping_key():
            raise ReloadDetected(f"guest 0x{translation.guest:08X} mapping changed during read")

    def read_guest(self, va: int, size: int, identity: Identity | None = None,
                   *, mappings: list[Translation] | None = None) -> bytes:
        if not 0 <= size <= RAM_SIZE or va < 0 or va + size > 0x100000000:
            raise ValueError("guest read is past the 32 bit range or 64 mb limit")
        own_identity = identity is None
        ident = self.detect() if own_identity else identity
        out = bytearray()
        cursor = va
        while cursor < va + size:
            translation = self.resolve(cursor, ident)
            count = min(0x1000 - (cursor & 0xFFF), va + size - cursor)
            out += self.read_host(translation.host, count)
            if self._pages is None:
                self._assert_mapping(translation, ident)
            if mappings is not None:
                mappings.append(translation)
            cursor += count
        if own_identity:
            self.assert_identity(ident)
        return bytes(out)

    def assert_identity(self, before: Identity) -> Identity:
        try:
            after = self.detect(known=before)
        except ResolverError as exc:
            raise ReloadDetected("couldnt recheck the emulator") from exc
        if before.stable_key() != after.stable_key():
            raise ReloadDetected("emulator or guest ram changed while working")
        return after

    def follow_pointer(self, va: int, offset: int = 0, identity: Identity | None = None,
                       *, mappings: list[Translation] | None = None) -> dict:
        own_identity = identity is None
        ident = self.detect() if own_identity else identity
        pointer = struct.unpack("<I", self.read_guest(va, 4, ident, mappings=mappings))[0]
        result = {"pointer_guest": va, "pointer_value": pointer, "offset": offset, "target": None}
        if pointer:
            target = pointer + offset
            if not 0 <= target <= 0xFFFFFFFF:
                raise MappingError("pointer plus offset is past the end of memory")
            translation = self.resolve(target, ident)
            result["target"] = asdict(translation)
            if mappings is not None:
                mappings.append(translation)
        if own_identity:
            self.assert_identity(ident)
        return result

    def snapshot(self, addresses: Iterable[int], pointer_addresses: Iterable[int] = ()) -> dict:
        before = self.detect()
        mappings = []
        rows = []
        for va in addresses:
            try:
                row_mappings = []
                raw = self.read_guest(va, 4, before, mappings=row_mappings)
                mappings.extend(row_mappings)
                rows.append({**asdict(row_mappings[0]), "bytes": raw.hex(),
                             "value_u32_le": struct.unpack("<I", raw)[0]})
            except ReloadDetected:
                raise
            except MappingError as exc:
                rows.append({"guest": va, "error": str(exc)})
        pointers = []
        for va in pointer_addresses:
            try:
                pointers.append(self.follow_pointer(va, identity=before, mappings=mappings))
            except ReloadDetected:
                raise
            except MappingError as exc:
                pointers.append({"pointer_guest": va, "error": str(exc)})
        after = self.assert_identity(before)
        for translation in mappings:
            self._assert_mapping(translation, after)
        self.assert_identity(after)
        return {"identity": asdict(before), "identity_after": asdict(after),
                "addresses": rows, "pointers": pointers,
                "consistency": "nothing changed while reading, not a perfect snapshot though",
                "ram_end_note": "end is what was seen, bounds use the 64 mb profile"}


def _hex_address(value):
    try:
        number = int(value, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("needs a hex address") from exc
    if not 0 <= number <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("address has to fit in 32 bits")
    return number
