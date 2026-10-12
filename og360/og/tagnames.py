import os
import struct

HEADER_SIZE = 0x800
LAYOUTS = {
    "halo2": (16, 0, 4, 8),
    "halo1": (0x20, 0, 0xC, 0x14),
}
SEARCH_ROOTS = ("~/Desktop", "~/Downloads", "~/Documents", "~")
_cache = {}


def map_header(read, address):
    head = read(address, HEADER_SIZE)
    if head[:4] != b"daeh" or head[0x7FC:0x800] != b"toof":
        raise ValueError("no map header at %08X" % address)
    path = head[0x20:0x120].split(b"\0")[0].decode("latin-1")
    return path.replace("/", "\\").rsplit("\\", 1)[-1], head[0x1BC:0x2BC].split(b"\0")[0].decode("latin-1")


def names_from_file(path):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime)
    if key not in _cache:
        with open(path, "rb") as f:
            head = f.read(HEADER_SIZE)
            if head[:4] != b"daeh":
                raise ValueError("%s isnt a halo 2 map file" % path)
            count, table_at, table_size, index_at = struct.unpack_from("<4I", head, 0x2C0)
            if not 0 < count < 0x10000 or table_size > 0x1000000:
                raise ValueError("%s has no tag name table" % path)
            f.seek(index_at)
            index = struct.unpack("<%dI" % count, f.read(4 * count))
            f.seek(table_at)
            table = f.read(table_size)
        names = []
        for at in index:
            end = table.find(b"\0", at)
            names.append(table[at:end if end >= 0 else len(table)].decode("latin-1") if at < len(table) else "")
        _cache.clear()
        _cache[key] = names
    return _cache[key]


def find_map_file(map_file, folders=(), depth=4):
    for folder in folders:
        for candidate in (os.path.join(folder, "maps", map_file), os.path.join(folder, map_file)):
            if os.path.isfile(candidate):
                return candidate
    for root in SEARCH_ROOTS:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        base = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - base >= depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("Library", "node_modules")]
            if os.path.basename(dirpath).lower() == "maps" and map_file in filenames:
                return os.path.join(dirpath, map_file)
    return None


def loaded_tags(read, table, groups, count=0x2400, layout="halo2"):
    size, at_group, at_datum, at_addr = LAYOUTS[layout]
    raw = read(table, size * count)
    wanted = {struct.unpack(">I", g.encode("latin-1"))[0]: g for g in groups}
    out = []
    for k in range(count):
        group, datum, addr = (struct.unpack_from("<I", raw, k * size + at)[0] for at in (at_group, at_datum, at_addr))
        if group in wanted and (datum & 0xFFFF) == k and addr:
            out.append((datum, wanted[group], addr))
    return out


def is_loaded(read, table, datum, group, layout="halo2"):
    size, at_group, at_datum, _ = LAYOUTS[layout]
    entry = read(table + (datum & 0xFFFF) * size, size)
    g, d = struct.unpack_from("<I", entry, at_group)[0], struct.unpack_from("<I", entry, at_datum)[0]
    return d == datum and g == struct.unpack(">I", group.encode("latin-1"))[0]


def names_from_memory(read, table, count):
    raw = read(table, 0x20 * count)
    pointers = [struct.unpack_from("<I", raw, k * 0x20 + 0x10)[0] for k in range(count)]
    used = [p for p in pointers if p]
    if not used:
        return []
    low, high = min(used), max(used) + 0x100
    if high - low > 0x400000:
        raise ValueError("the tag names arent where halo 1 keeps them")
    blob = read(low, high - low)
    names = []
    for p in pointers:
        if not p:
            names.append("")
            continue
        end = blob.find(b"\0", p - low)
        names.append(blob[p - low:end if end >= 0 else len(blob)].decode("latin-1"))
    return names
