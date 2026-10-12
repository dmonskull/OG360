import collections
import contextlib
import datetime
import itertools
import os
import threading
import time

MAX_LINES = 400
MAX_CHILDREN = 40
REPEAT_LOOKBACK = 25
REPEAT_SECONDS = 600
MAX_DETAIL = 200
SHOWN_COMMAND = 110
FILE_REPEAT_EVERY = 60

QUERIES = {"modules", "threads", "threadinfo", "getcontext", "walkmem", "dbgname", "xbeinfo",
           "isstopped", "dmversion", "systeminfo", "drivelist", "dirlist", "getpid",
           "consoletype", "getconsoleid", "altaddr"}

ICONS = {"info": "•", "ok": "✓", "warn": "!", "error": "✗"}
STATE_ICONS = {"running": "…", "ok": "✓", "failed": "✗"}
INDENT = " " * 12


def describe(error):
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    text = str(error).strip()
    return text or type(error).__name__


def verb_of(command):
    parts = command.split(None, 1)
    return parts[0].lower() if parts else ""


def size_text(n):
    if n < 1024:
        return "%d byte%s" % (n, "" if n == 1 else "s")
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.1f MB" % (n / 2 ** 20)


def clip(text, limit=SHOWN_COMMAND):
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Entry:
    __slots__ = ("id", "kind", "level", "text", "key", "count", "first", "last", "detail",
                 "children", "state", "result", "duration", "reads", "read_bytes", "dropped",
                 "verb", "reply", "started", "file_count", "file_time", "queries")

    def __init__(self, eid, kind, level, text, key=None):
        self.id = eid
        self.kind = kind
        self.level = level
        self.text = text
        self.key = key if key is not None else (kind, text)
        self.count = 1
        self.first = self.last = time.time()
        self.detail = []
        self.children = []
        self.state = None
        self.result = ""
        self.duration = None
        self.reads = 0
        self.read_bytes = 0
        self.dropped = 0
        self.verb = ""
        self.reply = ""
        self.started = time.monotonic()
        self.file_count = 1
        self.file_time = time.time()
        self.queries = {}


class Action:
    def __init__(self, log, entry):
        self._log = log
        self.entry = entry

    @property
    def finished(self):
        return self.entry.state != "running"

    def step(self, text, level="info", detail=None):
        self._log._step(self, text, level, detail)

    def done(self, result="", level="ok"):
        self._log._finish(self, "failed" if level == "error" else "ok", result, level)

    def fail(self, error):
        self._log._finish(self, "failed", describe(error), "error")

    def __enter__(self):
        self._log._push(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        self._log._pop(self)
        if not self.finished:
            if exc is not None:
                self.fail(exc)
            else:
                self.done()
        return False


class ActivityLog:
    def __init__(self, folder=None):
        self._lock = threading.RLock()
        self._ids = itertools.count(1)
        self._lines = collections.deque()
        self._local = threading.local()
        self.version = 0
        self.show_background = False
        self.folder = folder
        self._file = None
        self._file_name = None
        self.background_reads = 0
        self.background_bytes = 0


    def current(self):
        stack = getattr(self._local, "stack", None)
        return stack[-1] if stack else None

    def _push(self, action):
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        self._local.stack.append(action)

    def _pop(self, action):
        stack = getattr(self._local, "stack", [])
        if action in stack:
            stack.remove(action)

    @contextlib.contextmanager
    def bound(self, action):
        if action is None:
            yield
            return
        self._push(action)
        try:
            yield
        finally:
            self._pop(action)


    def event(self, text, level="info", key=None, detail=None):
        with self._lock:
            key = ("event", key if key is not None else text)
            entry = self._repeat(key)
            if entry is not None:
                entry.count += 1
                entry.last = time.time()
                entry.text, entry.level = text, level
                if detail:
                    entry.detail = list(detail)[:MAX_DETAIL]
                self._to_end(entry)
                self._file_repeat(entry)
            else:
                entry = Entry(next(self._ids), "event", level, text, key)
                if detail:
                    entry.detail = list(detail)[:MAX_DETAIL]
                self._append(entry)
                self._write(["%s %s" % (self._stamp(entry.last), self._plain(entry))] +
                            [INDENT + line for line in entry.detail])
            self.version += 1
            return entry

    def info(self, text, key=None, detail=None):
        return self.event(text, "info", key, detail)

    def ok(self, text, key=None, detail=None):
        return self.event(text, "ok", key, detail)

    def warn(self, text, key=None, detail=None):
        return self.event(text, "warn", key, detail)

    def error(self, text, key=None, detail=None):
        return self.event(text, "error", key, detail)

    def note(self, text, level="info", detail=None):
        act = self.current()
        if act is not None and not act.finished:
            act.step(text, level, detail)
        else:
            self.event(text, level, detail=detail)

    def action(self, title):
        with self._lock:
            entry = Entry(next(self._ids), "action", "info", title)
            entry.state = "running"
            self._append(entry)
            self.version += 1
        return Action(self, entry)

    def exchange(self, command, reply, ok=True, detail=None):
        act = self.current()
        level = "info" if ok else "error"
        with self._lock:
            if act is not None and not act.finished:
                self._exchange_under(act.entry, command, reply, level, detail)
            elif self.show_background:
                key = ("bg", command, reply)
                entry = self._repeat(key)
                if entry is not None:
                    entry.count += 1
                    entry.last = time.time()
                    self._to_end(entry)
                else:
                    entry = Entry(next(self._ids), "xchg", level, command, key)
                    entry.verb, entry.reply = verb_of(command), reply
                    entry.detail = ["→ %s   ← %s" % (command, reply)] + list(detail or [])[:MAX_DETAIL]
                    self._append(entry)
            else:
                return
            self.version += 1

    def query(self, command, reply, detail=None):
        act = self.current()
        if act is None or act.finished:
            self.exchange(command, reply, True, detail)
            return
        with self._lock:
            verb = verb_of(command)
            act.entry.queries[verb] = act.entry.queries.get(verb, 0) + 1
            self.version += 1

    def read(self, size):
        act = self.current()
        with self._lock:
            if act is not None and not act.finished:
                act.entry.reads += 1
                act.entry.read_bytes += size
                self.version += 1
            else:
                self.background_reads += 1
                self.background_bytes += size


    def _exchange_under(self, parent, command, reply, level, detail):
        verb = verb_of(command)
        line = "→ %s   ← %s" % (command, reply)

        def same(child):
            return (child.kind == "xchg" and child.verb == verb and child.level == level
                    and child.reply[:3] == reply[:3])

        for back in (1, 2):
            if len(parent.children) < back:
                break
            last = parent.children[-back]
            if same(last) and (back == 1 or parent.children[-1].kind == "xchg"):
                last.count += 1
                last.last = time.time()
                last.text, last.reply = command, reply
                if len(last.detail) < MAX_DETAIL:
                    last.detail.append(line)
                return
        entry = Entry(next(self._ids), "xchg", level, command)
        entry.verb, entry.reply = verb, reply
        entry.detail = [line] + list(detail or [])[:MAX_DETAIL]
        self._add_child(parent, entry)

    def _step(self, action, text, level, detail):
        with self._lock:
            parent = action.entry
            last = parent.children[-1] if parent.children else None
            if last is not None and last.kind == "step" and last.text == text and last.level == level:
                last.count += 1
                last.last = time.time()
            else:
                entry = Entry(next(self._ids), "step", level, text)
                entry.detail = list(detail or [])[:MAX_DETAIL]
                self._add_child(parent, entry)
            self.version += 1

    def _add_child(self, parent, entry):
        parent.children.append(entry)
        if len(parent.children) > MAX_CHILDREN:
            parent.children.pop(0)
            parent.dropped += 1

    def _finish(self, action, state, result, level):
        with self._lock:
            entry = action.entry
            if entry.state != "running":
                return
            entry.state, entry.result, entry.level = state, result, level
            entry.duration = time.monotonic() - entry.started
            entry.last = time.time()
            entry.key = ("action", entry.text, state, result)
            earlier = self._repeat(entry.key, exclude=entry)
            if earlier is not None:
                earlier.count += 1
                earlier.last = entry.last
                earlier.duration = entry.duration
                earlier.children = entry.children
                earlier.dropped = entry.dropped
                earlier.reads, earlier.read_bytes = entry.reads, entry.read_bytes
                earlier.queries = entry.queries
                with contextlib.suppress(ValueError):
                    self._lines.remove(entry)
                self._to_end(earlier)
                action.entry = earlier
                self._file_repeat(earlier)
            else:
                self._write(self._file_lines(entry))
            self.version += 1


    def _repeat(self, key, exclude=None):
        now = time.time()
        for i, entry in enumerate(reversed(self._lines)):
            if i >= REPEAT_LOOKBACK:
                break
            if (entry is not exclude and entry.key == key and now - entry.last < REPEAT_SECONDS
                    and entry.state != "running"):
                return entry
        return None

    def _append(self, entry):
        self._lines.append(entry)
        while len(self._lines) > MAX_LINES:
            self._lines.popleft()

    def _to_end(self, entry):
        with contextlib.suppress(ValueError):
            self._lines.remove(entry)
        self._lines.append(entry)

    def clear(self):
        with self._lock:
            self._lines.clear()
            self.version += 1


    @staticmethod
    def _stamp(t):
        return time.strftime("%H:%M:%S", time.localtime(t))

    @staticmethod
    def _count(entry):
        return "  ×%d" % entry.count if entry.count > 1 else ""

    def _plain(self, entry):
        if entry.kind == "action":
            icon = "!" if entry.state == "ok" and entry.level == "warn" else STATE_ICONS.get(entry.state, "")
            text = "%s %s" % (icon, entry.text)
            if entry.result:
                text += "  →  " + entry.result
            if entry.duration is not None:
                text += "  (%s)" % (("%.1f s" % entry.duration) if entry.duration >= 0.1
                                    else "%d ms" % max(1, round(entry.duration * 1000)))
            return text + self._count(entry)
        if entry.kind == "xchg":
            return "→ %s   ← %s%s" % (clip(entry.text), entry.reply, self._count(entry))
        return "%s %s%s" % (ICONS.get(entry.level, ""), entry.text, self._count(entry))

    @staticmethod
    def _tally(entry):
        parts = []
        if entry.reads:
            parts.append("%d memory read%s (%s)" % (entry.reads, "" if entry.reads == 1 else "s",
                                                    size_text(entry.read_bytes)))
        if entry.queries:
            parts.append(", ".join("%s ×%d" % (verb, n) if n > 1 else verb
                                   for verb, n in sorted(entry.queries.items())))
        return ", ".join(parts)

    def _child_text(self, child):
        if child.kind == "xchg":
            shown = clip(child.text) if child.count == 1 else "%s ×%d" % (child.verb, child.count)
            return "→ %s   ← %s" % (shown, child.reply)
        return "· %s%s" % (child.text, self._count(child))

    def lines(self, limit=None):
        out = []
        with self._lock:
            entries = list(self._lines)
            if limit:
                entries = entries[-limit:]
            for entry in entries:
                stamp = self._stamp(entry.last)
                if entry.kind == "action":
                    level = ("running" if entry.state == "running" else "error" if entry.state == "failed"
                             else "warn" if entry.level == "warn" else "ok")
                    out.append((entry.id, level, "action", "%s  %s" % (stamp, self._plain(entry))))
                    if entry.dropped:
                        out.append((entry.id, "dim", "child", "%s… %d earlier line%s hidden" % (
                            INDENT, entry.dropped, "" if entry.dropped == 1 else "s")))
                    for child in entry.children:
                        out.append((child.id, "error" if child.level == "error" else
                                    "warn" if child.level == "warn" else
                                    "tx" if child.kind == "xchg" else "dim", "child",
                                    INDENT + self._child_text(child)))
                    summary = self._tally(entry)
                    if summary:
                        out.append((entry.id, "dim", "child", INDENT + "⋯ " + summary))
                else:
                    level = "tx" if entry.kind == "xchg" else entry.level
                    out.append((entry.id, level, entry.kind, "%s  %s" % (stamp, self._plain(entry))))
        return out

    def find(self, eid):
        with self._lock:
            for entry in self._lines:
                if entry.id == eid:
                    return entry
                for child in entry.children:
                    if child.id == eid:
                        return child
        return None

    def details(self, eid):
        entry = self.find(eid)
        if entry is None:
            return ""
        with self._lock:
            out = ["%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.last)),
                               self._plain(entry) if entry.kind != "step" else entry.text)]
            if entry.count > 1:
                out.append("happened %d times, first at %s" % (entry.count, self._stamp(entry.first)))
            out += entry.detail
            for child in entry.children:
                out.append(INDENT + self._child_text(child))
                out += [INDENT + "    " + line for line in child.detail]
            summary = self._tally(entry)
            if summary:
                out.append(INDENT + "⋯ " + summary)
        return "\n".join(out)

    def text(self):
        return "\n".join(text for _, _, _, text in self.lines()) + "\n"


    def _file_lines(self, entry):
        out = ["%s %s" % (self._stamp(entry.last), self._plain(entry))]
        if entry.kind == "action":
            out += [INDENT + self._child_text(c) for c in entry.children]
            summary = self._tally(entry)
            if summary:
                out.append(INDENT + "⋯ " + summary)
        out += [INDENT + line for line in entry.detail]
        return out

    def _file_repeat(self, entry):
        now = time.time()
        if now - entry.file_time >= FILE_REPEAT_EVERY:
            self._write(["%s %s  (repeated, %d times so far)" % (
                self._stamp(now), self._plain(entry).split("  ×")[0], entry.count)])
            entry.file_time = now

    def _write(self, lines):
        if not self.folder or not lines:
            return
        try:
            name = os.path.join(self.folder, "og360-%s.log" % datetime.date.today().isoformat())
            if name != self._file_name:
                if self._file:
                    self._file.close()
                os.makedirs(self.folder, exist_ok=True)
                self._file = open(name, "a", encoding="utf-8")
                self._file_name = name
            self._file.write("\n".join(lines) + "\n")
            self._file.flush()
        except OSError:
            self._file = self._file_name = None


class NullLog(ActivityLog):
    def __init__(self):
        super().__init__(None)
