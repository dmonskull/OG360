import time
from tkinter import ttk

from ..og.snapshot import Snapshot
from .common import AreaPicker, hex8, make_tree, size_text


class ChangesTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.snap = None

        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text="Area").pack(side="left")
        self.area = AreaPicker(row)
        self.area.pack(side="left", padx=6)
        ttk.Button(row, text="Take first snapshot", command=self.take).pack(side="left", padx=6)

        self.state = ttk.Label(self, foreground=app.pal["dim"], text=(
            "take a snapshot, do something in game, then hit a button below to see every byte that changed"))
        self.state.pack(fill="x", pady=(8, 0))

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(10, 6))
        self.buttons = [
            ttk.Button(row, text="Changed since first snapshot", command=lambda: self.diff("first")),
            ttk.Button(row, text="Changed since last check", command=lambda: self.diff("last")),
            ttk.Button(row, text="Ignore whats changing now", command=self.ignore),
        ]
        for b in self.buttons:
            b.configure(state="disabled")
            b.pack(side="left", padx=(0, 6))

        self.summary = ttk.Label(self, foreground=app.pal["dim"], text="")
        self.summary.pack(fill="x", pady=(0, 6))

        frame, self.tree = make_tree(self, (
            ("addr", "Address", 110, "w"), ("size", "Bytes", 60, "e"), ("old", "Before", 250, "w"),
            ("new", "After", 250, "w"), ("num", "As a number (before -> after)", 300, "w")), height=17)
        frame.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.view())

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="View in Memory", command=self.view).pack(side="left")
        ttk.Button(row, text="Add to mods...", command=self.add_mod).pack(side="left", padx=6)
        ttk.Label(row, foreground=app.pal["dim"], text=(
            "tip: hit ignore whats changing now while nothing is happening to hide timers and animations, "
            "then do your thing and check again")).pack(side="left", padx=8)

    def on_game(self):
        self.snap = None
        self.tree.delete(*self.tree.get_children())
        for b in self.buttons:
            b.configure(state="disabled")
        if self.app.s.running:
            self.app.tasks.run(self.app.s.areas, self.area.set_areas)

    def take(self):
        area, lo, hi = self.area.get()

        def work(progress):
            snap = Snapshot(self.app.s.need_game(), self.app.s.regions(area, lo, hi))
            snap.take(progress)
            return snap

        def done(snap):
            self.snap = snap
            self.tree.delete(*self.tree.get_children())
            for b in self.buttons:
                b.configure(state="normal")
            text = "first snapshot: %s taken at %s" % (
                size_text(snap.total_bytes()), time.strftime("%H:%M:%S", time.localtime(snap.taken_at)))
            self.state.configure(text=text)
            self.summary.configure(text="")
            self.app.say(text)

        self.app.job("Take a snapshot", work, done, log_result=lambda snap: size_text(snap.total_bytes()))

    def diff(self, against):
        snap = self.snap
        if not snap:
            return

        def done(result):
            self.tree.delete(*self.tree.get_children())
            for r in result["rows"]:
                num = ""
                if "as_float" in r:
                    num = "float  %s -> %s" % tuple(r["as_float"])
                if "as_int" in r:
                    num += ("     " if num else "") + "int  %d -> %d" % tuple(r["as_int"])
                more = " ..." if r["size"] > 32 else ""
                self.tree.insert("", "end", iid=str(r["addr"]), values=(
                    hex8(r["addr"]), r["size"], r["old"] + more, r["new"] + more, num))
            text = "%s change%s (%s) since %s" % (
                format(result["changes"], ","), "" if result["changes"] == 1 else "s",
                size_text(result["changed_bytes"]),
                "the first snapshot" if against == "first" else "your last check")
            if result["changes"] > len(result["rows"]):
                text += ", showing the first %d" % len(result["rows"])
            if result["ignored_bytes"]:
                text += ", %s noisy bytes ignored" % format(result["ignored_bytes"], ",")
            self.summary.configure(text=text)
            self.app.say(text)

        self.app.job("check what changed since %s" % ("the first snapshot" if against == "first" else "the last check"),
                     lambda progress: snap.diff(against, 500, progress), done,
                     log_result=lambda r: "%s change%s" % (format(r["changes"], ","), "" if r["changes"] == 1 else "s"))

    def ignore(self):
        snap = self.snap
        if not snap:
            return

        def done(result):
            text = "ignoring %s bytes that change on their own (%s just added)" % (
                format(result["ignored_bytes"], ","), format(result["ignored_now"], ","))
            self.summary.configure(text=text)
            self.app.say(text)

        self.app.job("Ignore whats changing now", snap.ignore_changes, done,
                     log_result=lambda r: "%s bytes ignored" % format(r["ignored_bytes"], ","))

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def view(self):
        addr = self._selected()
        if addr is not None:
            self.app.goto(addr)

    def add_mod(self):
        addr = self._selected()
        if addr is None:
            return
        num, size = self.tree.set(str(addr), "num"), int(self.tree.set(str(addr), "size"))
        self.app.add_mod(hex8(addr), "float" if "float" in num else "u32" if size == 4 else "u8")
