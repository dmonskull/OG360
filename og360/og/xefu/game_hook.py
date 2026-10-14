import datetime
import hashlib
import json
from pathlib import Path
import struct
import time

from . import kernel_apc
from .kernel_apc import (EDX_OUT, FLOAT_OUT, NO_FLOAT, REAL_TARGET, REGISTERS, STORAGE_SIZE, TEXTS, WIDE_TEXT,
                         SafePointTimeout)
from .resolve import PROFILE_BASE, MappingError, Resolver
from .runtime_discover import discover
from .xbe import metadata, scratch_candidates

HOOKS = Path(__file__).parent / 'hooks'
MAIN, ENTRIES, MAIN_ENTRIES, LAST_THREAD, PENDING, RUNNING = 0x0, 0x4, 0x8, 0xC, 0x10, 0x14
MAILBOX_SIZE = 0x18
HOOK_WAIT = 3.0
COMPLETED = 0x0C


class Unavailable(RuntimeError):
    pass


NO_HOOK = 'this game has no hook'


def profile_for(title_id):
    path = HOOKS / ('%08X.json' % title_id)
    return json.loads(path.read_text()) if path.is_file() else None


def _u32(v):
    return struct.pack('<I', v & 0xFFFFFFFF)


def hook_units(mailbox, original):
    return [
        b'\x9c',
        b'\x60',
        b'\xfc',
        b'\x64\xa1\x28\x00\x00\x00',
        b'\xbb' + _u32(mailbox),
        b'\xff\x43' + bytes([ENTRIES]),
        b'\x89\x43' + bytes([LAST_THREAD]),
        b'\x3b\x03',
        ('jcc', 0x85, 'done'),
        b'\xff\x43' + bytes([MAIN_ENTRIES]),
        b'\x33\xc9',
        b'\x87\x4b' + bytes([PENDING]),
        b'\x85\xc9',
        ('jcc', 0x84, 'done'),
        b'\x89\x4b' + bytes([RUNNING]),
        b'\x8b\xd9',
        b'\x8b\xec',
        b'\xdf\xe0',
        b'\x89\x43\x6c',
        b'\x83\xec\x18',
        b'\x8b\xfc',
        b'\x8d\x73\x10',
        b'\x6a\x06', b'\x59',
        b'\xf3\xa5',
        b'\x8b\x43\x2c', b'\x8b\x4b\x30', b'\x8b\x53\x34', b'\x8b\x73\x38', b'\x8b\x7b\x3c',
        b'\xff\x53\x28',
        b'\x8b\xe5',
        b'\x89\x43\x08',
        b'\x89\x53\x68',
        b'\xdf\xe0',
        b'\x32\x63\x6d',
        b'\x80\xe4\x38',
        b'\x74\x03\xdd\x5b\x70',
        b'\xff\x43' + bytes([COMPLETED]),
        b'\xbb' + _u32(mailbox),
        b'\x83\x63' + bytes([RUNNING]) + b'\x00',
        ('label', 'done'),
        b'\x61',
        b'\x9d',
        ('jmp', original),
    ]


def _size(unit):
    return 0 if isinstance(unit, tuple) and unit[0] == 'label' else 6 if isinstance(unit, tuple) and unit[0] == 'jcc' \
        else 5 if isinstance(unit, tuple) else len(unit)


def layout(units, blocks):
    placed, at = [], {}
    used = []
    pending = list(units)
    for n, b in enumerate(blocks):
        room = len(bytes.fromhex(b['original']))
        nxt = blocks[n + 1]['address'] if n + 1 < len(blocks) else None
        link = 0 if nxt is None else 2 if -128 + room <= nxt - (b['address'] + room) <= 127 - room else 5
        off = 0
        while pending:
            size = _size(pending[0])
            rest = sum(_size(u) for u in pending)
            if off + rest <= room:
                link_here = 0
            else:
                link_here = link
            if off + size + link_here > room or (nxt is None and off + size > room):
                break
            u = pending.pop(0)
            if isinstance(u, tuple) and u[0] == 'label':
                at[u[1]] = b['address'] + off
            placed.append((n, off, u))
            off += size
        used.append(off)
        if not pending:
            break
        if not off or nxt is None:
            raise RuntimeError("hook doesnt fit its padding blocks")
    out = []
    for n in range(len(used)):
        b = blocks[n]
        raw = bytearray()
        for bn, off, u in placed:
            if bn != n:
                continue
            here = b['address'] + off
            if isinstance(u, bytes):
                raw += u
            elif u[0] == 'jcc':
                raw += bytes([0x0F, u[1]]) + struct.pack('<i', at[u[2]] - (here + 6))
            elif u[0] == 'jmp':
                raw += b'\xe9' + struct.pack('<i', u[1] - (here + 5))
        if n + 1 < len(used):
            nxt = blocks[n + 1]['address']
            here = b['address'] + len(raw)
            if -128 <= nxt - (here + 2) <= 127 and len(raw) + 2 <= len(bytes.fromhex(b['original'])):
                raw += b'\xeb' + struct.pack('<b', nxt - (here + 2))
            else:
                raw += b'\xe9' + struct.pack('<i', nxt - (here + 5))
        if len(raw) > len(bytes.fromhex(b['original'])):
            raise RuntimeError("hook doesnt fit its padding blocks")
        out.append(bytes(raw))
    return out


def hook_code(h):
    return layout(hook_units(h['mailbox']['address'], h['original']), h['hook']['blocks'])


def digest(h):
    return hashlib.sha256(b''.join(_u32(b['address']) + c for b, c in zip(h['hook']['blocks'], hook_code(h)))).hexdigest()


def locate(read, h):
    spec = h['pointer']
    if 'address' in spec:
        return spec['address']
    path = spec['path']
    slot = path[0]
    for off in path[1:]:
        base = struct.unpack('<I', read(slot, 4))[0]
        if not 0x10000 <= base < 0x80000000:
            raise Unavailable("hook pointer isnt there right now (%s)" % h.get('point', 'its object is missing'))
        slot = base + off
    return slot


def call(client, target, arguments=(), *, registers=None, string=None, scratch=None, report_path=None,
         before_publish=None, notify=None, title_id=None):
    registers = dict(registers or {})
    if len(arguments) > 6 or any(a not in TEXTS and (type(a) is not int or not 0 <= a <= 0xFFFFFFFF) for a in arguments):
        raise ValueError("use 0 to 6 stack args (32 bit)")
    if set(registers) - set(REGISTERS) or any(v not in TEXTS and (type(v) is not int or not 0 <= v <= 0xFFFFFFFF)
                                              for v in registers.values()):
        raise ValueError("registers are eax, ecx, edx, esi and edi with 32 bit values, @string or @wstring")
    used = set(TEXTS) & set(list(arguments) + list(registers.values()))
    if len(used) > 1:
        raise ValueError("use @string or @wstring, not both (they share the text)")
    if used and string is None:
        raise ValueError("%s needs text" % used.pop())
    raw_string = None
    if string is not None:
        raw_string = string.encode('utf-16-le') + bytes(2) if WIDE_TEXT in used else string.encode('ascii') + bytes(1)
        if len(raw_string) > 64:
            raise ValueError("text is max 31 characters for @wstring" if WIDE_TEXT in used else
                             "text is max 63 characters")
    r = Resolver.from_client(client, cache=True)
    ident = r.detect(guess=PROFILE_BASE)
    read = lambda a, n: r.read_guest(a, n, ident)
    u32 = lambda a: struct.unpack('<I', read(a, 4))[0]
    info = metadata(read)
    if title_id is not None and info['title_id'] != title_id:
        raise Unavailable('a different game is running')
    h = profile_for(info['title_id'])
    if h is None:
        raise Unavailable(NO_HOOK)
    if info['timestamp'] != h['xbe_timestamp'] or any(
            hashlib.sha256(read(c['address'], c['size'])).hexdigest() != c['sha256'] for c in h['checks']):
        raise Unavailable("this copy of the game isnt the xbe its hook was made for")
    p = kernel_apc.profile_data(ident.emulator)
    code = hook_code(h)
    if digest(h) != h['hook_sha256']:
        raise RuntimeError("hook code changed, cant reuse it")
    blocks = h['hook']['blocks'][:len(code)]
    hook, mailbox, original = blocks[0]['address'], h['mailbox']['address'], h['original']
    spans = [(b['address'], b['address'] + len(bytes.fromhex(b['original']))) for b in blocks]
    report = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'backend': 'game hook (%s)' % h['name'], 'emulator': ident.emulator, 'target': target,
              'arguments': list(arguments), 'registers': registers, 'stack_slots': 6, 'hook': hook,
              'hook_sha256': h['hook_sha256'], 'mailbox': mailbox, 'samples': [], 'success': False}
    held = False
    published = storage_owned = False
    request = slot = None

    def say(text):
        if notify:
            try:
                notify(text)
            except Exception:
                pass

    def save():
        if report_path:
            out = Path(report_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2) + '\n')

    def write(a, raw):
        if not held or (a & 4095) + len(raw) > 4096:
            raise RuntimeError("write needs the game stopped and one page")
        client.write(r.resolve(a, ident).host, raw)
        if read(a, len(raw)) != raw:
            raise RuntimeError(f'write didnt stick at {a:08X}')

    def put(a, value):
        write(a, _u32(value))

    def header_unchanged():
        raw = bytearray(read(info['base'], info['headers_size']))
        if request is not None and info['base'] <= request < info['base'] + info['headers_size']:
            at = request - info['base']
            raw[at:at + STORAGE_SIZE] = bytes(min(STORAGE_SIZE, len(raw) - at))
        return hashlib.sha256(bytes(raw)).hexdigest() == info['header_sha256']

    def stop():
        nonlocal held
        client.command('stop')
        held = True
        r.forget()
        r.assert_identity(ident)
        if not header_unchanged():
            raise RuntimeError("the game changed")

    def go():
        nonlocal held
        client.command('go')
        held = False

    def box():
        return dict(zip(('main', 'entries', 'main_entries', 'last_thread', 'pending', 'running'),
                        struct.unpack('<6I', read(mailbox, MAILBOX_SIZE))))

    try:
        ticks = [u32(p['tick'])]
        time.sleep(0.05)
        ticks.append(u32(p['tick']))
        report['ticks_before'] = ticks
        if ticks[0] == ticks[1]:
            raise RuntimeError("the game is paused, unpause it first")
        main, why = kernel_apc.main_thread(read, p)
        if main is None:
            raise Unavailable("cant find its main thread (%s)" % why)
        report['main_thread'] = main
        stop()
        kpcr = p['irql'] - 0x24
        if struct.unpack('<2I', read(kpcr + 0x1C, 8)) != (kpcr, p['current_thread']):
            raise RuntimeError("kernel doesnt match the %s profile (kpcr)" % ident.emulator)
        runtime = discover(r, ident, info)
        ranges = runtime['compile_range_rows']
        if not any(lo <= target < hi for lo, hi, _ in ranges):
            raise RuntimeError("target isnt in the code ranges")
        if not all(any(lo <= a and e <= hi for lo, hi, _ in ranges) for a, e in spans):
            raise RuntimeError("hook padding isnt in the code ranges")
        if any(a <= target < e for a, e in spans):
            raise ValueError('Target overlaps the hook')

        def installed(at, raw):
            raw = bytearray(raw)
            for b, c in zip(blocks, code):
                lo, hi = max(at, b['address']), min(at + len(raw), b['address'] + len(c))
                if lo < hi:
                    raw[lo - at:hi - at] = c[lo - b['address']:hi - b['address']]
            return bytes(raw)

        states = set()
        for b, c in zip(blocks, code):
            original_bytes = bytes.fromhex(b['original'])
            owner = bytes.fromhex(b['preceding_bytes'])
            if read(b['preceding_address'], len(owner)) not in (owner, installed(b['preceding_address'], owner)):
                raise Unavailable("code before the hook padding at %08X is different" % b['address'])
            now = read(b['address'], len(original_bytes))
            states.add('fresh' if now == original_bytes else 'ours' if now == c + original_bytes[len(c):] else 'other')
        if states not in ({'fresh'}, {'ours'}):
            raise Unavailable("hook padding has something else in it")
        fresh = states == {'fresh'}
        page = r.resolve(mailbox, ident)
        if not (page.pte if page.pte is not None else page.pde) & 2:
            raise Unavailable("hook mailbox page isnt writable")
        raw_box = read(mailbox, MAILBOX_SIZE)
        clean = raw_box in (bytes(MAILBOX_SIZE), bytes.fromhex(h['mailbox'].get('first_run', '00' * MAILBOX_SIZE)))
        mb = box()
        if not clean:
            if mb['pending'] or mb['running']:
                raise Unavailable("hook mailbox is busy (a call is still queued or running)")
            if mb['main'] != main and h.get('thread') != 'hook':
                raise Unavailable("hook mailbox has something weird in it")
        slot = locate(read, h)
        held_value = u32(slot)
        if held_value not in (original, hook):
            raise Unavailable("hook pointer at %08X is %08X, not %08X (%s)"
                              % (slot, held_value, original, h.get('point', '')))
        report['pointer'] = slot
        report['mailbox_before'] = mb
        candidates = [scratch] if scratch is not None else scratch_candidates(info, STORAGE_SIZE)
        for candidate in candidates:
            if type(candidate) is not int or candidate & 15 or candidate + STORAGE_SIZE > 0x100000000:
                raise ValueError("scratch has to be 16 byte aligned")
            if (candidate & 4095) + STORAGE_SIZE > 4096:
                continue
            if candidate <= target < candidate + STORAGE_SIZE or any(
                    candidate < e and a < candidate + STORAGE_SIZE for a, e in spans + [(mailbox, mailbox + MAILBOX_SIZE)]):
                raise ValueError("scratch overlaps the target, hook or mailbox")
            if read(candidate, STORAGE_SIZE) == bytes(STORAGE_SIZE):
                request = candidate
                break
        if request is None:
            raise Unavailable("no free space in the xbe header for the request")
        if before_publish:
            before_publish(r, ident, request)
        report['request'] = request
        data = bytearray(STORAGE_SIZE)
        slots = [request + 0x80 if a in TEXTS else a for a in arguments]
        report['resolved_arguments'] = slots
        loaded = [request + 0x80 if registers.get(n) in TEXTS else registers.get(n, 0) for n in REGISTERS]
        report['resolved_registers'] = dict(zip(REGISTERS, loaded))
        struct.pack_into('<4I6I', data, 0, 0, len(arguments), 0xDEADBEEF, 0, *(slots + [0] * (6 - len(arguments))))
        struct.pack_into('<6I', data, REAL_TARGET, target, *loaded)
        struct.pack_into('<I', data, EDX_OUT, 0xDEADBEEF)
        data[FLOAT_OUT:FLOAT_OUT + 8] = NO_FLOAT
        if raw_string is not None:
            data[0x80:0x80 + len(raw_string)] = raw_string
        say("game stopped, hook %s at %08X, pointer %08X, request at %08X, main thread %08X" % (
            "to put in" if fresh else 'in place', hook, slot, request, main))
        storage_owned = True
        write(request, data)
        if fresh:
            for b, c in zip(blocks, code):
                write(b['address'], c)
        if h.get('thread') == 'hook':
            write(mailbox, struct.pack('<6I', 0, 0, 0, 0, 0, 0))
            if held_value != hook:
                put(slot, hook)
                held_value = hook
            go()
            wait = float(h.get('wait', HOOK_WAIT))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and box()['entries'] < 3:
                time.sleep(0.02)
            stop()
            seen = box()
            report['learned'] = seen
            if seen['entries'] < 3 or not seen['last_thread']:
                raise SafePointTimeout("%s wasnt reached in %g s, nothing ran" % (h.get('point', 'the hook point'), wait))
            main = report['main_thread'] = seen['last_thread']
            clean = False
        if clean:
            write(mailbox, struct.pack('<6I', main, 0, 0, 0, 0, 0))
        else:
            put(mailbox + MAIN, main)
        published = True
        put(mailbox + PENDING, request)
        if held_value != hook:
            put(slot, hook)
        report['published'] = box()
        save()
        go()
        say("game resumed, waiting for the main thread to reach %s" % h['point'])
        wait = float(h.get('wait', HOOK_WAIT))
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            done = struct.unpack('<I', read(request + COMPLETED, 4))[0]
            mb = box()
            report['samples'].append({'completed': done, 'pending': mb['pending'], 'running': mb['running']})
            if done and not mb['running'] and not mb['pending']:
                break
            time.sleep(0.02)
        stop()
        mb = box()
        report['after'] = mb
        done = struct.unpack('<I', read(request + COMPLETED, 4))[0]
        if mb['pending'] == request:
            put(mailbox + PENDING, 0)
            report['withdrawn'] = True
            raise SafePointTimeout("main thread didnt reach the hook in %g s, call pulled without running"
                                   % wait)
        if mb['running']:
            raise RuntimeError("call is still running in the hook, left it")
        if done != 1 or mb['pending']:
            raise RuntimeError("hook call didnt finish exactly once")
        out = read(request, STORAGE_SIZE)
        report['eax'] = struct.unpack_from('<I', out, 8)[0]
        report['edx'] = struct.unpack_from('<I', out, EDX_OUT)[0]
        report['float'] = None if out[FLOAT_OUT:FLOAT_OUT + 8] == NO_FLOAT else struct.unpack_from('<d', out, FLOAT_OUT)[0]
        report['timing'] = "safe spot: %s, main thread %08X (game hook)" % (h['point'], main)
        say("done at the hook: eax = %d (0x%08X), edx = 0x%08X%s" % (
            report['eax'], report['eax'], report['edx'],
            '' if report['float'] is None else ", float %r" % report['float']))
        report['frames_seen'] = (mb['main_entries'] - report['published']['main_entries']) & 0xFFFFFFFF
        report['success'] = True
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        try:
            if storage_owned or published:
                if not held:
                    stop()
                mb = box()
                if published and mb['pending'] == request:
                    put(mailbox + PENDING, 0)
                    report['withdrawn'] = True
                if mb['running']:
                    raise RuntimeError("call is still running in the hook, left it")
                cleanup = {}
                if slot is not None:
                    try:
                        here = locate(read, h)
                    except (Unavailable, MappingError):
                        here = None
                    if here is not None and u32(here) == hook:
                        put(here, original)
                        cleanup['pointer_restored'] = True
                    else:
                        cleanup['pointer_restored'] = False
                        cleanup['pointer_note'] = "the game already replaced the pointer (nothing to put back)"
                write(request, bytes(STORAGE_SIZE))
                cleanup['request_zero'] = read(request, STORAGE_SIZE) == bytes(STORAGE_SIZE)
                report['cleanup'] = cleanup
                say("pointer %s, request %s" % (
                    'put back' if cleanup.get('pointer_restored') else "already the games own",
                    'cleared' if cleanup['request_zero'] else "not cleared"))
        except BaseException as exc:
            report['cleanup_error'] = repr(exc)
            report['success'] = False
        try:
            if held:
                go()
            if report['success']:
                t0 = u32(p['tick'])
                time.sleep(0.05)
                t1 = u32(p['tick'])
                report['ticks_after_cleanup'] = [t0, t1]
                if t0 == t1:
                    report['success'] = False
                    raise RuntimeError("game didnt keep running after cleanup")
        except BaseException as exc:
            report.setdefault('cleanup_error', repr(exc))
            report['success'] = False
        if published or storage_owned:
            save()
    if not report['success']:
        raise RuntimeError("call or cleanup failed, check the report before trying again")
    return report['eax'], report
