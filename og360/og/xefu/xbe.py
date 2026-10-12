import hashlib
import struct


def metadata(read, base=0x10000):
    head = read(base, 0x178)
    if head[:4] != b'XBEH':
        raise ValueError("no og xbox xbe at 0x10000")
    actual, headers, image, fixed, stamp, cert, count, table = struct.unpack_from('<8I', head, 0x104)
    if actual != base or not 0x178 <= headers <= 0x100000 or not 0 < count <= 1024:
        raise ValueError("bad xbe header bounds")
    if not base <= cert <= base + headers - 12 or not base <= table <= base + headers - count * 56:
        raise ValueError("xbe structures are outside the headers")
    certificate = read(cert, 12)
    title_id = struct.unpack_from('<I', certificate, 8)[0]
    title = read(cert + 12, 80).decode('utf-16-le', errors='replace').split('\0', 1)[0] if cert + 92 <= base + headers else ''
    rows = read(table, count * 56)
    sections = []
    for i in range(count):
        flags, address, size, offset, raw_size = struct.unpack_from('<5I', rows, i * 56)
        if size and (address < base or address + size > 0x100000000 or raw_size > size):
            raise ValueError("bad xbe section bounds")
        sections.append({'flags': flags, 'address': address, 'size': size,
                         'file_offset': offset, 'raw_size': raw_size, 'executable': bool(flags & 4)})
    full_header = read(base, headers)
    normalized = bytearray(full_header)
    mutable = [0x128, 0x158, cert - base + 0x9C]
    for i in range(count):
        at = table - base + i * 56
        mutable.append(at + 24)
        for offset in (28, 32):
            pointer = struct.unpack_from('<I', rows, i * 56 + offset)[0]
            if base <= pointer <= base + headers - 4:
                mutable.append(pointer - base)
    for at in mutable:
        if not 0 <= at <= headers - 4:
            raise ValueError("xbe field is outside the header")
        normalized[at:at + 4] = bytes(4)
    return {'base': base, 'headers_size': headers, 'image_size': image,
            'timestamp': stamp, 'title_id': title_id, 'title': title,
            'header_sha256': hashlib.sha256(full_header).hexdigest(),
            'identity_sha256': hashlib.sha256(normalized).hexdigest(), 'sections': sections,
            'header_used_end': _header_used_end(full_header, base, headers, rows, count)}


def _header_used_end(header, base, headers, rows, count):
    end = base + struct.unpack_from('<I', header, 0x110)[0]
    limit = base + headers
    u32 = lambda o: struct.unpack_from('<I', header, o)[0]

    def inside(va, size=1):
        return base <= va and va + size <= limit

    def string_end(va, wide=False):
        o = va - base
        step = 2 if wide else 1
        while o + step <= headers and header[o:o + step] != bytes(step):
            o += step
        return base + o + step

    spans = [(u32(0x118), u32(u32(0x118) - base) if inside(u32(0x118), 4) else 0),
             (u32(0x120), count * 56),
             (u32(0x164), u32(0x160) * 16),
             (u32(0x168), 16), (u32(0x16C), 16),
             (u32(0x170), u32(0x174)),
             (u32(0x12C), 24)]
    for i in range(count):
        name = struct.unpack_from('<I', rows, i * 56 + 20)[0]
        head_ref, tail_ref = struct.unpack_from('<II', rows, i * 56 + 28)
        if inside(name):
            end = max(end, string_end(name))
        spans += [(head_ref, 2), (tail_ref, 2)]
    for va, size in spans:
        if size and inside(va, size):
            end = max(end, va + size)
    for va, wide in ((u32(0x14C), False), (u32(0x150), False), (u32(0x154), True)):
        if inside(va):
            end = max(end, string_end(va, wide))
    return end


class Xbe:
    def __init__(self, raw):
        self.raw = bytes(raw)
        if len(self.raw) < 0x178:
            raise ValueError("xbe is cut off")
        self.info = metadata(self.read)
        self.sha256 = hashlib.sha256(self.raw).hexdigest()

    def read(self, address, size):
        base = 0x10000
        headers = struct.unpack_from('<I', self.raw, 0x108)[0]
        if base <= address and address + size <= base + headers:
            offset = address - base
        else:
            info = getattr(self, 'info', None)
            if info is None:
                raise ValueError("metadata read is outside the xbe headers")
            section = next((s for s in info['sections'] if s['address'] <= address
                            and address + size <= s['address'] + s['raw_size']), None)
            if section is None:
                raise ValueError("function bytes arent in an xbe section")
            offset = section['file_offset'] + address - section['address']
        if offset < 0 or offset + size > len(self.raw):
            raise ValueError("xbe range is cut off")
        return self.raw[offset:offset + size]


def scratch_candidates(info, size=128):
    start = (info['base'] + info['headers_size'] + 15) & ~15
    end = (info['base'] + info['headers_size'] + 4095) & ~4095
    end = min([end] + [s['address'] for s in info['sections'] if s['size'] and s['address'] >= start])
    slack = list(range(start, end - size + 1, 16))
    used = info.get('header_used_end')
    if used is None:
        return slack
    padding = range((used + 15) & ~15, info['base'] + info['headers_size'] - size + 1, 16)
    return slack + list(padding)
