import json
import os
import re
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import paths
from ..log import describe
from ..og import caller, values
from ..og.mods import call_args, call_items, split_arg
from .common import HEADING, MONO, TITLE, Form, ScrollFrame, hex8, make_tree, open_folder

TYPE_LABELS = [values.LABELS[t] for t in values.ALL]
ADDRESS_NOTE = ("address from ghidra (hex). names and formulas work too: BB85B8 + 2*6140, [C01234] + 10 to "
                "follow a pointer, datum([array], handle) for halo objects. |0x40 or &~0x08 only flips those bits")
CALL_NOTE = ("up to 6 stack args, space separated: 50000  0x10  -1  f:1.5 (float)  @string (the text below)  "
             "@wstring (same but utf-16, halo 2 uses it). pick the convention ghidra shows: thiscall = object "
             "first, fastcall = first two in ecx and edx. for __usercall set registers: eax=5  esi=0x1234  ebx=1  ebp=2, "
             "and float registers xmm0=1.5 to xmm3 (those calls save and restore the games fpu around them). "
             "=address reads a value from the game when the call runs: =car  ecx==[player+234] (0 there stops "
             "the call). eax, edx, any float result and xmm0 show after")
CONVENTIONS = list(caller.CONVENTIONS)
NO_CHECK = "no, just show what eax returns"


def shown(text):
    return "".join(ch if ch.isprintable() else "\\u%04x" % ord(ch) for ch in text)


def call_text(c):
    parts = []
    for a in c.get("args", []):
        reg, _, a = a.partition("=") if "=" in a and not a.startswith("=") else ("", "", a)
        if a in caller.TEXTS:
            a = '%s"%s"' % ("L" if a == "@wstring" else "", shown(c.get("string", "")))
        elif a.lower().startswith("f:"):
            a = a[2:] + "f"
        parts.append("%s=%s" % (reg, a) if reg else a)
    conv = c.get("convention", "cdecl")
    return "%s%s(%s)" % ("" if conv == "cdecl" else conv + " ", c["address"], ", ".join(parts))


def type_id(label):
    return values.ALL[TYPE_LABELS.index(label)] if label in TYPE_LABELS else "u32"


def summary(note):
    m = re.match(r"(.+?[.!?])(\s|$)", note or "")
    text = m.group(1) if m else (note or "")
    return text if len(text) <= 90 else text[:87] + "..."


class OGModsTab(ScrollFrame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.data = {"entries": [], "patches": [], "calls": [], "switches": []}
        self.loading = False
        self.shape = None
        self.switch_shape = None
        self.switch_rows = {}
        self.switch_pending = {}
        self.pickers = {}
        self.calling = False
        self.last_call = None
        body = self.inner
        dim = app.pal["dim"]

        row = ttk.Frame(body)
        row.pack(fill="x")
        self.title = ttk.Label(row, text="Mods", font=TITLE)
        self.title.pack(side="left")
        ttk.Button(row, text="Names...", command=self.names).pack(side="right")
        ttk.Button(row, text="Export...", command=self.export).pack(side="right", padx=6)
        ttk.Button(row, text="Import...", command=self.import_).pack(side="right")
        self.file_note = ttk.Label(body, foreground=dim, text="")
        self.file_note.pack(fill="x", pady=(2, 8))

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(0, 4))
        ttk.Label(row, text="Switches", font=HEADING).pack(side="left")
        ttk.Label(row, foreground=dim, text=(
            "   click a mod to turn it on or off. off puts things back")).pack(side="left")
        ttk.Button(row, text="Delete a switch...", command=self.delete_switch).pack(side="right")
        self.switch_box = ttk.Frame(body)
        self.switch_box.pack(fill="x")
        self.switch_box.columnconfigure(3, weight=1)

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(16, 4))
        ttk.Label(row, text="Values", font=HEADING).pack(side="left")
        ttk.Label(row, foreground=dim, text=(
            "   see and set values. lock keeps one at your value")).pack(side="left")
        frame, self.tree = make_tree(body, (
            ("frozen", "Lock", 60, "center"), ("name", "Name", 250, "w"), ("addr", "Address", 230, "w"),
            ("type", "Type", 130, "w"), ("live", "In game", 170, "w"), ("value", "Your value", 150, "w")),
            height=6)
        frame.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selected_changed())
        self.tree.bind("<Double-1>", lambda e: self.edit())
        self.tree.bind("<ButtonRelease-1>", self._clicked_lock)
        self.tree.tag_configure("frozen", foreground=app.pal["ok"])
        self.tree.tag_configure("bad", foreground=app.pal["bad"])

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Add...", command=self.add).pack(side="left")
        ttk.Button(row, text="Edit...", command=self.edit).pack(side="left", padx=6)
        ttk.Button(row, text="Delete", command=self.delete).pack(side="left")
        ttk.Button(row, text="View in Memory", command=self.view).pack(side="left", padx=6)
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(row, text="New value").pack(side="left")
        self.value = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.value, width=16, font=MONO)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self.set_now())
        ttk.Button(row, text="Set now", command=self.set_now).pack(side="left")
        self.lock_btn = ttk.Button(row, text="Lock", command=self.toggle_lock, width=18)
        self.lock_btn.pack(side="left", padx=6)
        ttk.Button(row, text="Make a switch...", command=self.make_switch).pack(side="left")

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(16, 4))
        ttk.Label(row, text="Function calls", font=HEADING).pack(side="left")
        ttk.Label(row, foreground=dim, text=(
            "   call game code by its address in ida/ghidra (works for any game)")).pack(side="left")

        form = ttk.Frame(body)
        form.pack(fill="x")
        self.call_addr = tk.StringVar(value="0x")
        self.call_args = tk.StringVar()
        self.call_string = tk.StringVar()
        self.call_scratch = tk.StringVar()
        self.call_conv = tk.StringVar(value="cdecl")
        for label, var, width in (("Address", self.call_addr, 12), ("Convention", self.call_conv, 8),
                                  ("Arguments", self.call_args, 30), ("Text", self.call_string, 18)):
            ttk.Label(form, text=label).pack(side="left", padx=(0 if label == "Address" else 12, 4))
            if var is self.call_conv:
                ttk.Combobox(form, textvariable=var, values=CONVENTIONS, width=width, state="readonly").pack(side="left")
                continue
            e = ttk.Entry(form, textvariable=var, width=width, font=MONO)
            e.pack(side="left")
            e.bind("<Return>", lambda ev: self.call_now())
        self.call_btn = ttk.Button(form, text="Call", command=self.call_now)
        self.call_btn.pack(side="left", padx=(14, 0))
        ttk.Button(form, text="Add to table...", command=self.save_call).pack(side="left", padx=6)

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(3, 0))
        ttk.Label(row, foreground=dim, text=CALL_NOTE, wraplength=560, justify="left").pack(side="left")
        ttk.Entry(row, textvariable=self.call_scratch, width=10, font=MONO).pack(side="right")
        ttk.Label(row, foreground=dim, text="Scratch (advanced)").pack(side="right", padx=4)

        self.call_result = ttk.Label(body, text="", font=MONO)
        self.call_result.pack(anchor="w", pady=(6, 0))
        self.call_detail = ttk.Label(body, foreground=dim, text="", wraplength=1150, justify="left")
        self.call_detail.pack(anchor="w")

        ttk.Label(body, text="Saved calls", foreground=dim).pack(anchor="w", pady=(6, 2))
        frame, self.ctree = make_tree(body, (
            ("name", "Name", 300, "w"), ("call", "Call", 300, "w"), ("expect", "Check", 130, "w"),
            ("last", "Last result", 380, "w")), height=3)
        frame.pack(fill="x")
        self.ctree.bind("<Double-1>", lambda e: self.run_call())
        self.ctree.tag_configure("bad", foreground=app.pal["bad"])
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Run", command=self.run_call).pack(side="left")
        ttk.Button(row, text="Load it above", command=self.load_call).pack(side="left", padx=6)
        ttk.Button(row, text="Add...", command=self.add_call).pack(side="left")
        ttk.Button(row, text="Edit...", command=self.edit_call).pack(side="left", padx=6)
        ttk.Button(row, text="Delete", command=self.delete_call).pack(side="left")
        ttk.Button(row, text="Reports folder", command=lambda: open_folder(paths.path("reports"))).pack(side="left", padx=6)

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(16, 4))
        ttk.Label(row, text="Byte patches", font=HEADING).pack(side="left")
        ttk.Label(row, foreground=dim, text=(
            "   flip bytes on and off. data changes right away, code only if the game hasnt run it yet")).pack(side="left")
        frame, self.ptree = make_tree(body, (
            ("on", "On", 50, "center"), ("name", "Name", 250, "w"), ("addr", "Address", 160, "w"),
            ("bytes", "Patch bytes", 230, "w"), ("orig", "Original bytes", 230, "w")), height=3)
        frame.pack(fill="x")
        self.ptree.tag_configure("on", foreground=app.pal["ok"])
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Add patch...", command=self.add_patch).pack(side="left")
        ttk.Button(row, text="Turn on", command=lambda: self.toggle(True)).pack(side="left", padx=6)
        ttk.Button(row, text="Turn off", command=lambda: self.toggle(False)).pack(side="left")
        ttk.Button(row, text="Delete patch", command=self.delete_patch).pack(side="left", padx=6)
        self.after(1500, self._tick)


    def on_game(self):
        self.shape = None
        if not self.app.s.running:
            self.title.configure(text="Mods")
            self.file_note.configure(text="start an og xbox game and its table loads here")
            self.data = {"entries": [], "patches": [], "calls": [], "switches": []}
            self._draw()
            return
        m = self.app.s.mods
        self.title.configure(text="Mods for %s" % m.title)
        self.file_note.configure(text="changes last till OG360 closes. every start is the default table with everything off. export "
                                      "saves your changes to a file")
        self.refresh()

    def _tick(self):
        if self.app.mods.visible_child(self) and self.app.s.running:
            self.refresh()
        self.after(1500, self._tick)

    def refresh(self):
        if self.loading or not self.app.s.running:
            return
        self.loading = True

        def done(data):
            self.loading = False
            self.data = data
            self._draw()
            if data.get("freeze_error"):
                self.app.say("lock problem: %s" % data["freeze_error"])

        def fail(_):
            self.loading = False

        self.app.tasks.run(self.app.s.mods.live, done, fail)

    def _draw(self):
        self._draw_switches(self.data.get("switches", []))
        entries, patches, calls = self.data["entries"], self.data["patches"], self.data.get("calls", [])
        shape = ([e["id"] for e in entries], [p["id"] for p in patches], [c["id"] for c in calls])
        if shape != self.shape:
            keep = self.tree.selection(), self.ctree.selection()
            self.tree.delete(*self.tree.get_children())
            self.ptree.delete(*self.ptree.get_children())
            self.ctree.delete(*self.ctree.get_children())
            for e in entries:
                self.tree.insert("", "end", iid=str(e["id"]))
            for p in patches:
                self.ptree.insert("", "end", iid=str(p["id"]))
            for c in calls:
                self.ctree.insert("", "end", iid=str(c["id"]))
            self.shape = shape
            for tree, iids in zip((self.tree, self.ctree), keep):
                for iid in iids:
                    if tree.exists(iid):
                        tree.selection_add(iid)
        for c in calls:
            check = "eax must be %s" % c["expect"] if c.get("expect") else ""
            self.ctree.item(str(c["id"]), values=(c["name"], call_text(c), check, c.get("last", "")),
                            tags=("bad",) if c.get("last", "").startswith("failed") else ())
        for e in entries:
            live = e.get("live")
            address = e["address"]
            if "resolved" in e and hex8(e["resolved"]) != address.upper().zfill(8):
                address = "%s  = %s" % (address, hex8(e["resolved"]))
            self.tree.item(str(e["id"]), values=(
                "on" if e.get("frozen") else "", e["name"], address, values.LABELS[e["type"]],
                e.get("error", "") if live is None else live, e.get("value", "")),
                tags=("bad",) if live is None else ("frozen",) if e.get("frozen") else ())
        self._lock_label()
        for p in patches:
            self.ptree.item(str(p["id"]), values=(
                "on" if p.get("enabled") else "", p["name"], p["address"], p["on"],
                p.get("off", "") or "(read when it turns on)"), tags=("on",) if p.get("enabled") else ())

    def _picked(self, tree, items, what):
        sel = tree.selection()
        if not sel:
            self.app.say("select %s first" % what)
            return None
        return next((x for x in items if str(x["id"]) == sel[0]), None)

    def _entry(self):
        return self._picked(self.tree, self.data["entries"], "an entry")

    def _patch(self):
        return self._picked(self.ptree, self.data["patches"], "a patch")

    def _selected_changed(self):
        sel = self.tree.selection()
        e = next((e for e in self.data["entries"] if sel and str(e["id"]) == sel[0]), None)
        if e:
            self.value.set(e.get("value") or ("" if e.get("live") is None else str(e["live"])))
        self._lock_label()


    def _draw_switches(self, switches):
        pal = self.app.pal
        shape = [(sw["id"], sw["name"], sw.get("note", "")) for sw in switches]
        if shape != self.switch_shape:
            for child in self.switch_box.winfo_children():
                child.destroy()
            self.switch_rows, self.pickers = {}, {}
            for r, sw in enumerate(switches):
                var = tk.BooleanVar(value=bool(sw.get("on")))
                check = ttk.Checkbutton(self.switch_box, text=sw["name"], variable=var,
                                        command=lambda i=sw["id"]: self.toggle_switch(i))
                check.grid(row=r, column=0, sticky="w", padx=(0, 14), pady=1)
                if sw.get("pick"):
                    combo = ttk.Combobox(self.switch_box, state="readonly", width=34, values=["loading..."],
                                         postcommand=lambda i=sw["id"]: self._choices_opened(i))
                    combo.grid(row=r, column=1, sticky="w", padx=(0, 10))
                    combo.bind("<<ComboboxSelected>>", lambda e, i=sw["id"]: self.picked(i))
                    combo.set(sw.get("picked_name", "").rsplit("\\", 1)[-1] or "choose...")
                    self.pickers[sw["id"]] = {"combo": combo, "choices": [], "fetched": None, "loading": False,
                                              "loaded": 0.0, "first": True, "busy": False, "next": None}
                status = ttk.Label(self.switch_box, text="", width=30)
                status.grid(row=r, column=2, sticky="w")
                ttk.Label(self.switch_box, text=summary(sw.get("note")), foreground=pal["dim"]).grid(
                    row=r, column=3, sticky="w")
                self.switch_rows[sw["id"]] = (var, check, status)
            for item_id in self.pickers:
                self.load_choices(item_id)
            if not switches:
                ttk.Label(self.switch_box, foreground=pal["dim"], text=(
                    "no switches for this game yet. pick a value below and hit make a switch")).grid(
                    row=0, column=0, sticky="w")
            self.switch_shape = shape
        for sw in switches:
            var, _, status = self.switch_rows[sw["id"]]
            if sw["id"] in self.switch_pending or self.pickers.get(sw["id"], {}).get("busy"):
                continue
            on = bool(sw.get("on"))
            var.set(on)
            text = sw.get("status", "") if on else ""
            colour = pal["bad"] if text.startswith("problem") else pal["dim"] if text.startswith("waiting") else pal["ok"]
            status.configure(text=text, foreground=colour)


    def load_choices(self, item_id):
        picker = self.pickers.get(item_id)
        if not picker or picker["loading"] or not self.app.s.running:
            return
        picker["loading"] = True
        mods = self.app.s.mods

        def done(choices):
            picker["loading"] = False
            picker["fetched"] = choices
            self._show_choices(item_id, picker)
            self.app.s.remember_game_folders()

        def fail(_):
            picker["loading"] = False

        self.app.tasks.run(lambda: mods.pick_choices(item_id), done, fail)

    def _show_choices(self, item_id, picker):
        if self.pickers.get(item_id) is not picker or picker["fetched"] is None:
            return
        combo = picker["combo"]
        if self._list_open(combo):
            self.after(200, lambda: self._show_choices(item_id, picker))
            return
        choices, picker["fetched"] = picker["fetched"], None
        picker["choices"], picker["loaded"] = choices, time.monotonic()
        combo.configure(values=[c["name"] for c in choices] or ["nothing to choose here"])
        if picker["first"]:
            picker["first"] = False
            sw = next((x for x in self.data.get("switches", []) if x["id"] == item_id), {})
            current = next((c["name"] for c in choices if c["path"] and c["path"] == sw.get("picked_name")), None)
            if current:
                combo.set(current)

    @staticmethod
    def _list_open(combo):
        popdown = "%s.popdown" % combo
        try:
            return bool(int(combo.tk.call("winfo", "exists", popdown))) and \
                bool(int(combo.tk.call("winfo", "ismapped", popdown)))
        except tk.TclError:
            return False

    def _choices_opened(self, item_id):
        picker = self.pickers.get(item_id)
        if picker and time.monotonic() - picker["loaded"] > 3.0:
            self.load_choices(item_id)

    def picked(self, item_id):
        picker = self.pickers.get(item_id)
        if not picker:
            return
        text = picker["combo"].get()
        choice = next((c for c in picker["choices"] if c["name"] == text), None)
        if choice is None:
            return
        if picker["busy"]:
            picker["next"] = choice
            return
        self._send_pick(item_id, picker, choice)

    def _send_pick(self, item_id, picker, choice):
        picker["busy"], picker["next"] = True, None
        self.switch_rows[item_id][2].configure(text="picking %s..." % choice["name"],
                                               foreground=self.app.pal["dim"])
        mods = self.app.s.mods

        def finished():
            picker["busy"] = False
            if self.pickers.get(item_id) is not picker:
                return
            self.switch_rows[item_id][2].configure(text="")
            if picker["next"]:
                self._send_pick(item_id, picker, picker["next"])
            else:
                self.refresh()

        def done(_):
            self.app.say("Picked %s" % choice["name"])
            finished()

        def bad(err):
            self.app.fail(err)
            finished()

        self.app.act("Pick %s" % choice["name"], lambda: mods.set_pick(item_id, choice["datum"]), done, bad,
                     result=lambda _: "Picked %s" % choice["name"])

    def toggle_switch(self, item_id):
        var, _, status = self.switch_rows[item_id]
        on = bool(var.get())
        sw = next((x for x in self.data.get("switches", []) if x["id"] == item_id), {"name": "the switch"})
        if not self.app.s.running:
            var.set(not on)
            return self.app.say("start the game first")
        self.switch_pending[item_id] = on
        status.configure(text="turning on..." if on else "turning off...", foreground=self.app.pal["dim"])
        mods = self.app.s.mods

        def done(_):
            self.switch_pending.pop(item_id, None)
            self.app.say("%s is %s" % (sw["name"], "on" if on else "off"))
            self.refresh()

        def bad(err):
            self.switch_pending.pop(item_id, None)
            var.set(not on)
            status.configure(text="")
            self.app.fail(err)
            self.refresh()

        self.app.act("Turn %s %s" % ("on" if on else "off", sw["name"]), lambda: mods.set_switch(item_id, on),
                     done, bad, result=lambda status: status or "off")

    def make_switch(self):
        e = self._entry()
        if not e:
            return
        restore = "Put the old value back"
        leave = "Leave it"
        r = Form(self, "Make a switch", [
            ("name", "Name", "text", e["name"], None),
            ("on", "Value while on", "mono", self.value.get().strip() or e.get("value", ""), None),
            ("off", "When off", "choice", restore, [restore, leave, "Write the value below"]),
            ("off_value", "Value when off", "mono", "", None),
        ], note=("keeps %s at this value while on. |0x40 or &~0x08 only flips those bits. hold stops it going "
                 "down (good for timers)" % e["address"]),
            ok="Make").wait()
        if not r or not r["on"]:
            return
        off = "restore" if r["off"] == restore else "" if r["off"] == leave else r["off_value"]
        mods = self.app.s.mods
        self._do(lambda: mods.add_switch(r["name"], [{"address": e["address"], "type": e["type"], "on": r["on"],
                                                      "off": off}], e.get("note", "")),
                 "made switch %s" % (r["name"] or e["name"]))

    def delete_switch(self):
        switches = self.data.get("switches", [])
        if not switches:
            return self.app.say("this game has no switches")
        r = Form(self, "Delete a switch", [("name", "Switch", "choice", switches[0]["name"],
                                            [sw["name"] for sw in switches])],
                 note="if its on it gets turned off first and puts things back", ok="Delete").wait()
        sw = next((x for x in switches if r and x["name"] == r["name"]), None)
        if sw:
            mods = self.app.s.mods
            self._do(lambda: mods.delete("switch", sw["id"]), "deleted switch %s" % sw["name"])

    def _do(self, fn, message, title=None):
        def done(_):
            self.app.say(message)
            self.refresh()
        if title:
            self.app.act(title, fn, done, result=lambda _: message)
        else:
            self.app.tasks.run(fn, done)


    def _form(self, title, e, ok):
        return Form(self, title, [
            ("name", "Name", "text", e.get("name", ""), None),
            ("address", "Address", "mono", e.get("address", ""), None),
            ("type", "Type", "choice", values.LABELS[e.get("type", "float")], TYPE_LABELS),
            ("value", "Value to set", "mono", e.get("value", ""), None),
            ("note", "Note", "text", e.get("note", ""), None),
        ], note=ADDRESS_NOTE, ok=ok).wait()

    def add(self, address="", vtype="float"):
        if not self.app.s.running:
            return self.app.say("start the game first")
        r = self._form("Add a value", {"address": address, "type": vtype}, "Add")
        if not r or not r["address"]:
            return
        mods = self.app.s.mods
        self._do(lambda: mods.add_entry(r["name"], r["address"], type_id(r["type"]), r["value"], r["note"]),
                 "Added %s" % (r["name"] or r["address"]))

    def edit(self):
        e = self._entry()
        r = self._form("Edit value", e, "Save") if e else None
        if not r:
            return
        mods = self.app.s.mods
        self._do(lambda: mods.update_entry(e["id"], name=r["name"], address=r["address"], type=type_id(r["type"]),
                                           value=r["value"], note=r["note"]), "Changed %s" % r["name"])

    def delete(self):
        e = self._entry()
        if e and messagebox.askyesno("Delete value", "delete '%s'?" % e["name"], parent=self):
            mods = self.app.s.mods
            self._do(lambda: mods.delete("entry", e["id"]), "Deleted %s" % e["name"])

    def view(self):
        e = self._entry()
        if e and "resolved" in e:
            self.app.goto(e["resolved"])

    def set_now(self):
        e = self._entry()
        if e:
            value, mods = self.value.get().strip(), self.app.s.mods
            self._do(lambda: mods.set_value(e["id"], value), "Set %s to %s" % (e["name"], value),
                     "Set %s to %s" % (e["name"], value))

    def _lock_label(self):
        sel = self.tree.selection()
        e = next((e for e in self.data["entries"] if sel and str(e["id"]) == sel[0]), None)
        self.lock_btn.configure(text="Unlock" if e and e.get("frozen") else "Lock")

    def _clicked_lock(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            row = self.tree.identify_row(event.y)
            if row:
                self.tree.selection_set(row)
                self.toggle_lock()

    def toggle_lock(self):
        e = self._entry()
        if not e:
            return
        mods = self.app.s.mods
        if e.get("frozen"):
            self._do(lambda: mods.update_entry(e["id"], frozen=False), "%s unlocked" % e["name"],
                     "Unlock %s" % e["name"])
            return
        value = self.value.get().strip() or e.get("value", "")

        def work():
            mods.set_value(e["id"], value)
            mods.update_entry(e["id"], frozen=True)

        self._do(work, "%s locked at %s" % (e["name"], value), "Lock %s at %s" % (e["name"], value))


    def call_now(self):
        self.call(self.call_addr.get(), self.call_args.get().split(), self.call_string.get(),
                  "", self.call_scratch.get(), convention=self.call_conv.get(), from_row=True)

    def call(self, address, args, string="", expect="", scratch="", name=None, call_id=None, from_row=False,
             convention="cdecl"):
        s = self.app.s
        if self.calling:
            return self.app.say("a call is still running")
        if not s.running:
            return self.app.fail("no og xbox game running")
        if self.app.busy:
            return self.app.say("wait for '%s' to finish first, it might have the game paused" % self.app.busy_label)
        try:
            args = call_args(args)
            if any(split_arg(a)[1].startswith("=") for a in args):
                args = s.mods.resolve_call_args(args)
            slots = caller.parse_arguments(call_items(args))
            plan = caller.plan_call(address, slots, convention)
            text = string if set(caller.TEXTS) & {v[1] if isinstance(v, tuple) else v for v in slots} else None
            expected = caller.number(expect) if str(expect or "").strip() else None
            scratch = caller.number(scratch) if str(scratch or "").strip() else None
        except ValueError as e:
            return self.app.fail(e)
        except Exception as e:
            return self.app.fail("call not made: %s" % describe(e))
        mods = s.mods
        title_id = int(mods.title_id, 16)
        shown = caller.show_call(plan["address"], slots, text, convention)
        self.calling = True
        self.call_btn.configure(state="disabled")
        self.call_result.configure(text="Calling %s..." % shown, foreground="")
        self.call_detail.configure(text="the game pauses for a sec while the call gets set up. the log shows each step")

        def work():
            return s.caller.call(plan["address"], *slots, convention=convention, expected=expected, string=text,
                                 scratch=scratch, title_id=title_id)

        def finish():
            self.calling = False
            self.call_btn.configure(state="normal")

        def done(eax):
            finish()
            v = caller.eax_views(eax)
            got = s.caller.last_result or {}
            extra = "   EDX 0x%08X" % got["edx"] if got.get("edx") is not None else ""
            if got.get("float") is not None:
                extra += "   float result %r" % got["float"]
            if got.get("xmm0") is not None:
                extra += "   XMM0 %r" % got["xmm0"]
            self.call_result.configure(text="%s = %d   %s   signed %d   as float %g%s" % (
                shown, v["dec"], v["hex"], v["signed"], v["float"], extra), foreground="")
            self.call_detail.configure(text=self._report_text(s))
            if from_row:
                self.last_call = (plan["address"], tuple(args), text or "", eax, convention)
            if call_id is not None:
                mods.set_call_result(call_id, "EAX = %d (0x%08X)%s" % (
                    eax, eax, "" if got.get("float") is None else ", float %r" % got["float"]))
                self.refresh()

        def bad(err):
            finish()
            report = self._report(s)
            self.call_result.configure(text="%s failed: %s" % (shown, describe(err)), foreground=self.app.pal["bad"])
            hint = (report or {}).get("hint", "")
            self.call_detail.configure(text=(hint + "  " if hint else "") + (
                "Report: %s" % os.path.basename(str(s.caller.last_report)) if s.caller and s.caller.last_report else ""))
            if call_id is not None:
                mods.set_call_result(call_id, "failed: %s" % describe(err))
                self.refresh()
            self.app.fail(err)

        self.app.act("Call %s%s" % (shown, "  (%s)" % name if name else ""), work, done, bad,
                     result=lambda eax: "EAX = %d (0x%08X)" % (eax, eax))

    @staticmethod
    def _report(s):
        try:
            with open(s.caller.last_report) as f:
                return json.load(f)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _report_text(self, s):
        report = self._report(s) or {}
        parts = []
        if report.get("timing"):
            parts.append("ran %s" % report["timing"])
        after = report.get("after") or {}
        cleanup = report.get("cleanup") or {}
        if after:
            parts.append("completed %s" % ("exactly once" if after.get("hits") == 1 else "%s times" % after.get("hits")))
        if cleanup:
            parts.append("%d gate blocks put back" % cleanup.get("blocks_restored", 0))
            parts.append("request storage %s" % ("cleared" if cleanup.get("request_zero") else "not cleared"))
        if report.get("ticks_after_cleanup"):
            parts.append("guest kernel still running")
        if s.caller and s.caller.last_report:
            parts.append("report %s" % os.path.basename(str(s.caller.last_report)))
        return "  ·  ".join(parts)

    def _call(self):
        return self._picked(self.ctree, self.data.get("calls", []), "a saved call")

    def run_call(self):
        c = self._call()
        if c:
            self.call(c["address"], c["args"], c.get("string", ""), c.get("expect", ""), name=c["name"], call_id=c["id"],
                      convention=c.get("convention", "cdecl"))

    def load_call(self):
        c = self._call()
        if c:
            self.call_addr.set(c["address"])
            self.call_args.set(" ".join(c["args"]))
            self.call_string.set(c.get("string", ""))
            self.call_conv.set(c.get("convention", "cdecl"))

    def save_call(self):
        if not self.app.s.running:
            return self.app.say("start the game first, calls get saved to the running games table")
        try:
            address = "0x%08X" % caller.address(self.call_addr.get())
            args = call_args(self.call_args.get().split())
            convention = self.call_conv.get()
            caller.plan_call(address, caller.parse_arguments(call_items(args)), convention)
        except ValueError as e:
            return self.app.fail(e)
        string = self.call_string.get()
        last = self.last_call
        same = last and last[0] == int(address, 16) and last[1] == tuple(args) and last[4] == convention and (
            not any(a.endswith(caller.TEXTS) for a in args) or last[2] == string)
        checks = [NO_CHECK] + (["yes, eax must be %d (what it just returned)" % last[3]] if same else [])
        r = Form(self, "Save call", [
            ("name", "Name", "text", "", None),
            ("check", "Check result", "choice", NO_CHECK, checks),
            ("note", "Note", "text", "", None),
        ], note=("stays in %ss table till OG360 closes (export keeps it). run it again with one click. a check "
                 "fails the call if eax is different, good for math, not for stuff that changes. %s"
                 % (self.app.s.mods.title, "" if same else "(call it first to check its result)")),
            ok="Save").wait()
        if not r:
            return
        expect = str(last[3]) if same and r["check"] != NO_CHECK else ""
        mods = self.app.s.mods
        self._do(lambda: mods.add_call(r["name"], address, args, string, expect, r["note"], convention),
                 "added call %s" % (r["name"] or address))

    def _call_form(self, title, c, ok):
        return Form(self, title, [
            ("name", "Name", "text", c.get("name", ""), None),
            ("address", "Address (hex)", "mono", c.get("address", "0x"), None),
            ("convention", "Convention", "choice", c.get("convention", "cdecl"), CONVENTIONS),
            ("args", "Arguments", "mono", " ".join(c.get("args", [])), None),
            ("string", "Text for @string / @wstring", "text", c.get("string", ""), None),
            ("expect", "Check: eax must be", "mono", c.get("expect", ""), None),
            ("note", "Note", "text", c.get("note", ""), None),
        ], note=CALL_NOTE + " leave the check empty unless the result is always the same", ok=ok).wait()

    def add_call(self):
        if not self.app.s.running:
            return self.app.say("start the game first")
        r = self._call_form("Add a call", {}, "Add")
        if not r or not r["address"].strip().lower().lstrip("0x"):
            return
        mods = self.app.s.mods
        self._do(lambda: mods.add_call(r["name"], r["address"], r["args"].split(), r["string"], r["expect"], r["note"],
                                       r["convention"]),
                 "added call %s" % (r["name"] or r["address"]))

    def edit_call(self):
        c = self._call()
        r = self._call_form("Edit call", c, "Save") if c else None
        if not r:
            return
        mods = self.app.s.mods
        self._do(lambda: mods.update_call(c["id"], name=r["name"], address=r["address"], args=r["args"].split(),
                                          convention=r["convention"], string=r["string"], expect=r["expect"],
                                          note=r["note"]),
                 "Changed %s" % (r["name"] or r["address"]))

    def delete_call(self):
        c = self._call()
        if c and messagebox.askyesno("Delete call", "delete '%s'?" % c["name"], parent=self):
            mods = self.app.s.mods
            self._do(lambda: mods.delete("call", c["id"]), "Deleted %s" % c["name"])


    def add_patch(self):
        if not self.app.s.running:
            return self.app.say("start the game first")
        r = Form(self, "Add a patch", [
            ("name", "Name", "text", "", None),
            ("address", "Address", "mono", "", None),
            ("on", "New bytes (hex)", "mono", "", None),
            ("off", "Original bytes (optional)", "mono", "", None),
        ], note="leave original bytes empty and they get read from the game the first time you turn it on",
            ok="Add").wait()
        if not r or not r["address"] or not r["on"]:
            return
        mods = self.app.s.mods
        self._do(lambda: mods.add_patch(r["name"], r["address"], r["on"], r["off"]), "added patch %s" % r["name"])

    def toggle(self, enable):
        p = self._patch()
        if p:
            mods = self.app.s.mods
            self._do(lambda: mods.toggle_patch(p["id"], enable), "%s turned %s" % (p["name"], "on" if enable else "off"),
                     "Turn %s patch %s" % ("on" if enable else "off", p["name"]))

    def delete_patch(self):
        p = self._patch()
        if p and messagebox.askyesno("Delete patch", "delete patch '%s'? this doesnt undo it in the game" % p["name"],
                                     parent=self):
            mods = self.app.s.mods
            self._do(lambda: mods.delete("patch", p["id"]), "deleted patch %s" % p["name"])


    def export(self):
        if not self.app.s.running:
            return self.app.say("start the game first")
        mods = self.app.s.mods
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                            initialfile="%s %s mods.json" % (mods.title_id, mods.title))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(mods.export(), f, indent=2)
            self.app.say("exported to %s" % path)

    def import_(self):
        if not self.app.s.running:
            return self.app.say("start the game first")
        path = filedialog.askopenfilename(parent=self, filetypes=[("Mod tables", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return self.app.fail(e)
        mods = self.app.s.mods
        if data.get("title_id") and data["title_id"] != mods.title_id:
            if not messagebox.askyesno("Different game", "that table is for %s (%s), not this game. import it anyway?" % (data.get("title", "another game"), data["title_id"]),
                                       parent=self):
                return
        self._do(lambda: mods.import_data(data), "Imported %s" % path)

    def names(self):
        if not self.app.s.running:
            return self.app.say("start the game first")
        mods = self.app.s.mods
        win = tk.Toplevel(self)
        win.title("Names")
        win.transient(self.winfo_toplevel())
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, wraplength=460, justify="left", text=(
            "one per line: a name then its address (hex or a formula like players + 2*6140 or [4E0300] + "
            "44). names work anywhere an address does")).pack(anchor="w")
        text = tk.Text(frame, width=60, height=14, font=MONO)
        text.pack(fill="both", expand=True, pady=8)
        text.insert("1.0", mods.symbols_text())

        def save():
            try:
                mods.set_symbols(text.get("1.0", "end"))
            except ValueError as e:
                return self.app.fail("couldnt read that list: %s" % e)
            win.destroy()
            self.app.say("Using %d names" % len(mods.symbols))

        row = ttk.Frame(frame)
        row.pack(anchor="e")
        ttk.Button(row, text="Cancel", command=win.destroy).pack(side="left", padx=4)
        ttk.Button(row, text="Use", command=save).pack(side="left")
