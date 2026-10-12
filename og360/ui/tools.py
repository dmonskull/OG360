import os
import tkinter as tk
from tkinter import ttk

from .. import paths
from .common import HEADING, MONO, AreaPicker, ScrollFrame, hex8, make_tree, open_folder


def raw_command(client, text):
    from py_xbdm.protocol import parse_response_line
    with client._lock:
        client._ensure_connected()
        client.conn.send(text.encode("ascii", "replace") + b"\r\n")
        resp = parse_response_line(client.conn.recv_line(), True)
        lines = []
        if resp.code == 202:
            while True:
                line = client.conn.recv_line().decode("ascii", "replace").strip()
                if line == ".":
                    break
                lines.append(line)
        elif resp.code in (203, 204):
            client.reconnect()
            lines = ["(binary reply not shown, use the OG360 tool for it)"]
    client._note(text, resp.code, resp.message, lines if resp.code == 202 else None)
    return resp.code, resp.message.strip(), lines


class ToolsTab(ScrollFrame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        pal = app.pal
        left = ttk.Frame(self.inner)
        left.pack(side="left", fill="y", anchor="n")
        right = ttk.Frame(self.inner)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))

        box = ttk.LabelFrame(left, text="Console", padding=10)
        box.pack(fill="x")
        self.info = self._grid(box, (("console", "Console"), ("game", "Running"), ("title_id", "Title ID"),
                                     ("emulator", "Emulator"), ("ram", "Game RAM on the 360"), ("image", "XBE image")))
        self.pause_reads = tk.BooleanVar(value=app.s.pause_big_reads)
        ttk.Checkbutton(box, text="Pause the game for big reads (10x faster)",
                        variable=self.pause_reads, command=self._pause_reads_changed).pack(anchor="w", pady=(8, 0))

        box = ttk.LabelFrame(left, text="Games tab", padding=10)
        box.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Search in").pack(side="left")
        self.games_folder = tk.StringVar(value=app.s.games_folder)
        e = ttk.Entry(row, textvariable=self.games_folder, width=26, font=MONO)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: self._games_folder_changed())
        ttk.Button(row, text="Save", command=self._games_folder_changed).pack(side="left")
        ttk.Label(box, foreground=pal["dim"], wraplength=380, justify="left", text=(
            "a folder on the xbox like Hdd:\\Games. leave it empty to search the whole hdd")).pack(anchor="w", pady=(6, 0))

        box = ttk.LabelFrame(left, text="Address converter", padding=10)
        box.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.conv = tk.StringVar()
        e = ttk.Entry(row, textvariable=self.conv, width=26, font=MONO)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.convert())
        ttk.Button(row, text="Convert", command=self.convert).pack(side="left", padx=6)
        self.conv_out = self._grid(box, (("addr", "Game address"), ("host", "360 address"),
                                         ("phys", "Xbox physical"), ("section", "Section")))
        ttk.Label(box, foreground=pal["dim"], wraplength=380, justify="left", text=(
            "the 360 address works in other xbdm tools. it can move when the game allocates memory so "
            "convert again after a level loads")).pack(anchor="w", pady=(6, 0))

        box = ttk.LabelFrame(left, text="Save memory to a file", padding=10)
        box.pack(fill="x", pady=(12, 0))
        self.area = AreaPicker(box, width=40)
        self.area.pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(anchor="w", pady=(6, 0))
        ttk.Button(row, text="Save", command=self.dump).pack(side="left")
        ttk.Button(row, text="Show dumps folder", command=lambda: open_folder(paths.path("dumps"))).pack(side="left", padx=6)
        self.dump_out = ttk.Label(box, foreground=pal["dim"], wraplength=380, justify="left", text="")
        self.dump_out.pack(anchor="w", pady=(6, 0))

        box = ttk.LabelFrame(right, text="Send an XBDM command", padding=10)
        box.pack(fill="x", pady=(0, 12))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.raw = tk.StringVar(value="dmversion")
        e = ttk.Entry(row, textvariable=self.raw, width=30, font=MONO)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.send_raw())
        ttk.Button(row, text="Send", command=self.send_raw).pack(side="left", padx=6)
        self.raw_out = tk.Text(box, width=48, height=4, font=MONO, wrap="none", borderwidth=0, highlightthickness=1)
        self.raw_out.pack(fill="x", pady=(6, 0))
        self.raw_out.configure(state="disabled")
        ttk.Label(box, foreground=pal["dim"], wraplength=380, justify="left", text=(
            "goes through py-xbdm on the main connection, its in the log too")).pack(anchor="w", pady=(4, 0))

        ttk.Label(right, text="Game xbe sections", font=HEADING).pack(anchor="w")
        frame, self.sections = make_tree(right, (
            ("name", "Section", 110, "w"), ("start", "Start", 100, "w"), ("end", "End", 100, "w"),
            ("size", "Size", 110, "e"), ("kind", "Kind", 120, "w")), height=6)
        frame.pack(fill="x", pady=(4, 0))
        self.sections.bind("<Double-1>", lambda e: self._view(self.sections))
        ttk.Label(right, text="Mapped memory", font=HEADING).pack(anchor="w", pady=(14, 0))
        frame, self.mapped = make_tree(right, (
            ("start", "Start", 100, "w"), ("end", "End", 100, "w"), ("size", "Size", 110, "e"),
            ("what", "What", 220, "w")), height=5)
        frame.pack(fill="both", expand=True, pady=(4, 0))
        self.mapped.bind("<Double-1>", lambda e: self._view(self.mapped))
        ttk.Label(right, foreground=pal["dim"], text="double click a row to open it in memory").pack(anchor="w", pady=(6, 0))

    def _grid(self, parent, rows):
        out = {}
        grid = ttk.Frame(parent)
        grid.pack(anchor="w", pady=(6, 0))
        for i, (key, label) in enumerate(rows):
            ttk.Label(grid, text=label, foreground=self.app.pal["dim"]).grid(row=i, column=0, sticky="w", pady=1)
            out[key] = ttk.Label(grid, text="-", font=MONO)
            out[key].grid(row=i, column=1, sticky="w", padx=(12, 0))
        return out

    def _games_folder_changed(self):
        self.app.s.games_folder = self.games_folder.get()
        self.app.games.show()
        self.app.say("games tab searches %s now. hit search there to list the games"
                     % (self.app.s.games_folder or "the whole hdd"))

    def _pause_reads_changed(self):
        self.app.s.pause_big_reads = self.pause_reads.get()
        if not self.app.s.running:
            return
        for picker in (self.area, self.app.search.area, self.app.changes.area):
            self.app.tasks.run(self.app.s.areas, picker.set_areas)

    def on_game(self):
        s = self.app.s
        self.sections.delete(*self.sections.get_children())
        self.mapped.delete(*self.mapped.get_children())
        self.info["console"].configure(text="%s (%s)" % (s.console_name, s.host) if s.connected else "-")
        for key in ("game", "title_id", "emulator", "ram", "image"):
            self.info[key].configure(text="-")
        if s.connected and s.kind == "x360":
            self.info["game"].configure(text=s.title_path.rsplit("\\", 1)[-1] if s.title_path else "Xbox 360 title")
        if not s.running:
            return
        g, x = s.guest, s.guest.xbe
        self.info["game"].configure(text=x["title"])
        self.info["title_id"].configure(text=x["title_id"])
        self.info["emulator"].configure(text=g.emulator)
        self.info["ram"].configure(text="%s - %s" % (hex8(g.ram_base), hex8(g.ram_base + 0x04000000 - 1)))
        self.info["image"].configure(text="%s - %s" % (hex8(x["base"]), hex8(x["base"] + x["image_size"])))
        for sec in x["sections"]:
            kind = "data (writable)" if sec["writable"] and not sec["name"].startswith(".text") else "code / read-only"
            self.sections.insert("", "end", iid=str(sec["va"]), values=(
                sec["name"], hex8(sec["va"]), hex8(sec["va"] + sec["size"]), format(sec["size"], ","), kind))
        self.app.tasks.run(s.areas, self.area.set_areas)
        self.app.tasks.run(lambda: g.mapped(0x10000, 0x80000000), self._show_mapped)

    def _show_mapped(self, ranges):
        if not self.app.s.running:
            return
        base = self.app.s.guest.xbe["base"]
        self.mapped.delete(*self.mapped.get_children())
        for a, b in ranges:
            self.mapped.insert("", "end", iid=str(a), values=(
                hex8(a), hex8(b), format(b - a, ","), "XBE image" if a == base else "heap"))

    def _view(self, tree):
        sel = tree.selection()
        if sel:
            self.app.goto(int(sel[0]))

    def convert(self):
        expr = self.conv.get().strip()
        if not expr:
            return

        def done(d):
            self.conv_out["addr"].configure(text=hex8(d["addr"]))
            self.conv_out["host"].configure(text=hex8(d["host"]) if d["host"] is not None else "not mapped right now")
            self.conv_out["phys"].configure(text=hex8(d["phys"]) if d["phys"] is not None else "-")
            self.conv_out["section"].configure(text=d["section"] or "-")

        self.app.tasks.run(lambda: self.app.s.describe(self.app.s.need_game().resolve(expr)), done)

    def dump(self):
        area, lo, hi = self.area.get()

        def done(files):
            text = "Saved %d file%s in %s" % (len(files), "" if len(files) == 1 else "s", os.path.dirname(files[0]))
            self.dump_out.configure(text=text + "\n" + "\n".join(os.path.basename(p) for p in files[:12]))
            self.app.say(text)

        self.app.job("Save memory to a file",
                     lambda progress: self.app.s.dump(self.app.s.regions(area, lo, hi), progress), done,
                     log_result=lambda files: "%d file%s" % (len(files), "" if len(files) == 1 else "s"))

    def send_raw(self):
        text = self.raw.get().strip()
        if not text:
            return
        if not self.app.s.connected:
            return self.app.say("not connected to an xbox")

        def done(result):
            code, message, lines = result
            self.raw_out.configure(state="normal")
            self.raw_out.delete("1.0", "end")
            self.raw_out.insert("1.0", "%d- %s\n%s" % (code, message, "\n".join(lines)))
            self.raw_out.configure(state="disabled")

        self.app.act("XBDM: %s" % text, lambda: self.app.s.console.run(lambda c: raw_command(c, text)), done,
                     result=lambda r: "%d- %s%s" % (r[0], r[1], " (%d lines)" % len(r[2]) if r[2] else ""))
