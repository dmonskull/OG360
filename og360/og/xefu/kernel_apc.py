import datetime
import hashlib
import json
from pathlib import Path
import random
import struct
import time

from .resolve import PROFILE_BASE, MappingError, Resolver
from .runtime_discover import discover
from .xbe import metadata, scratch_candidates

PROFILE = Path(__file__).parent / 'profiles/kernel-apc-xefu7.json'
PROFILES = {'xefu7': PROFILE, 'xefu1_1': Path(__file__).parent / 'profiles/kernel-apc-xefu1_1.json',
            **{name: Path(__file__).parent / ('profiles/kernel-apc-%s.json' % name)
               for name in ('xefu5', 'xefu', 'xefu2', 'xefu3', 'xefu6', 'xefu7b')}}
STORAGE_SIZE = 192
REGISTERS = ('eax', 'ecx', 'edx', 'esi', 'edi')
REAL_TARGET, EDX_OUT, X87_BEFORE, FLOAT_OUT = 0x28, 0x68, 0x6C, 0x70
NO_FLOAT = bytes.fromhex('efbeadde0000f07f')
TEXT, WIDE_TEXT = '@string', '@wstring'
TEXTS = (TEXT, WIDE_TEXT)
THUNK = (
    '8bec',
    'dfe0',
    '89436c',
    '83ec18',
    '8bfc',
    '8d7310',
    '6a06',
    '59',
    'f3a5',
    '8b432c',
    '8b4b30',
    '8b5334',
    '8b7338',
    '8b7b3c',
    'ff5328',
    '8be5',
    '895368',
    '92',
    'dfe0',
    '32636d',
    '80e438',
    '7403dd5b70',
    '92',
    'c3',
)
STOP_TRIES = 30
THREAD_LIST_ENTRY = 0x104
KPROCESS_SIZE = 0x20
KPCR_NEXT_THREAD, KPCR_IDLE_THREAD, KPCR_DPC_LIST, KPCR_DPC_ACTIVE = 0x2C, 0x30, 0x50, 0x58
RUNNING, WAITING = 2, 5
SAFE_WAIT_OBJECTS = {0: 'notification event', 2: 'mutant', 6: 'thread', 8: 'timer', 9: 'timer'}
SOFTWARE_APC, SOFTWARE_DPC = 2, 4
SAFE_TRIES = 12
SAFE_WAIT = 3.0
SAFE_LOOKS = 12
NO_SAFE_POINT_RECHECK = 300
_NO_SAFE_POINT = {}
_KERNEL_VERIFIED = set()


class SafePointTimeout(RuntimeError):
    pass


def profile_data(emulator='xefu7'):
    path = PROFILES.get(emulator)
    if path is None:
        raise RuntimeError(f'calls dont work on {emulator} yet')
    return json.loads(path.read_text())


def gate_code(blocks):
    addresses = [b['address'] for b in blocks]
    bodies = [
        bytes.fromhex('9c60fc8b5c2428'),
        bytes.fromhex('ff7324ff7320'),
        bytes.fromhex('ff731cff7318'),
        bytes.fromhex('ff7314ff7310'),
        bytes.fromhex('ff13 894308'),
        bytes.fromhex('83c418ff430c'),
        bytes.fromhex('619dc20c00'),
    ]
    result = []
    for i, raw in enumerate(bodies):
        if i + 1 < len(bodies):
            raw += b'\xE9' + struct.pack('<i', addresses[i + 1] - addresses[i] - len(raw) - 5)
        if len(raw) > len(bytes.fromhex(blocks[i]['original'])):
            raise RuntimeError("gate doesnt fit in the kernel padding")
        result.append(raw)
    return result


def thunk_code(blocks):
    items = [bytes.fromhex(x) for x in THUNK]
    result = []
    for i, b in enumerate(blocks):
        room = len(bytes.fromhex(b['original']))
        last = i + 1 == len(blocks)
        raw = b''
        while items and len(raw) + len(items[0]) + (0 if last else 5) <= room:
            raw += items.pop(0)
        if not last:
            if not raw or not items:
                raise RuntimeError("register gate doesnt match its padding")
            raw += b'\xE9' + struct.pack('<i', blocks[i + 1]['address'] - b['address'] - len(raw) - 5)
        result.append(raw)
    if items:
        raise RuntimeError("register gate doesnt fit in the kernel padding")
    return result


def _digest(blocks, code):
    return hashlib.sha256(b''.join(struct.pack('<I', b['address']) + c for b, c in zip(blocks, code))).hexdigest()


def main_thread(read, p):
    u32 = lambda a: struct.unpack('<I', read(a, 4))[0]
    try:
        for _ in range(20):
            cur, _next, idle = struct.unpack('<3I', read(p['current_thread'], 12))
            if cur != idle:
                break
            time.sleep(0.01)
        process = u32(cur + 0x44) if cur != idle else u32(idle + 0x44) - KPROCESS_SIZE
        first = process + 8
        threads, e = [], u32(first)
        while e != first and len(threads) < 64:
            threads.append(e - THREAD_LIST_ENTRY)
            e = u32(e)
        if not threads or e != first or any(read(t, 1)[0] != 6 or u32(t + 0x44) != process for t in threads):
            return None, "thread list looks wrong for this kernel"
        commit = u32(0x10130)
        for t in threads:
            base, limit = struct.unpack('<2I', read(t + 0x1C, 8))
            if base - limit == commit:
                return t, ''
        return None, "cant find the main thread (no stack of size 0x%X)" % commit
    except (MappingError, struct.error) as exc:
        return None, "couldnt read the thread list (%s)" % exc


def call(client, target, arguments=(), *, registers=None, fallback=False, report_path=None, before_publish=None,
         scratch=None, string=None, notify=None, when='auto', wait_thread=None, safe_returns=()):
    if when not in ('auto', 'safe', 'now'):
        raise ValueError("timing has to be auto, safe or now")
    if len(arguments) > 6 or any(a not in TEXTS and (type(a) is not int or not 0 <= a <= 0xFFFFFFFF) for a in arguments):
        raise ValueError("use 0 to 6 stack args (32 bit)")
    if registers is not None:
        registers = dict(registers)
        if set(registers) - set(REGISTERS):
            raise ValueError("only eax, ecx, edx, esi and edi can be set")
        if any(v not in TEXTS and (type(v) is not int or not 0 <= v <= 0xFFFFFFFF) for v in registers.values()):
            raise ValueError("register values are 32 bit numbers, @string or @wstring")
    used = set(TEXTS) & set(list(arguments) + list((registers or {}).values()))
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
    p = profile_data(ident.emulator)
    gate_blocks = p['blocks']
    code = gate_code(gate_blocks)
    digest = _digest(gate_blocks, code)
    if digest != p['gate_sha256']:
        raise RuntimeError("gate code changed, cant reuse it")
    report = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'backend': 'guest-kernel-normal-apc-v1', 'emulator': ident.emulator, 'target': target,
              'arguments': list(arguments), 'stack_slots': 6, 'gate_sha256': digest,
              'samples': [], 'success': False}
    blocks = list(gate_blocks)
    if registers == {} and fallback and not p.get('thunk_blocks'):
        registers = None
        report['fallback'] = "no register gate here, cdecl only"
    if registers is not None:
        thunk_blocks = p.get('thunk_blocks')
        if not thunk_blocks:
            raise RuntimeError("only cdecl calls work on %s" % ident.emulator)
        thunk = thunk_code(thunk_blocks)
        if _digest(thunk_blocks, thunk) != p['thunk_sha256']:
            raise RuntimeError("register gate code changed, cant reuse it")
        blocks += thunk_blocks
        code += thunk
        report.update({'backend': 'guest-kernel-normal-apc-v1 + register gate', 'registers': registers,
                       'thunk_sha256': p['thunk_sha256']})
    held = False
    installed = []

    def say(text):
        if notify:
            try:
                notify(text)
            except Exception:
                pass

    published = False
    resumed = False
    requested = False
    storage_owned = False
    request = apc = thread = head = None

    def save():
        if report_path:
            out = Path(report_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2) + '\n')

    def read(a, n):
        return r.read_guest(a, n, ident)

    def u32(a):
        return struct.unpack('<I', read(a, 4))[0]

    def write(a, raw):
        if not held or (a & 4095) + len(raw) > 4096:
            raise RuntimeError("write needs the game stopped and one page")
        mapping = r.resolve(a, ident)
        client.write(mapping.host, raw)
        if read(a, len(raw)) != raw:
            raise RuntimeError(f'write didnt stick at {a:08X}')

    def put(a, value):
        write(a, struct.pack('<I', value))

    def header_unchanged():
        raw = bytearray(read(info['base'], info['headers_size']))
        if request is not None and info['base'] <= request < info['base'] + info['headers_size']:
            at = request - info['base']
            raw[at:at + STORAGE_SIZE] = bytes(min(STORAGE_SIZE, len(raw) - at))
        return hashlib.sha256(bytes(raw)).hexdigest() == info['header_sha256']

    def stop(check=True):
        nonlocal held
        client.command('stop')
        held = True
        r.forget()
        if check:
            r.assert_identity(ident)
            if not header_unchanged():
                raise RuntimeError("the game changed")

    def why_busy():
        lo = min(p['irql'], p['current_thread'])
        kpcr = read(lo, p['current_thread'] + 12 - lo)
        field = lambda a: struct.unpack_from('<I', kpcr, a - lo)[0]
        current = field(p['current_thread'])
        if current == field(p['current_thread'] + 8):
            return "idle thread is running, try again"
        try:
            t = read(current, 0x6C)
        except MappingError:
            return "current thread isnt running"
        queue = current + 0x34
        if t[0] != 6 or t[0x2C] != 2:
            return "current thread isnt running"
        if t[0x34:0x3C] != struct.pack('<2I', queue, queue):
            return "apc queue is busy, try again"
        if t[0x48:0x4A] != bytes(2) or struct.unpack_from('<I', t, 0x68)[0] != 0:
            return "apc delivery is off or busy"
        if kpcr[p['irql'] - lo] != 0 or u32(p['software_pending']) & 2:
            return "apc pending or irql too high, try again"
        return ''

    def go():
        nonlocal held
        client.command('go')
        held = False

    def find_main():
        return (wait_thread, '') if wait_thread is not None else main_thread(read, p)

    def quiet_stop(main):
        k = read(p['irql'], KPCR_DPC_ACTIVE + 4 - 0x24)
        f = lambda off: struct.unpack_from('<I', k, off - 0x24)[0]
        irql, cur, nxt, idle = k[0], f(0x28), f(KPCR_NEXT_THREAD), f(KPCR_IDLE_THREAD)
        if u32(p['software_pending']) & (SOFTWARE_APC | SOFTWARE_DPC):
            return '', "an apc or dpc is pending"
        if irql == 0 and cur not in (main, idle):
            return 'another thread at passive level', ''
        kpcr = p['irql'] - 0x24
        if cur == idle and irql == 2 and nxt == 0 and f(KPCR_DPC_ACTIVE) == 0 and f(KPCR_DPC_LIST) == kpcr + KPCR_DPC_LIST:
            return 'the idle loop', ''
        return '', "kernel is busy right now"

    def why_unsafe(main):
        where, busy = quiet_stop(main)
        if busy:
            return '', busy
        t = read(main, 0x6C)
        if t[0x2C] != WAITING or t[0x54] != 0:
            return '', "main thread isnt waiting"
        if t[0x34:0x3C] != struct.pack('<2I', main + 0x34, main + 0x34) or t[0x48:0x4A] != bytes(2) \
                or struct.unpack_from('<I', t, 0x68)[0]:
            return '', "main threads apcs are busy or off"
        first = block = struct.unpack_from('<I', t, 0x58)[0]
        for _ in range(66):
            if not block:
                return '', "main thread has no wait blocks"
            obj, block = struct.unpack('<2I', read(block + 0x0C, 8))
            kind = read(obj, 1)[0]
            if kind not in SAFE_WAIT_OBJECTS:
                return '', "main thread is waiting on a lock (type %d at %08X)" % (kind, obj)
            if block == first:
                break
        else:
            return '', "main threads wait blocks are broken"
        if safe_returns:
            ksp, base = struct.unpack_from('<2I', t, 0x24)[0], struct.unpack_from('<I', t, 0x1C)[0]
            size = max(0, min(0x200, base - ksp)) & ~3
            words = set(struct.unpack('<%dI' % (size // 4), read(ksp, size))) if size else set()
            if not words & set(safe_returns):
                return '', "main thread isnt at a safe spot for this game"
        return where, ''

    def sample():
        return {'hits': u32(request + 12), 'eax': u32(request + 8),
                'inserted': read(apc + 3, 1)[0], 'head': read(head, 8).hex(),
                'apc_pending': read(thread + 0x49, 1)[0],
                'normal_in_progress': read(thread + 0x48, 1)[0],
                'software_pending': u32(p['software_pending']),
                'irql': read(p['irql'], 1)[0], 'tick': u32(p['tick'])}

    try:
        ticks = [u32(p['tick'])]
        time.sleep(0.05)
        ticks.append(u32(p['tick']))
        report['ticks_before'] = ticks
        if ticks[0] == ticks[1]:
            raise RuntimeError("the game is paused, unpause it first")
        main = None
        if when != 'now':
            main, why = find_main()
            key = (ident.stable_key(), u32(0x10114), u32(u32(0x10118) + 8), main)
            if main is not None and when == 'auto' and \
                    time.monotonic() - _NO_SAFE_POINT.get(key, -1e9) < NO_SAFE_POINT_RECHECK:
                main, why = None, "its main thread never waited (checked the last 5 minutes)"
            elif main is not None:
                report['main_thread'] = main
                for look in range(SAFE_LOOKS):
                    if read(main + 0x2C, 1)[0] == WAITING:
                        break
                    time.sleep(0.01)
                else:
                    _NO_SAFE_POINT[key] = time.monotonic()
                    main, why = None, "its main thread never waited in %d tries" % SAFE_LOOKS
            if main is None:
                report['safe_point'] = why
                if when == 'safe':
                    raise RuntimeError("no safe spot: %s. this game needs its own hook" % why)
                say("no safe spot: %s, calling right away" % why)
        stop(check=False)
        kpcr = p['irql'] - 0x24
        if struct.unpack('<2I', read(kpcr + 0x1C, 8)) != (kpcr, p['current_thread']):
            raise RuntimeError("kernel doesnt match the %s profile (kpcr)" % ident.emulator)
        info = metadata(read)
        report['game'] = info
        runtime = discover(r, ident, info)
        report['runtime'] = runtime
        ranges = runtime['compile_range_rows']
        if not any(lo <= target < hi for lo, hi, _ in ranges):
            raise RuntimeError("target isnt in the code ranges")
        verified = (ident.stable_key(), p['name'])
        if verified not in _KERNEL_VERIFIED:
            for check in p['checks']:
                if hashlib.sha256(read(check['address'], check['size'])).hexdigest() != check['sha256']:
                    raise RuntimeError("kernel doesnt match the %s profile" % ident.emulator)
        for b in blocks:
            original = bytes.fromhex(b['original'])
            prior = bytes.fromhex(b['preceding_bytes'])
            size = len(original)
            if not any(lo <= b['address'] and b['address'] + size <= hi for lo, hi, _ in ranges):
                raise RuntimeError("kernel gate isnt in the code ranges")
            if b['address'] <= target < b['address'] + size:
                raise ValueError("target overlaps the gate")
            if b['preceding_address'] + len(prior) == b['address']:
                seen = read(b['preceding_address'], len(prior) + size)
                owner, padding = seen[:len(prior)], seen[len(prior):]
            else:
                owner, padding = read(b['preceding_address'], len(prior)), read(b['address'], size)
            if padding != original:
                raise RuntimeError("kernel padding is in use")
            if owner != prior:
                raise RuntimeError("kernel padding owner changed")
        say("%s profile ok (%s), %d gate blocks free, target ok (%s), kernel running (tick %d -> %d)"
            % (ident.emulator, "checked earlier" if verified in _KERNEL_VERIFIED else '%d checks' % len(p['checks']),
               len(blocks), runtime.get('source', 'compile-range table'), ticks[0], ticks[1]))
        _KERNEL_VERIFIED.add(verified)
        safe = ''
        if main is not None:
            for tries in range(1, SAFE_TRIES + 1):
                if not held:
                    stop(check=False)
                safe, why = why_unsafe(main)
                if safe:
                    break
                go()
                time.sleep(random.uniform(0.02, 0.15))
            report['safe_stops'] = tries
            if not safe:
                report['safe_point'] = "main thread never waited in %d stops (%s)" % (tries, why)
                if when == 'safe' or safe_returns:
                    raise RuntimeError("no safe spot: %s" % report['safe_point'])
                say("no safe spot: %s, calling right away" % report['safe_point'])
        if not safe:
            for tries in range(1, STOP_TRIES + 1):
                if not held:
                    stop(check=False)
                busy = why_busy()
                if not busy:
                    break
                go()
                time.sleep(random.uniform(0.02, 0.15))
            report['stops'] = tries
            if busy:
                raise RuntimeError(busy)
            if tries > 1:
                say("stopped the game %d times to find a safe spot" % tries)
        r.assert_identity(ident)
        if not header_unchanged():
            raise RuntimeError("the game changed")
        candidates = [scratch] if scratch is not None else scratch_candidates(info, STORAGE_SIZE)
        for candidate in candidates:
            if type(candidate) is not int or candidate & 15 or candidate + STORAGE_SIZE > 0x100000000:
                raise ValueError("scratch has to be 16 byte aligned")
            if (candidate & 4095) + STORAGE_SIZE > 4096:
                continue
            if candidate <= target < candidate + STORAGE_SIZE or any(
                    candidate < b['address'] + len(bytes.fromhex(b['original']))
                    and b['address'] < candidate + STORAGE_SIZE for b in blocks):
                raise ValueError("scratch overlaps the target or gate")
            if read(candidate, STORAGE_SIZE) == bytes(STORAGE_SIZE):
                request = candidate
                break
        if request is None:
            raise RuntimeError("no free space in the xbe header. put a 192 byte area you own in the scratch field")
        apc = request + 0x40
        if safe:
            where, busy = why_unsafe(main)
            thread = main
            report['timing'] = "safe spot: thread %08X waiting (stop in %s)" % (main, where)
        else:
            busy = why_busy()
            thread = u32(p['current_thread'])
            report['timing'] = "right away on the current thread" + (
                " (no safe spot: %s)" % report['safe_point'] if 'safe_point' in report else '')
        if busy:
            raise RuntimeError(busy)
        head = thread + 0x34
        say("game stopped, request at %08X, %s thread %08X" % (
            request, 'waiting' if safe else 'current', thread))
        if before_publish:
            before_publish(r, ident, request)
        report['request'] = request
        report['thread'] = thread
        data = bytearray(STORAGE_SIZE)
        slots = [request + 0x80 if a in TEXTS else a for a in arguments]
        report['resolved_arguments'] = slots
        struct.pack_into('<10I', data, 0, target if registers is None else thunk_blocks[0]['address'],
                         len(arguments), 0xDEADBEEF, 0, *(slots + [0] * (6 - len(arguments))))
        if registers is not None:
            loaded = [request + 0x80 if registers.get(n) in TEXTS else registers.get(n, 0) for n in REGISTERS]
            report['resolved_registers'] = dict(zip(REGISTERS, loaded))
            struct.pack_into('<6I', data, REAL_TARGET, target, *loaded)
            struct.pack_into('<I', data, EDX_OUT, 0xDEADBEEF)
            data[FLOAT_OUT:FLOAT_OUT + 8] = NO_FLOAT
        struct.pack_into('<HBB9I', data, 0x40, 0x12, 0, 1, thread, head, head,
                         p['kernel_noop'], 0, gate_blocks[0]['address'], request, 0, 0)
        if raw_string is not None:
            data[0x80:0x80 + len(raw_string)] = raw_string
        storage_owned = True
        write(request, data)
        for b, raw in zip(blocks, code):
            original = bytes.fromhex(b['original'])
            if read(b['address'], len(original)) != original:
                raise RuntimeError("padding changed before install")
            installed.append((b, raw))
            write(b['address'], raw)
        published = True
        write(head, struct.pack('<2I', apc + 8, apc + 8))
        write(thread + 0x49, b'\x01')
        if not safe:
            requested = True
            put(p['software_pending'], u32(p['software_pending']) | SOFTWARE_APC)
        report['published'] = sample()
        say("gate put in %d padding blocks, apc queued" % len(installed))
        save()
        resumed = True
        go()
        say("game resumed, waiting for the %s" % (
            "thread to run the call" if safe else 'kernel to run the call'))
        deadline = time.monotonic() + (SAFE_WAIT if safe else 5)
        while time.monotonic() < deadline:
            raw = read(request + 8, 0x3C)
            state = {'hits': struct.unpack_from('<I', raw, 4)[0], 'inserted': raw[0x3B],
                     'normal_in_progress': read(thread + 0x48, 1)[0]}
            report['samples'].append(state)
            if state['hits'] and state['inserted'] == 0 and state['normal_in_progress'] == 0:
                break
            time.sleep(0.02)
        stop()
        report['after'] = sample()
        after = report['after']
        if safe and after['hits'] == 0 and after['inserted'] and not after['normal_in_progress'] \
                and read(thread + 0x2C, 1)[0] == WAITING:
            raise SafePointTimeout("main thread didnt wake up in %g s, call pulled without running" % SAFE_WAIT)
        if after['hits'] != 1 or after['inserted'] or after['normal_in_progress']:
            raise RuntimeError("call didnt finish exactly once")
        report['eax'] = report['after']['eax']
        if registers is not None:
            out = read(request + EDX_OUT, 16)
            report['edx'] = struct.unpack_from('<I', out, 0)[0]
            report['float'] = None if out[8:16] == NO_FLOAT else struct.unpack_from('<d', out, 8)[0]
            say("done: eax = %d (0x%08X), edx = 0x%08X%s" % (
                report['eax'], report['eax'], report['edx'],
                '' if report['float'] is None else ", float %r" % report['float']))
        else:
            say("done: eax = %d (0x%08X)" % (report['eax'], report['eax']))
        report['success'] = True
    except BaseException as exc:
        report['error'] = repr(exc)
        raise
    finally:
        try:
            r.assert_identity(ident)
            if storage_owned or installed or published:
                if not held:
                    stop()
                if published:
                    for _ in range(10):
                        cur = u32(p['current_thread'])
                        if cur != thread or read(p['irql'], 1)[0] == 0:
                            break
                        go()
                        time.sleep(random.uniform(0.01, 0.05))
                        stop()
                    else:
                        raise RuntimeError("kernel was running the call every time, left it in place")
                if not header_unchanged():
                    raise RuntimeError("the game changed, skipped cleanup")
                if published and read(apc + 3, 1) == b'\x01':
                    linked = read(head, 8)
                    if linked == struct.pack('<2I', head, head) and not resumed:
                        pass
                    elif linked != struct.pack('<2I', apc + 8, apc + 8):
                        raise RuntimeError("queue changed, cant pull the call safely")
                    elif read(thread + 0x48, 1) != b'\x00':
                        raise RuntimeError("apc still running")
                    else:
                        write(head, struct.pack('<2I', head, head))
                        write(thread + 0x49, b'\x00')
                        if requested and u32(p['current_thread']) == thread:
                            put(p['software_pending'], u32(p['software_pending']) & ~SOFTWARE_APC)
                        write(apc + 3, b'\x00')
                        report['withdrawn'] = True
                        say("call hadnt run yet, pulled it")
                if read(thread + 0x48, 1) != b'\x00':
                    raise RuntimeError("call still running, cant clean up yet")
                restored, foreign = 0, []
                for b, raw in reversed(installed):
                    original = bytes.fromhex(b['original'])
                    now = read(b['address'], len(original))
                    if now == original:
                        continue
                    if now != raw + original[len(raw):]:
                        foreign.append('%08X' % b['address'])
                        continue
                    write(b['address'], original)
                    restored += 1
                write(request, bytes(STORAGE_SIZE))
                report['cleanup'] = {'blocks_restored': restored, 'request_zero': read(request, STORAGE_SIZE) == bytes(STORAGE_SIZE)}
                say("put back %d gate blocks, request %s" % (
                    restored, 'cleared' if report['cleanup']['request_zero'] else "not cleared"))
                if foreign:
                    raise RuntimeError("code changed at %s, left it alone" % ', '.join(foreign))
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
                say("game still running after cleanup (tick %d -> %d)" % (t0, t1))
        except BaseException as exc:
            report.setdefault('cleanup_error', repr(exc))
            report['success'] = False
        save()
    if not report['success']:
        raise RuntimeError("call or cleanup failed, check the report before trying again")
    return report['eax'], report
