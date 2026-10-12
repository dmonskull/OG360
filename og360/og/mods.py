import json
import os
import re
import struct
import threading
import time

from .. import paths
from ..log import NullLog, describe
from . import address as expressions, tagnames, values
from .caller import CONVENTIONS, REGISTERS, TEXTS, address as call_address, float_bits, number, parse_arguments, plan_call
from .drivers import DRIVERS
from .guest import Empty

FREEZE_INTERVAL = 0.25
CALL_KINDS = ("int", "float", "string")


def split_arg(a):
    if str(a).strip().startswith("="):
        return None, str(a).strip()
    reg, eq, v = str(a).partition("=")
    return (reg.strip().lower(), v.strip()) if eq else (None, reg.strip())


def call_args(args):
    out, seen = [], set()
    for a in args:
        if not str(a).strip():
            continue
        reg, v = split_arg(a)
        if reg is not None:
            if reg not in REGISTERS:
                raise ValueError("%r cant be set, use eax, ecx, edx, esi or edi" % (reg or a))
            if reg in seen:
                raise ValueError("%s is set twice" % reg)
            seen.add(reg)
        if v in TEXTS:
            pass
        elif v.lower().startswith("f:"):
            float_bits(v[2:])
        elif v.startswith("="):
            expressions.parse(v[1:])
        else:
            number(v)
        out.append("%s=%s" % (reg, v) if reg else v)
    return out


def call_items(args):
    out = []
    for a in args:
        reg, v = split_arg(a)
        if v in TEXTS:
            out.append((v[1:], "", reg))
        elif v.lower().startswith("f:"):
            out.append(("float", v[2:], reg))
        elif v.startswith("="):
            out.append(("int", "0", reg))
        else:
            out.append(("int", v, reg))
    return out


def call_convention(name):
    name = str(name or "cdecl").strip().lower()
    if name not in CONVENTIONS:
        raise ValueError("convention has to be one of: %s" % ", ".join(CONVENTIONS))
    return name


def check_call(address, args, convention):
    plan_call(address, parse_arguments(call_items(args)), convention)


class ModTable:
    def __init__(self, guest, log=None):
        self.guest = guest
        self.log = log or NullLog()
        self.caller = None
        self.title_id = None
        self.title = ""
        self.entries = []
        self.patches = []
        self.calls = []
        self.switches = []
        self.symbols = {}
        self.freeze_error = ""
        self.switch_status = {}
        self._touched = {}
        self._drivers = {}
        self.tag_info = {}
        self.game_folders = []
        self._names = (None, None)
        self._lock = threading.RLock()
        self._switch_lock = threading.RLock()
        self._next_id = 1
        self._thread = None
        self._stop = threading.Event()

    def load_for(self, title_id, title):
        with self._lock:
            self.title_id, self.title = title_id, title
            self.entries, self.patches, self.calls, self.switches, self.symbols = [], [], [], [], {}
            self.tag_info = {}
            folder = paths.bundled("tables")
            found = sorted(f for f in os.listdir(folder) if f.upper().startswith(title_id.upper())
                           and f.endswith(".json")) if os.path.isdir(folder) else []
            if found:
                with open(os.path.join(folder, found[0]), encoding="utf-8") as f:
                    self._apply(json.load(f))
            self.guest.symbols = self.symbols

    def _apply(self, data):
        self.entries = [dict(e) for e in data.get("entries", [])]
        self.patches = [dict(p) for p in data.get("patches", [])]
        self.calls = [dict(c) for c in data.get("calls", [])]
        self.symbols = dict(data.get("symbols", {}))
        self._next_id = 1
        for entry in self.entries:
            entry["id"] = self._next_id
            entry["frozen"] = False
            self._next_id += 1
        for patch in self.patches:
            patch["id"] = self._next_id
            patch["enabled"] = False
            self._next_id += 1
        for call in self.calls:
            call["id"] = self._next_id
            call["args"] = [str(a) for a in call.get("args", [])]
            call["convention"] = call_convention(call.get("convention"))
            call["last"] = ""
            self._next_id += 1
        self.switches = []
        for sw in data.get("switches", []):
            self.switches.append({"id": self._next_id, "name": sw.get("name", "Unnamed switch"), "note": sw.get("note", ""),
                                  "writes": [{k: str(w.get(k, "u32" if k == "type" else "")).strip()
                                              for k in ("address", "type", "on", "off")} for w in sw.get("writes", [])],
                                  "driver": str(sw.get("driver", "")), "group": str(sw.get("group", "")),
                                  "pick": dict(sw["pick"]) if sw.get("pick") else None,
                                  "picked_name": str(sw.get("picked_name", "")), "picked": None, "on": False})
            self._next_id += 1
        self._touched, self.switch_status = {}, {}
        self.tag_info = dict(data.get("tag_info", {}))

    def export(self):
        with self._lock:
            return {
                "title_id": self.title_id,
                "title": self.title,
                "entries": [{k: e.get(k, "") for k in ("name", "address", "type", "value", "note")} for e in self.entries],
                "patches": [{k: p.get(k, "") for k in ("name", "address", "on", "off", "note")} for p in self.patches],
                "calls": [{k: c.get(k, "" if k != "args" else []) for k in ("name", "address", "args", "convention",
                                                                              "string", "expect", "note")}
                          for c in self.calls],
                "switches": [dict({"name": w["name"], "note": w["note"], "writes": [dict(x) for x in w["writes"]]},
                                  **{k: w[k] for k in ("driver", "group", "pick", "picked_name") if w.get(k)})
                             for w in self.switches],
                "tag_info": self.tag_info,
                "symbols": self.symbols,
            }

    def import_data(self, data):
        with self._lock:
            self.symbols.update(data.get("symbols", {}))
            self.guest.symbols = self.symbols
            for e in data.get("entries", []):
                self.add_entry(e.get("name", ""), e.get("address", ""), e.get("type", "u32"),
                               e.get("value", ""), e.get("note", ""))
            for p in data.get("patches", []):
                self.add_patch(p.get("name", ""), p.get("address", ""), p.get("on", ""),
                               p.get("off", ""), p.get("note", ""))
            for c in data.get("calls", []):
                self.add_call(c.get("name", ""), c.get("address", ""), c.get("args", []),
                              c.get("string", ""), c.get("expect", ""), c.get("note", ""),
                              convention=c.get("convention", "cdecl"))
            for sw in data.get("switches", []):
                self.add_switch(sw.get("name", ""), sw.get("writes", []), sw.get("note", ""),
                                driver=sw.get("driver", ""), group=sw.get("group", ""))

    def _find(self, items, item_id):
        for item in items:
            if item["id"] == item_id:
                return item
        raise KeyError("that entry is gone")

    def _items(self, kind):
        return {"entry": self.entries, "patch": self.patches, "call": self.calls, "switch": self.switches}[kind]


    def add_entry(self, name, address, vtype, value="", note=""):
        if vtype not in values.ALL:
            raise ValueError("unknown type %r" % vtype)
        self.guest.check(str(address))
        values.bit_op(vtype, value)
        with self._lock:
            entry = {"id": self._next_id, "name": name or "Unnamed", "address": str(address), "type": vtype,
                     "value": str(value), "note": note, "frozen": False}
            self._next_id += 1
            self.entries.append(entry)
            return entry

    def update_entry(self, item_id, **fields):
        if fields.get("address") is not None:
            self.guest.check(str(fields["address"]))
        with self._lock:
            entry = self._find(self.entries, item_id)
            values.bit_op(fields.get("type") or entry["type"], entry["value"] if fields.get("value") is None
                          else fields["value"])
            for k in ("name", "address", "type", "value", "note"):
                if fields.get(k) is not None:
                    entry[k] = str(fields[k])
            if fields.get("frozen") is not None:
                entry["frozen"] = bool(fields["frozen"])
            self._start_freezer()
            return entry

    def delete(self, kind, item_id):
        if kind == "switch" and self._find(self.switches, item_id).get("on"):
            self.set_switch(item_id, False)
        with self._lock:
            items = self._items(kind)
            items.remove(self._find(items, item_id))

    @staticmethod
    def _encode(t, value):
        data = values.encode(t, value)
        return data + b"\0" if t == "text" else data + b"\0\0" if t == "text16" else data

    def set_value(self, item_id, value):
        with self._lock:
            entry = self._find(self.entries, item_id)
            op = values.bit_op(entry["type"], value)
            data = None if op else self._encode(entry["type"], value)
            entry["value"] = str(value)
        addr = self.guest.resolve(entry["address"])
        if op:
            data = self._bits(entry, addr, op)
        if data is not None:
            self.guest.write(addr, data)
        self.log.note("%s: %s %s (%s) at %08X" % (entry["name"], "wrote" if data is not None else "already",
                                                  value, values.LABELS[entry["type"]].lower(), addr))

    def _bits(self, entry, addr, op):
        now = self.guest.read(addr, values.size_of(entry["type"]))
        new = values.apply_bits(now, op)
        return None if new == now else new

    def live(self):
        with self._lock:
            entries = [dict(e) for e in self.entries]
            patches = [dict(p) for p in self.patches]
            calls = [dict(c) for c in self.calls]
            switches = [dict(sw, status=self._drivers[sw["id"]].status if sw["id"] in self._drivers
                             else self.switch_status.get(sw["id"], "")) for sw in self.switches]
        wanted, owners = [], []
        memo = {}
        for e in entries:
            try:
                e["resolved"] = self.guest.resolve(e["address"], memo)
                wanted.append((e["resolved"], values.size_of(e["type"]) if e["type"] in values.NUMERIC else 32))
                owners.append(e)
            except Exception as err:
                e["live"] = None
                e["error"] = str(err)
        for e, raw in zip(owners, self.guest.read_small(wanted) if wanted else []):
            if raw is None:
                e["live"] = None
                e["error"] = "not mapped right now"
            else:
                e["live"] = values.decode(e["type"], raw)
        for p in patches:
            try:
                addr = self.guest.resolve(p["address"], memo)
                on = values.encode("bytes", p["on"])
                p["resolved"] = addr
                p["enabled"] = self.guest.read(addr, len(on)) == on
            except Exception as err:
                p["error"] = str(err)
        return {"entries": entries, "patches": patches, "calls": calls, "switches": switches,
                "freeze_error": self.freeze_error}

    def symbols_text(self):
        return "\n".join("%s %s" % (k, v) for k, v in self.symbols.items())

    def set_symbols(self, text):
        symbols = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"([A-Za-z_][A-Za-z0-9_.:$@?]*)\s*[=,]?\s*(.*)$", line)
            if not m or not m.group(2):
                raise ValueError("%r needs a name then an address" % line)
            expressions.parse(m.group(2))
            symbols[m.group(1)] = m.group(2)
        for name, expr in symbols.items():
            for word in expressions.words(expressions.parse(expr)):
                if word not in symbols and not expressions.is_hex(word):
                    raise ValueError("%s uses %r, thats not a name or a hex number" % (name, word))
        with self._lock:
            self.symbols = symbols
            self.guest.symbols = symbols


    def add_patch(self, name, address, on, off="", note=""):
        self.guest.check(str(address))
        if off and len(values.encode("bytes", off)) != len(values.encode("bytes", on)):
            raise ValueError("original bytes have to be the same length as the patch")
        values.encode("bytes", on)
        with self._lock:
            patch = {"id": self._next_id, "name": name or "Unnamed patch", "address": str(address),
                     "on": on, "off": off, "note": note, "enabled": False}
            self._next_id += 1
            self.patches.append(patch)
            return patch

    def toggle_patch(self, item_id, enable):
        with self._lock:
            patch = self._find(self.patches, item_id)
        addr = self.guest.resolve(patch["address"])
        on = values.encode("bytes", patch["on"])
        if enable:
            now = self.guest.read(addr, len(on))
            if not patch.get("off") and now != on:
                patch["off"] = now.hex(" ").upper()
            self.guest.write(addr, on)
            self.log.note("%s: %d byte%s patched at %08X" % (patch["name"], len(on), "" if len(on) == 1 else "s", addr))
        else:
            if not patch.get("off"):
                raise ValueError("dont know the original bytes so this patch cant be undone")
            self.guest.write(addr, values.encode("bytes", patch["off"]))
            self.log.note("%s: original bytes put back at %08X" % (patch["name"], addr))
        patch["enabled"] = bool(enable)


    def add_call(self, name, address, args=(), string="", expect="", note="", convention="cdecl"):
        address = "0x%08X" % call_address(address)
        args = call_args(args)
        convention = call_convention(convention)
        check_call(address, args, convention)
        if expect not in (None, ""):
            number(expect)
        with self._lock:
            call = {"id": self._next_id, "name": name or "Unnamed call", "address": address, "args": args,
                    "convention": convention, "string": string or "", "expect": str(expect or ""), "note": note,
                    "last": ""}
            self._next_id += 1
            self.calls.append(call)
            return call

    def update_call(self, item_id, **fields):
        with self._lock:
            call = self._find(self.calls, item_id)
            if fields.get("address") is not None:
                call["address"] = "0x%08X" % call_address(fields["address"])
            if fields.get("args") is not None or fields.get("convention") is not None:
                args = call["args"] if fields.get("args") is None else call_args(fields["args"])
                conv = call_convention(call["convention"] if fields.get("convention") is None else fields["convention"])
                check_call(call["address"], args, conv)
                call["args"], call["convention"] = args, conv
            if fields.get("expect") is not None:
                if str(fields["expect"]).strip():
                    number(fields["expect"])
                call["expect"] = str(fields["expect"]).strip()
            for k in ("name", "string", "note"):
                if fields.get(k) is not None:
                    call[k] = str(fields[k])
            return call

    def set_call_result(self, item_id, text):
        with self._lock:
            try:
                self._find(self.calls, item_id)["last"] = text
            except KeyError:
                pass

    def get_call(self, item_id):
        with self._lock:
            return dict(self._find(self.calls, item_id))

    def resolve_call_args(self, args):
        memo, out = {}, []
        for a in args:
            reg, v = split_arg(a)
            if v.startswith("="):
                n = self.guest.resolve(v[1:], memo)
                if not n:
                    raise Empty("%s is 0, nothing there right now" % v[1:])
                v = "0x%08X" % n
            out.append("%s=%s" % (reg, v) if reg else v)
        return out


    def _check_write(self, w):
        t = str(w.get("type") or "u32")
        if t not in values.ALL:
            raise ValueError("unknown type %r" % t)
        address = str(w.get("address", "")).strip()
        self.guest.check(address)
        on, off = str(w.get("on", "")).strip(), str(w.get("off", "")).strip()
        if not on:
            raise ValueError("%s needs a value for when the switch is on" % address)
        if on == "hold" and t not in values.NUMERIC:
            raise ValueError("hold only works on numbers")
        for v in ([off] if on == "hold" else [on, off]):
            if v.startswith("="):
                if t not in values.NUMERIC or values.is_float(t) or t == "bool":
                    raise ValueError("=address values only work on whole numbers")
                if v != "=pick":
                    self.guest.check(v[1:])
            elif v and v != "restore" and not values.bit_op(t, v) and values.at_least(t, v) is None:
                values.encode(t, v)
        return {"address": address, "type": t, "on": on, "off": off}

    def add_switch(self, name, writes, note="", driver="", group=""):
        writes = [self._check_write(w) for w in writes]
        if driver and driver not in DRIVERS:
            raise ValueError("no driver called %r" % driver)
        if not writes and not driver:
            raise ValueError("a switch needs an address")
        with self._lock:
            sw = {"id": self._next_id, "name": name or "Unnamed switch", "note": note, "writes": writes,
                  "driver": driver, "group": group, "on": False}
            self._next_id += 1
            self.switches.append(sw)
            return sw

    def _group(self, sw):
        kind = DRIVERS.get(sw.get("driver", ""))
        return sw.get("group") or (kind.group if kind else "")

    def set_switch(self, item_id, on):
        sw = self._find(self.switches, item_id)
        if on and not sw.get("on") and self._group(sw):
            for other in [x for x in self.switches if x.get("on") and x is not sw and self._group(x) == self._group(sw)]:
                self.set_switch(other["id"], False)
        if sw.get("driver"):
            return self._set_driver(sw, on)
        with self._switch_lock:
            with self._lock:
                sw = self._find(self.switches, item_id)
                if bool(sw.get("on")) == bool(on):
                    return self.switch_status.get(item_id, "")
                sw["on"] = bool(on)
            if not on:
                self._switch_off(sw)
                self.log.note("%s: off" % sw["name"])
                return ""
            self._touched[item_id] = {}
            status, problem = self._apply_switch(sw, {})
            if problem:
                with self._lock:
                    sw["on"] = False
                self._switch_off(sw)
                raise ValueError("%s couldnt turn on: %s" % (sw["name"], problem))
        self.log.note("%s: on" % sw["name"])
        self._start_freezer()
        return status

    def _set_driver(self, sw, on):
        with self._switch_lock:
            if bool(sw.get("on")) == bool(on):
                return self._drivers[sw["id"]].status if on else ""
            if not on:
                driver = self._drivers.pop(sw["id"], None)
                with self._lock:
                    sw["on"] = False
                if driver:
                    driver.stop()
                self.log.note("%s: off" % sw["name"])
                return ""
            if sw["driver"] not in DRIVERS:
                raise ValueError("no driver called %r" % sw["driver"])
            driver = DRIVERS[sw["driver"]](self.guest, self.log, sw["name"])
            driver.switch, driver.names, driver.caller = sw, self.tag_names, self.caller
            driver.start()
            self._drivers[sw["id"]] = driver
            with self._lock:
                sw["on"] = True
        self.log.note("%s: on" % sw["name"])
        return driver.status


    def _tags(self):
        return (self.guest.resolve(self.tag_info["table"]), self.guest.resolve(str(self.tag_info.get("count", "2400"))),
                self.tag_info.get("layout", "halo2"))

    def tag_names(self):
        if self.tag_info.get("names") == "memory":
            table, count, _ = self._tags()
            key = (table, self.guest.read(table - 0x28, 0x28))
            if self._names[0] != key:
                self._names = (key, tagnames.names_from_memory(self.guest.read, table, count))
            return self._names[1]
        if not self.tag_info.get("map_header"):
            return []
        map_file, _ = tagnames.map_header(self.guest.read, self.guest.resolve(self.tag_info["map_header"]))
        if self._names[0] != map_file:
            path = tagnames.find_map_file(map_file, self.game_folders)
            self._names = (map_file, tagnames.names_from_file(path) if path else [])
            if path:
                folder = os.path.dirname(os.path.dirname(path))
                if folder not in self.game_folders:
                    self.game_folders.insert(0, folder)
        return self._names[1]

    @staticmethod
    def short_name(path):
        parts = [p for p in path.split("\\") if p]
        if not parts:
            return path
        context = [p for p in parts[:-1] if p not in (parts[-1], "projectiles", "objects", "weapons")]
        return "%s (%s)" % (parts[-1], context[-1]) if context else parts[-1]

    def pick_choices(self, item_id):
        sw = self._find(self.switches, item_id)
        if not sw["pick"].get("tags"):
            kind = DRIVERS.get(sw.get("driver", ""))
            if not hasattr(kind, "choices"):
                raise ValueError("%s has nothing to pick" % sw["name"])
            return kind.choices(self.guest, self.tag_names())
        group = sw["pick"]["tags"]
        table, count, layout = self._tags()
        names = self.tag_names()
        out = []
        for datum, g, _ in tagnames.loaded_tags(self.guest.read, table, (group,), count, layout):
            path = names[datum & 0xFFFF] if (datum & 0xFFFF) < len(names) else ""
            out.append({"datum": datum, "path": path,
                        "name": self.short_name(path) if path else "%s %08X" % (group, datum)})
        return sorted(out, key=lambda c: c["name"].lower())

    def set_pick(self, item_id, datum):
        sw = self._find(self.switches, item_id)
        group = sw["pick"].get("tags")
        if group:
            table, _, layout = self._tags()
            if not tagnames.is_loaded(self.guest.read, table, datum, group, layout):
                raise ValueError("%08X isnt loaded in this level" % datum)
            names = self.tag_names()
            path = names[datum & 0xFFFF] if (datum & 0xFFFF) < len(names) else ""
            choice = {"datum": datum, "path": path, "name": self.short_name(path) if path else "%s %08X" % (group, datum)}
        else:
            choice = next((c for c in self.pick_choices(item_id) if c["datum"] == datum), None)
            if choice is None:
                raise ValueError("thats not there anymore")
        tags = bool(group)
        with self._lock:
            sw["picked"] = datum
            if tags:
                sw["picked_name"] = choice["path"] or choice["name"]
        self.log.note("%s: %s" % (sw["name"], choice["name"]))
        driver = self._drivers.get(item_id)
        if driver:
            driver.repick()
        elif sw.get("on"):
            self._apply_switch(sw, {})
        return choice

    def _current_pick(self, sw):
        group = sw["pick"]["tags"]
        table, _, layout = self._tags()
        if sw.get("picked") is not None and tagnames.is_loaded(self.guest.read, table, sw["picked"], group, layout):
            return sw["picked"]
        wanted = sw.get("picked_name") or sw["pick"].get("default", "")
        if wanted:
            for choice in self.pick_choices(sw["id"]):
                if wanted in (choice["path"], choice["name"]):
                    sw["picked"] = choice["datum"]
                    return choice["datum"]
        return None

    def _hold(self, t, addr, seen):
        fmt = values.NUMERIC[t][2]
        raw = self.guest.read(addr, values.size_of(t))
        if seen is None:
            return raw, raw
        before, kept = seen
        if struct.unpack(fmt, raw)[0] > struct.unpack(fmt, kept)[0]:
            return before, raw
        if raw != kept:
            self.guest.write(addr, kept)
        return before, kept

    def _write_value(self, t, value, addr, keep_old=False, memo=None):
        if str(value).startswith("="):
            size = values.size_of(t)
            data = (self.guest.resolve(value[1:], memo) & ((1 << (8 * size)) - 1)).to_bytes(size, "little")
            old = self.guest.read(addr, size) if keep_old else None
            self.guest.write(addr, data)
            return data, old
        floor = values.at_least(t, value)
        if floor is not None:
            old = self.guest.read(addr, values.size_of(t))
            if struct.unpack(values.NUMERIC[t][2], old)[0] >= floor:
                return old, old
            data = struct.pack(values.NUMERIC[t][2], floor)
            self.guest.write(addr, data)
            return data, old
        op = values.bit_op(t, value)
        if op:
            old = self.guest.read(addr, values.size_of(t))
            new = values.apply_bits(old, op)
            if new != old:
                self.guest.write(addr, new)
            return new, old
        data = self._encode(t, value)
        old = self.guest.read(addr, len(data)) if keep_old else None
        self.guest.write(addr, data)
        return data, old

    def _apply_switch(self, sw, memo):
        with self._switch_lock:
            if not sw.get("on"):
                return "", ""
            touched = self._touched.setdefault(sw["id"], {})
            if (sw.get("pick") or {}).get("tags") and "level" not in touched:
                touched["level"] = self._level()
            done, waiting, problem = 0, "", ""
            picked = None
            if sw.get("pick"):
                try:
                    picked = self._current_pick(sw)
                except Exception as err:
                    problem = describe(err)
                if picked is None and not problem:
                    self.switch_status[sw["id"]] = "waiting: pick one for this level"
                    return self.switch_status[sw["id"]], ""
            for i, w in enumerate(sw["writes"] if not problem else []):
                try:
                    addr = self.guest.resolve(w["address"], memo)
                    first = (i, addr) not in touched
                    if w["on"] == "hold":
                        touched[(i, addr)] = self._hold(w["type"], addr, None if first else touched[(i, addr)])
                        done += 1
                        continue
                    on = "=0x%X" % picked if w["on"] == "=pick" else w["on"]
                    data, old = self._write_value(w["type"], on, addr, keep_old=first and w["off"] == "restore",
                                                  memo=memo)
                    touched[(i, addr)] = (old if first else touched[(i, addr)][0], data)
                    done += 1
                except Empty as err:
                    waiting = str(err)
                except Exception as err:
                    problem = "%s: %s" % (w["address"], describe(err))
            status = "problem: %s" % problem if problem else "waiting: %s" % waiting if waiting and not done else "on"
            self.switch_status[sw["id"]] = status
            return status, problem

    def _switch_off(self, sw):
        with self._switch_lock:
            touched = self._touched.pop(sw["id"], {})
            self.switch_status.pop(sw["id"], None)
            memo = {}
            put_back = set()
            for i, w in reversed(list(enumerate(sw["writes"]))):
                if not w["off"]:
                    continue
                try:
                    addr = self.guest.resolve(w["address"], memo)
                    if w["off"] != "restore":
                        self._write_value(w["type"], w["off"], addr, memo=memo)
                    elif touched.get((i, addr), (None,))[0] is not None:
                        self._restore(w, addr, *touched[(i, addr)])
                        put_back.add((i, addr))
                except Empty:
                    continue
                except Exception as err:
                    self.log.warn("%s: couldnt put back %s: %s" % (sw["name"], w["address"], describe(err)),
                                  key=("switch", sw["name"]))
            if touched.get("level") is None:
                return
            try:
                if touched["level"] != self._level():
                    return
                stale = sorted((k for k in touched if k != "level" and k not in put_back), reverse=True)
                for i, addr in stale:
                    w = sw["writes"][i]
                    if w["off"] == "restore" and touched[(i, addr)][0] is not None:
                        self._restore(w, addr, *touched[(i, addr)])
            except Exception as err:
                self.log.warn("%s: couldnt put back the tags it changed: %s" % (sw["name"], describe(err)),
                              key=("switch", sw["name"]))

    def _level(self):
        try:
            if self.tag_info.get("names") == "memory":
                table = self.guest.resolve(self.tag_info["table"])
                return (table, self.guest.read(table - 0x28, 0x28))
            if self.tag_info.get("map_header"):
                return tagnames.map_header(self.guest.read, self.guest.resolve(self.tag_info["map_header"]))
        except Exception:
            pass
        return None

    def _restore(self, w, addr, old, wrote):
        now = self.guest.read(addr, len(old))
        if values.bit_op(w["type"], w["on"]):
            changed = int.from_bytes(old, "little") ^ int.from_bytes(wrote, "little")
            current = int.from_bytes(now, "little")
            if changed and current & changed == int.from_bytes(wrote, "little") & changed:
                back = (current & ~changed) | (int.from_bytes(old, "little") & changed)
                self.guest.write(addr, back.to_bytes(len(old), "little"))
        elif now == wrote:
            self.guest.write(addr, old)

    def all_off(self):
        on = [sw for sw in self.switches if sw.get("on")]
        for sw in on:
            try:
                self.set_switch(sw["id"], False)
            except Exception:
                pass
        return [sw["name"] for sw in on] + self.unfreeze_all()


    def _start_freezer(self):
        if self._thread and self._thread.is_alive():
            return
        if any(e.get("frozen") for e in self.entries) or any(s.get("on") for s in self.switches):
            self._stop.clear()
            self._thread = threading.Thread(target=self._freeze_loop, name="og360-freeze", daemon=True)
            self._thread.start()

    def _freeze_loop(self):
        failing = set()
        while not self._stop.is_set():
            if not self.freeze_once(failing):
                return
            time.sleep(FREEZE_INTERVAL)

    def freeze_once(self, failing=None):
        failing = set() if failing is None else failing
        with self._lock:
            frozen = [dict(e) for e in self.entries if e.get("frozen")]
            active = [s for s in self.switches if s.get("on") and not s.get("driver")]
        if (frozen or active) and not self.guest.still_there():
            names = self.unfreeze_all()
            self.log.warn("the game is gone so OG360 stopped writing: %s" % ", ".join(names), key="gone")
            return False
        memo = {}
        for e in frozen:
            try:
                addr = self.guest.resolve(e["address"], memo)
                op = values.bit_op(e["type"], e["value"])
                data = self._bits(e, addr, op) if op else values.encode(e["type"], e["value"])
                if data is not None:
                    self.guest.write(addr, data)
                if e["name"] in failing:
                    failing.discard(e["name"])
                    self.log.info("lock %s works again" % e["name"], key=("freeze", e["name"]))
                self.freeze_error = ""
            except Empty:
                continue
            except Exception as err:
                self.freeze_error = "%s: %s" % (e["name"], err)
                failing.add(e["name"])
                self.log.warn("Freeze %s: %s" % (e["name"], describe(err)), key=("freeze", e["name"]))
        for sw in active:
            status, problem = self._apply_switch(sw, memo)
            if problem:
                self.freeze_error = "%s: %s" % (sw["name"], problem)
                self.log.warn("%s: %s" % (sw["name"], problem), key=("switch", sw["name"]))
        return bool(frozen or active)

    def release_all(self):
        had = self.all_off()
        with self._lock:
            patches = [p for p in self.patches if p.get("enabled")]
        for patch in patches:
            try:
                self.toggle_patch(patch["id"], False)
                had.append(patch["name"])
            except Exception as err:
                self.log.warn("%s: couldnt put the original bytes back: %s" % (patch["name"], describe(err)),
                              key=("patch", patch["name"]))
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        return had

    def unfreeze_all(self):
        with self._lock:
            had = [e["name"] for e in self.entries if e.get("frozen")] + [s["name"] for s in self.switches if s.get("on")]
            for e in self.entries:
                e["frozen"] = False
            for sw in self.switches:
                sw["on"] = False
            self._touched, self.switch_status = {}, {}
            drivers, self._drivers = list(self._drivers.values()), {}
        for driver in drivers:
            driver.stop(put_back=False)
        self._stop.set()
        return had
