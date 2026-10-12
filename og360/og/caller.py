import datetime
import hashlib
import json
import re
import struct
import threading
import time
import uuid
from pathlib import Path

from .. import paths
from ..log import describe
from .xefu import game_hook, kernel_apc
from .xefu.xbe import metadata

MAX_ARGS = 6
REGISTERS = kernel_apc.REGISTERS
FULL_GATE_REGISTERS = ('ebx', 'ebp')
XMM = ('xmm0', 'xmm1', 'xmm2', 'xmm3')
TEXTS = kernel_apc.TEXTS
CONVENTIONS = {'cdecl': (), 'stdcall': (), 'thiscall': ('ecx',), 'fastcall': ('ecx', 'edx')}
BUSY = ("irql too high", "apc queue is busy",
        "apc delivery is off", "idle thread is running",
        "current thread isnt running")
RETRIES = 2

HINTS = (
    ("kernel padding is in use", "a call left the gate in. restart the game before calling again"),
    ("the game is paused", "unpause the game and call again"),
    ("apc queue is busy", "the game was busy right then, try again"),
    ("irql too high", "the game was busy right then, try again"),
    ("apc delivery is off", "the games thread cant take a call right now, try again in a sec"),
    ("idle thread is running", "the game was idle every time, try again"),
    ("current thread isnt running", "the games thread was switching every time, try again"),
    ("no free space in the xbe header", "use the scratch field (advanced)"),
    ("isnt in the code ranges", "that address isnt in this games xbe. use an address from this game"),
    ("unknown emulator build", "OG360 doesnt know this emulator build (it knows the 8 xefu builds from the xbox 360s compat pack)"),
    ("calls dont work on", "this emulator build cant do calls yet. memory editing still works"),
    ("only cdecl calls work", "this build only does cdecl calls with no register args"),
    ("no safe spot", "the games main thread never stopped at a safe spot. halo 2 only gets there in unpaused "
                      "gameplay. use timing auto or now, or unpause and try again"),
    ("didnt reach the hook", "halo 2s hook only runs in unpaused gameplay (not menus, pause or loading). unpause and try "
                               "again or use timing auto or now"),
    ("doesnt match the", "calls need the right emulator build and a stock xbox kernel"),
    ("didnt finish exactly once", "check the report before calling again. restart the game if anything looks off"),
    ("still running in the hook", "the function never returned. restart the game before calling again"),
    ("hook code changed", "the games hook bytes are different. restart the game before using a new hook"),
    ("gaps in the games code for the full gate", "this game has too little padding for ebx, ebp or xmm calls"),
)


def hint_for(message):
    for needle, hint in HINTS:
        if needle.lower() in message.lower():
            return hint
    return ""


def number(value):
    if isinstance(value, bool):
        raise ValueError("use 0 or 1 for a bool arg")
    if isinstance(value, int):
        result = value
    else:
        value = str(value).strip()
        result = int(value, 16 if value.lower().lstrip('+-').startswith('0x') else 10)
    if not -0x80000000 <= result <= 0xFFFFFFFF:
        raise ValueError("args have to fit in 32 bits")
    return result & 0xFFFFFFFF


def address(value):
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r'(?:0[xX])?[0-9a-fA-F]{1,8}', value.strip()):
        result = int(value.strip(), 16)
    else:
        raise ValueError("enter a function address in hex, like 0x000679E0")
    if not 0x10000 <= result < 0x80000000:
        raise ValueError("use an og xbox game address, not a host or kernel one")
    return result


class FloatBits(int):
    pass


def value(text):
    if text in TEXTS or isinstance(text, FloatBits):
        return text
    if isinstance(text, str) and text.strip().lower().startswith('f:'):
        return float_bits(text.strip()[2:])
    return number(text)


def xmm_value(text):
    if isinstance(text, int):
        return text & 0xFFFFFFFF
    t = str(text).strip()
    if t.lower().startswith('0x'):
        return number(t)
    return int(float_bits(t[2:] if t.lower().startswith('f:') else t))


def plan_call(target, arguments, convention='cdecl'):
    target = address(target)
    if convention not in CONVENTIONS:
        raise ValueError("convention has to be one of: %s" % ', '.join(CONVENTIONS))
    args, registers, extra, xmm = [], {}, {}, {}
    for a in arguments:
        reg = None
        if isinstance(a, tuple):
            reg, a = a
        elif isinstance(a, str) and '=' in a:
            reg, a = a.split('=', 1)
        if reg is None:
            args.append(value(a))
            continue
        reg = reg.strip().lower()
        if reg in registers or reg in extra or reg in xmm:
            raise ValueError("%s is set twice" % reg)
        if reg in XMM:
            xmm[reg] = xmm_value(a)
            continue
        if reg in FULL_GATE_REGISTERS:
            extra[reg] = number(a)
            continue
        if reg not in REGISTERS:
            raise ValueError("%r cant be set, use eax, ecx, edx, esi, edi, ebx, ebp or xmm0 to xmm3" % reg)
        if reg in registers:
            raise ValueError("%s is set twice" % reg)
        registers[reg] = value(a.strip() if isinstance(a, str) else a)
    for reg in CONVENTIONS[convention]:
        if reg in registers:
            raise ValueError("%s takes %s from its args, leave out %s=" % (convention, reg, reg))
        if convention == 'thiscall':
            if not args or isinstance(args[0], FloatBits):
                raise ValueError("thiscall needs the object (this) as the first arg")
            registers[reg] = args.pop(0)
            continue
        i = next((i for i, a in enumerate(args) if not isinstance(a, FloatBits)), None)
        if i is not None:
            registers[reg] = args.pop(i)
    if len(args) > MAX_ARGS:
        raise ValueError("max 6 stack args")
    plain = lambda v: v if v in TEXTS else int(v)
    args, registers = [plain(a) for a in args], {r: plain(v) for r, v in registers.items()}
    full = None
    if extra or xmm:
        top = max([XMM.index(r) for r in xmm] + [-1]) + 1
        full = {'ebx': extra.get('ebx', 0), 'ebp': extra.get('ebp', 0), 'xmm': [xmm.get(XMM[i], 0) for i in range(top)]}
    return {'address': target, 'arguments': args, 'registers': registers, 'abi': convention, 'extra': full,
            'fallback': convention == 'cdecl' and not registers and not full}


def float_bits(text):
    s = str(text).strip()
    if len(s) > 1 and s[-1] in "fF" and (s[-2].isdigit() or s[-2] == "."):
        s = s[:-1]
    try:
        return FloatBits(struct.unpack("<I", struct.pack("<f", float(s)))[0])
    except ValueError:
        raise ValueError("%r isnt a number" % text) from None
    except OverflowError:
        raise ValueError("%r is too big for a float" % text) from None


def parse_arguments(items):
    items = [tuple(i) + (None,) * (3 - len(i)) for i in items]
    while items and items[-1][0] not in ("string", "wstring") and not str(items[-1][1]).strip():
        items.pop()
    out = []
    for i, (kind, text, reg) in enumerate(items, 1):
        if kind in ("string", "wstring"):
            v = "@" + kind
        elif not str(text).strip():
            raise ValueError("arg %d is empty, fill them in order" % i)
        elif kind == "float":
            v = float_bits(text)
        else:
            v = number(text)
        out.append(v if reg is None else (reg, v))
    return out


def eax_views(value):
    signed = value - (1 << 32) if value & 0x80000000 else value
    as_float = struct.unpack("<f", struct.pack("<I", value))[0]
    return {"dec": value, "hex": "0x%08X" % value, "signed": signed, "float": as_float}


def show_call(target, slots, string=None, convention="cdecl"):
    def one(slot):
        if isinstance(slot, tuple):
            return "%s=%s" % (slot[0], one(slot[1]))
        if slot in TEXTS:
            return '%s"%s"' % ("L" if slot == "@wstring" else "", string or "")
        return str(slot) if slot < 0x10000 else "0x%X" % slot
    return "%s0x%08X(%s)" % ("" if convention == "cdecl" else convention + " ", target,
                             ", ".join(one(s) for s in slots))


SAFE_RETURNS = {
    0x54540009: (0x0022412E,),
}


class FunctionCaller:
    def __init__(self, console, log, reports=None):
        self.console = console
        self.log = log
        self.reports = Path(reports) if reports else Path(paths.path("reports"))
        self.last_report = None
        self.last_result = None
        self._lock = threading.RLock()

    def _report_path(self):
        self.reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')
        return self.reports / (stamp + '-' + uuid.uuid4().hex[:12] + '.json')

    def call(self, target, *arguments, convention='cdecl', expected=None, string=None, scratch=None, title_id=None,
             when='auto'):
        with self._lock:
            return self._call(target, arguments, convention, expected, string, scratch, title_id, when)

    def _call(self, target, arguments, convention, expected, string, scratch, title_id, when):
        if when not in ('auto', 'safe', 'now'):
            raise ValueError("timing has to be auto, safe or now")
        plan = plan_call(target, arguments, convention)
        plan['when'] = when
        if expected is not None and str(expected).strip() != "":
            expected = number(expected)
        else:
            expected = None
        used = set(TEXTS) & set(plan["arguments"] + list((plan["registers"] or {}).values()))
        if len(used) > 1:
            raise ValueError("use @string or @wstring, not both (they share the text)")
        if string is not None:
            string = str(string)
            if "@wstring" in used:
                if len(string.encode("utf-16-le")) > 62:
                    raise ValueError("@wstring text is max 31 characters")
            else:
                try:
                    string.encode("ascii")
                except UnicodeEncodeError:
                    raise ValueError("@string text has to be plain ascii (use @wstring for anything else)") from None
                if len(string) > 63:
                    raise ValueError("string text is max 63 characters")
        if used and string is None:
            raise ValueError("a string arg needs text")
        if scratch is not None:
            scratch = number(scratch)
        self.last_result = None
        if self.console.stopped:
            raise RuntimeError("the game is paused, unpause it first")
        if self.console.holding:
            raise RuntimeError("a big read has the game stopped, try again when its done")
        attempt = 0
        while True:
            try:
                return self._attempt(plan, expected, string, scratch, title_id, last=attempt == RETRIES)
            except kernel_apc.SafePointTimeout:
                if plan['when'] != 'auto' or title_id in SAFE_RETURNS or not self._withdrawn():
                    raise
                self.log.note("main thread didnt wake up in time and nothing ran, calling right away instead")
                plan['when'] = 'now'
            except RuntimeError as exc:
                if attempt < RETRIES and self._busy_before_writing(exc):
                    attempt += 1
                    self.log.note("game was busy right then and nothing got written, trying again (%d of %d)" % (attempt + 1, RETRIES + 1))
                    time.sleep(0.5)
                    continue
                raise

    def _withdrawn(self):
        try:
            gate = json.loads(self.last_report.with_suffix('.gate.json').read_text())
        except (OSError, ValueError, AttributeError):
            return False
        return bool(gate.get('withdrawn') and (gate.get('cleanup') or {}).get('request_zero')
                    and not gate.get('cleanup_error'))

    def _busy_before_writing(self, exc):
        if not any(needle in str(exc) for needle in BUSY):
            return False
        try:
            gate = json.loads(self.last_report.with_suffix('.gate.json').read_text())
        except (OSError, ValueError, AttributeError):
            return False
        return 'published' not in gate and 'cleanup' not in gate

    def _attempt(self, plan, expected, string, scratch, title_id, last=True):
        path = self._report_path()
        self.last_report = path
        gate_path = path.with_suffix('.gate.json')
        report = {'plan': plan, 'gate_report': str(gate_path), 'success': False,
                  'backend': 'shared guest kernel normal APC', 'tool': 'OG360'}
        if string is not None:
            report['string'] = string
        try:
            return self.console.run(lambda client: self._execute(
                client, plan, expected, string, scratch, title_id, report, gate_path))
        except BaseException as exc:
            report['error'] = describe(exc)
            if last or not any(needle in str(exc) for needle in BUSY):
                self._explain(gate_path, report)
            raise
        finally:
            try:
                path.write_text(json.dumps(report, indent=2) + '\n')
                self.log.note("report saved: %s" % path.name)
            except OSError as e:
                self.log.note("couldnt save the report: %s" % e, "warn")

    def _explain(self, gate_path, report):
        try:
            gate = json.loads(gate_path.read_text())
        except (OSError, ValueError):
            gate = {}
        for key in ('error', 'cleanup_error'):
            if gate.get(key):
                report['gate_' + key] = gate[key]
        if gate.get('cleanup_error'):
            self.log.note("cleanup didnt finish: %s. check the report and restart the game before calling again" % gate['cleanup_error'], "error")
        elif gate.get('cleanup'):
            c = gate['cleanup']
            self.log.note("put things back after the failure: %d gate blocks, request %s"
                          % (c.get('blocks_restored', 0), "cleared" if c.get('request_zero') else "not cleared"))
        hint = hint_for(report.get('error', '') + ' ' + gate.get('error', '') + ' ' + gate.get('cleanup_error', ''))
        if hint:
            report['hint'] = hint
            self.log.note(hint, "warn")

    def _execute(self, client, plan, expected, string, scratch, title_id, report, gate_path):
        def validate(r, identity, request):
            info = metadata(lambda a, n: r.read_guest(a, n, identity))
            if title_id is not None and info['title_id'] != title_id:
                raise ValueError("the running game (%08X) isnt the one this call was saved for (%08X), nothing changed" % (info['title_id'], title_id))
            target = plan['address']
            owner = next((s for s in info['sections'] if s['executable']
                          and s['address'] <= target < s['address'] + s['size']), None)
            if owner is None:
                raise ValueError("address isnt in the games code")
            raw = r.read_guest(target, min(16, owner['address'] + owner['size'] - target), identity)
            report['game_title_id'] = '%08X' % info['title_id']
            report['game_title'] = info['title']
            report['target_entry_bytes'] = raw.hex()
            report['target_entry_sha256'] = hashlib.sha256(raw).hexdigest()
            report['game_identity_sha256'] = info['identity_sha256']
            report['target_validation'] = "game code section, live entry bytes, no offline check"
            self.log.note("target %08X is in %ss code, entry bytes %s" % (
                target, info['title'] or 'the game', raw[:8].hex(' ').upper()))

        gate = None
        if plan['when'] != 'now' and not plan.get('extra'):
            try:
                value, gate = game_hook.call(client, plan['address'], plan['arguments'], registers=plan['registers'],
                                             string=string, scratch=scratch, report_path=gate_path,
                                             before_publish=validate, notify=self.log.note, title_id=title_id)
            except game_hook.Unavailable as exc:
                if str(exc) != game_hook.NO_HOOK:
                    self.log.note('game hook not used: %s' % exc)
                    report['hook_unavailable'] = str(exc)
        if gate is None:
            value, gate = kernel_apc.call(client, plan['address'], plan['arguments'], registers=plan['registers'],
                                          fallback=plan['fallback'], string=string, report_path=gate_path,
                                          before_publish=validate, scratch=scratch, notify=self.log.note,
                                          when=plan['when'], safe_returns=SAFE_RETURNS.get(title_id, ()),
                                          extra=plan.get('extra'))
        report['backend'] = gate.get('backend', report['backend'])
        report['eax'] = value
        report['gate_success'] = gate['success']
        self.last_result = {'eax': value, 'edx': gate.get('edx'), 'float': gate.get('float'),
                            'xmm0': gate.get('xmm0'), 'timing': gate.get('timing')}
        for key in ('edx', 'float', 'xmm0', 'full_gate', 'timing', 'safe_point', 'main_thread'):
            if key in gate:
                report[key] = gate[key]
        for key in ('after', 'cleanup', 'ticks_before', 'ticks_after_cleanup', 'request', 'thread'):
            if key in gate:
                report[key] = gate[key]
        if expected is not None and value != expected:
            raise RuntimeError('Expected %d, received %d' % (expected, value))
        report['success'] = True
        return value
