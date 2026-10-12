import os
import queue
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

MONO = "TkFixedFont"
HEADING = "OG360Heading"
TITLE = "OG360Title"
_fonts = []


def setup_fonts(root):
    wanted = ("Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono", "Courier New")
    have = set(tkfont.families(root))
    family = next((f for f in wanted if f in have), None)
    mono = tkfont.nametofont("TkFixedFont")
    if family:
        mono.configure(family=family)
    base = tkfont.nametofont("TkDefaultFont").actual()
    if sys.platform == "darwin":
        scaling = float(root.tk.call("tk", "scaling"))
        px = -base["size"] if base["size"] < 0 else round(base["size"] * scaling)
        mono.configure(size=-12)
        sizes = (-(px + 1), -(px + 3))
    else:
        mono.configure(size=10)
        sizes = (abs(base["size"]) + 1, abs(base["size"]) + 3)
    for name, size in zip((HEADING, TITLE), sizes):
        _fonts.append(tkfont.Font(root=root, name=name, family=base["family"], size=size, weight="bold"))


def hex8(n):
    return "%08X" % (n & 0xFFFFFFFF)


def size_text(n):
    if n < 1024:
        return "%d byte%s" % (n, "" if n == 1 else "s")
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024)
    return "%.1f MB" % (n / 2 ** 20)


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def palette(root):
    dark = False
    if sys.platform == "darwin":
        try:
            dark = bool(int(root.tk.call("::tk::unsupported::MacWindowStyle", "isdark", root)))
        except Exception:
            try:
                dark = sum(root.winfo_rgb("systemWindowBackgroundColor")) / (3 * 65535) < 0.5
            except Exception:
                pass
    if dark:
        return {"diff_bg": "#7a3b12", "diff_fg": "#ffd9b8", "flash_bg": "#2c5b34", "sel_bg": "#2f6fd6",
                "sel_fg": "#ffffff", "dim": "#8a93a2", "ok": "#58b368", "warn": "#e0a83e", "bad": "#e5605e",
                "tx": "#7fb2f0", "log_bg": "#1e2127", "log_fg": "#d7dae0"}
    return {"diff_bg": "#ffd9b0", "diff_fg": "#5a2600", "flash_bg": "#c9efcf", "sel_bg": "#2f6fd6",
            "sel_fg": "#ffffff", "dim": "#7a8494", "ok": "#2f8f45", "warn": "#a66a00", "bad": "#c23533",
            "tx": "#1f5fae", "log_bg": "#fbfbfc", "log_fg": "#1d2025"}


class Tasks:
    def __init__(self, root, on_error):
        self.root = root
        self.on_error = on_error
        self.results = queue.Queue()
        self.pool = ThreadPoolExecutor(max_workers=8)
        self._pump()

    def run(self, fn, done=None, fail=None):
        def work():
            try:
                self.results.put((done, fn()))
            except Exception as e:
                self.results.put((fail or self.on_error, e))
        self.pool.submit(work)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _pump(self):
        try:
            while True:
                callback, value = self.results.get_nowait()
                if callback:
                    callback(value)
        except queue.Empty:
            pass
        self.root.after(30, self._pump)


class Form(tk.Toplevel):
    def __init__(self, parent, title, fields, note="", ok="Save"):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.result = None
        self.vars = {}
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        first = None
        for row, (key, label, kind, initial, choices) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
            var = tk.StringVar(value=initial or "")
            self.vars[key] = var
            if kind == "choice":
                w = ttk.Combobox(frame, textvariable=var, values=choices, state="readonly", width=30)
            elif kind == "mono":
                w = ttk.Entry(frame, textvariable=var, width=38, font=MONO)
            else:
                w = ttk.Entry(frame, textvariable=var, width=38)
            w.grid(row=row, column=1, sticky="ew", pady=4)
            first = first or w
        if note:
            ttk.Label(frame, text=note, wraplength=420, foreground="gray").grid(
                row=len(fields), column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text=ok, command=self._ok).pack(side="left")
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        if first:
            first.focus_set()

    def _ok(self):
        self.result = {k: v.get().strip() for k, v in self.vars.items()}
        self.destroy()

    def wait(self):
        self.grab_set()
        self.wait_window()
        return self.result


def make_tree(parent, columns, height=12, mono=True):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height,
                        style="Mono.Treeview" if mono else "Treeview")
    for cid, heading, width, anchor in columns:
        tree.heading(cid, text=heading, anchor=anchor)
        tree.column(cid, width=width, anchor=anchor, stretch=cid == columns[-1][0])
    bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")
    return frame, tree


class AreaPicker(ttk.Frame):
    def __init__(self, parent, width=52):
        super().__init__(parent)
        self.ids = ["data"]
        self.box = ttk.Combobox(self, state="readonly", width=width)
        self.box.pack(side="left")
        self.box.bind("<<ComboboxSelected>>", lambda e: self._toggle())
        self.custom = ttk.Frame(self)
        self.lo = tk.StringVar()
        self.hi = tk.StringVar()
        ttk.Label(self.custom, text="from").pack(side="left", padx=(8, 4))
        ttk.Entry(self.custom, textvariable=self.lo, width=11, font=MONO).pack(side="left")
        ttk.Label(self.custom, text="to").pack(side="left", padx=4)
        ttk.Entry(self.custom, textvariable=self.hi, width=11, font=MONO).pack(side="left")

    def set_areas(self, areas):
        keep = self.area
        self.ids = [a for a, _ in areas]
        self.box["values"] = [label for _, label in areas]
        self.box.current(self.ids.index(keep) if keep in self.ids else 0)
        self._toggle()

    def select(self, area):
        self.box.current(self.ids.index(area))
        self._toggle()

    @property
    def area(self):
        i = self.box.current()
        return self.ids[i] if 0 <= i < len(self.ids) else "data"

    def _toggle(self):
        if self.area == "custom":
            self.custom.pack(side="left")
        else:
            self.custom.pack_forget()

    def get(self):
        return self.area, self.lo.get().strip(), self.hi.get().strip()


class ScrollFrame(ttk.Frame):
    _routing = False

    def __init__(self, parent, padding=10):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        background = ttk.Style(self).lookup("TFrame", "background")
        if background:
            self.canvas.configure(background=background)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = ttk.Frame(self.canvas, padding=padding)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", self._resized)
        self.canvas._og360_scroll = self
        if not ScrollFrame._routing:
            ScrollFrame._routing = True
            self.bind_all("<MouseWheel>", ScrollFrame._route, add="+")
            self.bind_all("<Button-4>", ScrollFrame._route, add="+")
            self.bind_all("<Button-5>", ScrollFrame._route, add="+")

    def _resized(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)
        self._update()

    def _update(self):
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1
        if needed and not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y", before=self.canvas)
        elif not needed and self.bar.winfo_ismapped():
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    @staticmethod
    def _route(event):
        widget = event.widget
        if isinstance(widget, str):
            return
        while widget is not None:
            if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview, ttk.Combobox)):
                return
            page = getattr(widget, "_og360_scroll", None)
            if isinstance(widget, ScrollFrame):
                page = widget
            if page is not None:
                if not page.bar.winfo_ismapped():
                    return
                if event.num == 4 or getattr(event, "delta", 0) > 0:
                    page.canvas.yview_scroll(-2, "units")
                else:
                    page.canvas.yview_scroll(2, "units")
                return
            widget = getattr(widget, "master", None)
