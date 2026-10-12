#!/usr/bin/env python3
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import struct
import sys

from .resolve import Resolver, ResolverError
from .xbe import metadata


KERNEL_BASE = 0x80010000


def discover(resolver, ident=None, info=None):
    ident = ident or resolver.detect()
    if ident.emulator != 'xefu7':
        return derived_ranges(resolver, ident, info)
    return _discover_xefu7(resolver, ident)


def derived_ranges(resolver, ident, info=None):
    read = lambda va, n: resolver.read_guest(va, n, ident)
    xbe = info or metadata(read)
    rows = [[s['address'], s['address'] + s['size'], 0] for s in xbe['sections'] if s['size']]
    dos = read(KERNEL_BASE, 0x40)
    if dos[:2] != b'MZ':
        raise ResolverError("guest kernel has no mz header")
    pe = struct.unpack_from('<I', dos, 0x3C)[0]
    head = read(KERNEL_BASE + pe, 0x18)
    if head[:4] != b'PE\0\0' or struct.unpack_from('<H', head, 4)[0] != 0x14C:
        raise ResolverError("guest kernel has no pe header")
    count, opt = struct.unpack_from('<H', head, 6)[0], struct.unpack_from('<H', head, 20)[0]
    table = read(KERNEL_BASE + pe + 0x18 + opt, count * 40)
    for i in range(count):
        vsize, va = struct.unpack_from('<II', table, i * 40 + 8)
        flags = struct.unpack_from('<I', table, i * 40 + 36)[0]
        if flags & 0x20000020 and vsize:
            rows.append([KERNEL_BASE + va, KERNEL_BASE + va + vsize, 0])
    resolver.assert_identity(ident)
    return {'identity': asdict(ident), 'compile_range_rows': rows,
            'source': "from the guest xbe and kernel headers",
            'note': f'{ident.emulator} has no xefu7 style compile range table, rows copy what xefu7 keeps'}


def _discover_xefu7(resolver, ident):
    base = ident.image_base
    be = lambda a: struct.unpack('>I', resolver.read_host(a, 4))[0]
    pool = be(base + 0x188CC0)
    if not pool or pool & 3:
        raise ResolverError("bad runtime object pointer")
    header = resolver.read_host(pool, 0xC0)
    field = lambda o: struct.unpack_from('>I', header, o)[0]
    if field(0) != pool or field(0x7C) != ident.ram_base:
        raise ResolverError("runtime object doesnt match the ram anchor")
    backing, executable, bias = field(0x98), field(0xA0), field(0x88)
    if not backing or executable != (backing - bias) & 0xFFFFFFFF:
        raise ResolverError("helper alias check failed")
    if backing + 0x9000 != field(0x9C) or executable + 0x9000 != field(0xA4):
        raise ResolverError("weird helper block bounds")
    offsets = struct.unpack('>248H', resolver.read_host(base + 0x168D18, 496))
    valid_offsets = [o for i, o in enumerate(offsets) if i == 0 or o != 0]
    table = struct.unpack('>' + str(len(valid_offsets)) + 'I',
                          resolver.read_host(pool + 0x530, len(valid_offsets) * 4))
    if table != tuple((executable + o) & 0xFFFFFFFF for o in valid_offsets):
        raise ResolverError("runtime helper table doesnt match")
    helpers = {}
    for name, offset in (('Dispatch', 0xD0), ('Compile', 0x1C0), ('RetGlue', 0x348)):
        original = resolver.read_host(base + 0x160318 + offset, 32)
        copy = resolver.read_host(backing + offset, 32)
        if copy != original:
            raise ResolverError(f'{name} bytes dont match the helper')
        helpers[name] = {'source_host': base + 0x160318 + offset,
                         'readable_host': backing + offset,
                         'execution_alias': executable + offset,
                         'offset': offset, 'matched_bytes': copy.hex()}
    worker = be(base + 0x188CB8)
    worker_bytes = resolver.read_host(worker, 0xE0)
    wf = lambda o: struct.unpack_from('>I', worker_bytes, o)[0]
    if wf(0) != pool:
        raise ResolverError("compile worker doesnt point at the runtime object")
    range_object = wf(0x20)
    range_header = resolver.read_host(range_object, 0x10)
    vtable, entries, capacity, count = struct.unpack('>4I', range_header)
    if count > 4096 or capacity < count:
        raise ResolverError("compile range table has bad bounds")
    raw = resolver.read_host(entries, count * 12)
    rows = [list(struct.unpack_from('>3I', raw, i * 12)) for i in range(count)]
    comparator = be(vtable)
    if be(base + 0x188CC0) != pool or be(base + 0x188CB8) != worker:
        raise ResolverError("runtime object changed while looking")
    resolver.assert_identity(ident)
    return {'identity': asdict(ident), 'runtime_object': pool,
            'helper_backing': backing, 'helper_execution_alias': executable,
            'alias_bias': bias, 'helper_table_verified_entries': len(table),
            'helpers': helpers, 'compile_worker': worker,
            'compile_range_object': range_object, 'compile_range_vtable': vtable,
            'compile_range_comparator': comparator,
            'compile_range_entries': entries, 'compile_range_count': count,
            'compile_range_capacity': capacity, 'compile_range_rows': rows,
            'compile_worker_bytes': worker_bytes.hex(),
            'note': "addresses are found again each time. execution aliases might not read through xbdm, read the backing bytes instead"}
