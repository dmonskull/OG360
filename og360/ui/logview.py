import os
import time
import tkinter as tk
from tkinter import filedialog, ttk

from .. import paths
from .common import HEADING, MONO, open_folder

SHOWN = 300


class LogView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=(10, 6, 10, 0))
        self.app = app
        self.log = app.log
        self._version = -1
        pal = app.pal

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Live log", font=HEADING).pack(side="left")
        self.hint = ttk.Label(bar, foreground=pal["dim"],
                              text="  what OG360 sends the xbox and what it answers. double click a line for details")
        self.hint.pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="right")
        ttk.Button(bar, text="Save...", command=self.save).pack(side="right", padx=6)
        ttk.Button(bar, text="Log folder", command=lambda: open_folder(paths.path("logs"))).pack(side="right")
        self.background = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show background traffic", variable=self.background,
                        command=self._background_changed).pack(side="right", padx=12)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(4, 0))
        self.text = tk.Text(body, height=10, font=MONO, wrap="word", borderwidth=0, highlightthickness=1,
                            padx=8, pady=4, background=pal["log_bg"], foreground=pal["log_fg"],
                            insertwidth=0, cursor="arrow")
        bar_y = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=bar_y.set)
        self.text.pack(side="left", fill="both", expand=True)
        bar_y.pack(side="right", fill="y")
        for tag, colour in (("ok", pal["ok"]), ("warn", pal["warn"]), ("error", pal["bad"]),
                            ("running", pal["warn"]), ("tx", pal["tx"]), ("dim", pal["dim"])):
            self.text.tag_configure(tag, foreground=colour)
        self.text.configure(state="disabled")
        self.text.bind("<Double-1>", self._details)
        self.after(150, self._tick)

    def _background_changed(self):
        self.log.show_background = self.background.get()
        self.log.info("background traffic is %s" % ("shown (polling, locks and checks, repeats merged)"
                                                     if self.log.show_background else "hidden"), key="background")

    def _at_bottom(self):
        return self.text.yview()[1] >= 0.999

    def _tick(self):
        if self.log.version != self._version:
            self._version = self.log.version
            self.render()
        self.after(150, self._tick)

    def render(self):
        follow = self._at_bottom()
        top = self.text.yview()[0]
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        rows = self.log.lines(SHOWN)
        for i, (eid, level, kind, text) in enumerate(rows):
            tags = ("e%d" % eid, level) if level else ("e%d" % eid,)
            t.insert("end", text + ("\n" if i + 1 < len(rows) else ""), tags)
        t.configure(state="disabled")
        if follow:
            t.see("end")
        else:
            t.yview_moveto(top)

    def _details(self, event):
        index = self.text.index("@%d,%d" % (event.x, event.y))
        eid = next((int(tag[1:]) for tag in self.text.tag_names(index) if tag.startswith("e") and tag[1:].isdigit()), None)
        if eid is None:
            return "break"
        text = self.log.details(eid)
        if not text:
            return "break"
        win = tk.Toplevel(self)
        win.title("Log details")
        win.transient(self.winfo_toplevel())
        box = tk.Text(win, width=110, height=min(40, text.count("\n") + 3), font=MONO, wrap="none",
                      padx=8, pady=6)
        bar_y = ttk.Scrollbar(win, orient="vertical", command=box.yview)
        bar_x = ttk.Scrollbar(win, orient="horizontal", command=box.xview)
        box.configure(yscrollcommand=bar_y.set, xscrollcommand=bar_x.set)
        box.grid(row=0, column=0, sticky="nsew")
        bar_y.grid(row=0, column=1, sticky="ns")
        bar_x.grid(row=1, column=0, sticky="ew")
        win.rowconfigure(0, weight=1)
        win.columnconfigure(0, weight=1)
        box.insert("1.0", text)
        box.configure(state="disabled")

        def copy():
            win.clipboard_clear()
            win.clipboard_append(text)

        ttk.Button(win, text="Copy", command=copy).grid(row=2, column=0, sticky="e", padx=8, pady=6)
        return "break"

    def clear(self):
        self.log.clear()

    def save(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".txt",
                                            initialfile="og360-log-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.text())
            self.app.say("saved the log to %s" % os.path.basename(path))
