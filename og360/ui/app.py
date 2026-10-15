import sys
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from .. import __version__, paths, updater
from ..log import ActivityLog, describe
from ..session import Session
from ..xbdm import Cancelled
from .changes import ChangesTab
from .common import HEADING, MONO, TITLE, Tasks, palette, setup_fonts
from .games import GamesTab
from .logview import LogView
from .memory import MemoryTab
from .mods import ModsTab
from .search import SearchTab
from .tools import ToolsTab
from .updatebox import UpdateBox

POLL_MS = 3000


class App:
    def __init__(self, host=None):
        self.root = tk.Tk()
        self.root.title("OG360")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(1180, sw - 120), min(800, sh - 140)
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        self.root.minsize(min(960, w), min(640, h))
        setup_fonts(self.root)
        self.pal = palette(self.root)
        ttk.Style(self.root).configure("Mono.Treeview", font=MONO, rowheight=22)
        self.root.report_callback_exception = self._callback_error

        self.log = ActivityLog(paths.path("logs"))
        self.s = Session(self.log)
        self.s.on_lost = lambda: self.tasks.results.put((lambda _: self._update_status(), None))
        self.tasks = Tasks(self.root, self.fail)
        self.busy = False
        self.busy_label = ""
        self._progress = None
        self._game_key = None
        self._checking = False
        self._typed_host = host
        self.exit_code = 0
        self._update = None
        self._update_box = None
        self._box_shown_for = None

        self._header()
        panes = ttk.PanedWindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=10)
        top = ttk.Frame(panes)
        self.nb = ttk.Notebook(top)
        self.nb.pack(fill="both", expand=True)
        self.games = GamesTab(self.nb, self)
        self.memory = MemoryTab(self.nb, self)
        self.search = SearchTab(self.nb, self)
        self.changes = ChangesTab(self.nb, self)
        self.mods = ModsTab(self.nb, self)
        self.tools = ToolsTab(self.nb, self)
        self.tabs = (self.games, self.memory, self.search, self.changes, self.mods, self.tools)
        for tab, name in zip(self.tabs, ("Games", "Memory", "Search", "Changes", "Mods", "Tools")):
            self.nb.add(tab, text="  %s  " % name)
        self.logview = LogView(panes, self)
        panes.add(top, weight=3)
        panes.add(self.logview, weight=1)
        self.panes = panes
        self.root.after(300, lambda: panes.sashpos(0, max(380, panes.winfo_height() - 140)))

        footer = ttk.Frame(self.root, padding=(12, 6, 12, 8))
        footer.pack(fill="x")
        self.message = ttk.Label(footer, text="", foreground=self.pal["dim"])
        self.message.pack(side="left")
        self.cancel_btn = ttk.Button(footer, text="Cancel", command=self.cancel_job)
        self.pbar = ttk.Progressbar(footer, length=260, mode="determinate", maximum=1000)
        self.plabel = ttk.Label(footer, text="", foreground=self.pal["dim"])

        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.log.info("OG360 %s started. connecting by itself, type an ip next to console to pick one"
                      % __version__, key="start")
        if host:
            self.s.config["host"] = host
            self.host_var.set(host)
        self.root.after(100, self._poll)
        if updater.release_info():
            self.root.after(5000, self.check_updates)


    def _header(self):
        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        row = ttk.Frame(header)
        row.pack(fill="x")
        ttk.Label(row, text="OG360", font=TITLE).pack(side="left", padx=(0, 12))
        self.dot = tk.Canvas(row, width=14, height=14, highlightthickness=0, bd=0)
        self.dot.pack(side="left")
        self.dot_item = self.dot.create_oval(2, 2, 12, 12, fill=self.pal["warn"], outline="")
        self.status = ttk.Label(row, text="connecting to the xbox...", font=HEADING)
        self.status.pack(side="left", padx=8)
        self.update_slot = ttk.Frame(row)
        self.update_slot.pack(side="right")
        ttk.Button(row, text="Screenshot", command=self.screenshot).pack(side="right")
        self.pause_btn = ttk.Button(row, text="Pause game", command=self.toggle_pause)
        self.pause_btn.pack(side="right", padx=6)

        row = ttk.Frame(header)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Console").pack(side="left")
        self.host_var = tk.StringVar(value=self.s.saved_host)
        self.host_box = ttk.Combobox(row, textvariable=self.host_var, width=22, font=MONO)
        self.host_box.pack(side="left", padx=6)
        self.host_box.bind("<Return>", lambda e: self.connect_typed())
        self.host_box.bind("<<ComboboxSelected>>", lambda e: self.connect_typed())
        ttk.Button(row, text="Connect", command=self.connect_typed).pack(side="left")
        ttk.Button(row, text="Find consoles", command=self.find).pack(side="left", padx=6)
        self.conn_note = ttk.Label(row, foreground=self.pal["dim"],
                                   text="connects by itself (saved console first, then the network). type an ip to pick one")
        self.conn_note.pack(side="left", padx=10)

    def connect_typed(self):
        host = self.host_var.get().strip().split(" ")[0]
        if not host:
            return self.say("type the xbox ip next to console")
        self.conn_note.configure(text="connecting to %s..." % host)

        def work():
            with self.log.action("Connect to %s" % host) as act:
                name = self.s.connect(host, "typed")
                act.done(name)
            return name

        def done(name):
            self.conn_note.configure(text="connected. type another ip anytime to switch")
            self.poll_now()

        def bad(err):
            self.conn_note.configure(text=str(err))
            self.fail(err)
            self.poll_now()

        self.tasks.run(work, done, bad)

    def find(self):
        self.conn_note.configure(text="looking for consoles...")

        def done(found):
            if not found:
                self.conn_note.configure(text="no console answered. make sure xbdm is running and type its ip")
                return
            self.host_box["values"] = ["%s  %s" % (ip, name) for ip, name in found]
            if not self.s.connected and len(found) == 1:
                self.host_var.set(found[0][0])
                return self.connect_typed()
            self.conn_note.configure(text="found: %s. pick one and hit connect" % ", ".join(
                "%s (%s)" % (ip, name or "?") for ip, name in found))

        self.tasks.run(self.s.search, done)


    def _poll(self):
        self.poll_now()
        self.root.after(POLL_MS, self._poll)

    def poll_now(self):
        if self._checking:
            return
        self._checking = True

        def done(_):
            self._checking = False
            self._update_status()

        self.tasks.run(self.s.check, done, done)

    def _update_status(self):
        s = self.s
        self.status.configure(text=s.summary())
        colour = (self.pal["ok"] if (s.running or (s.connected and s.kind == "x360")) else
                  self.pal["warn"] if s.connected else self.pal["bad"])
        self.dot.itemconfigure(self.dot_item, fill=colour)
        self.pause_btn.configure(text="Resume game" if s.paused else "Pause game")
        if s.found:
            self.host_box["values"] = ["%s  %s" % (ip, name) for ip, name in s.found]
        if s.connected and self.host_var.get().strip().split(" ")[0] != s.host and not self.host_box.focus_get() == self.host_box:
            self.host_var.set(s.host)
        if not s.connected and s.problem:
            self.conn_note.configure(text=s.problem)
        key = ((s.guest.xbe["title_id"], s.guest.ram_base) if s.running else
               ("x360", s.title_path) if s.connected and s.kind == "x360" else None)
        if key != self._game_key:
            self._game_key = key
            for tab in self.tabs:
                tab.on_game()
            if s.running:
                self.say("found %s running in %s" % (s.guest.xbe["title"], s.guest.emulator))

    def visible(self, tab):
        try:
            return self.nb.nametowidget(self.nb.select()) is tab
        except Exception:
            return False


    def say(self, text):
        self.message.configure(text=text, foreground=self.pal["dim"])

    def fail(self, err):
        text = describe(err) if isinstance(err, BaseException) else str(err)
        self.message.configure(text=text or type(err).__name__, foreground=self.pal["bad"])
        self.root.bell()

    def _callback_error(self, exc, val, tb):
        traceback.print_exception(exc, val, tb, file=sys.stderr)
        self.log.error("something broke: %s" % describe(val), detail=traceback.format_exception(exc, val, tb))
        self.fail(val)

    def act(self, title, fn, done=None, fail=None, result=None):
        def work():
            with self.log.action(title) as a:
                value = fn()
                a.done(result(value) if result else value if isinstance(value, str) else "")
                return value

        self.tasks.run(work, done, fail or self.fail)

    def job(self, label, fn, done=None, log_result=None):
        if self.busy:
            self.say("still working on: %s" % self.busy_label)
            return False
        self.busy, self.busy_label = True, label
        state = {"done": 0, "total": 1, "cancel": False}
        self._progress = state
        self.pbar.configure(value=0)
        self.plabel.configure(text=label + "...")
        self.cancel_btn.pack(side="right")
        self.pbar.pack(side="right", padx=(0, 10))
        self.plabel.pack(side="right", padx=(0, 10))

        def progress(d, t):
            if state["cancel"]:
                raise Cancelled("cancelled")
            state["done"], state["total"] = d, t

        def finish():
            self.busy = False
            self._progress = None
            for w in (self.cancel_btn, self.pbar, self.plabel):
                w.pack_forget()

        def ok(result):
            finish()
            if done:
                done(result)

        def bad(err):
            finish()
            if isinstance(err, Cancelled):
                self.say("%s cancelled." % label)
            else:
                self.fail(err)

        def work():
            with self.log.action(label) as a:
                try:
                    value = fn(progress)
                except Cancelled:
                    a.done("cancelled", level="warn")
                    raise
                a.done(log_result(value) if log_result else "")
                return value

        self.tasks.run(work, ok, bad)
        self._progress_tick()
        return True

    def _progress_tick(self):
        state = self._progress
        if state is None:
            return
        part = state["done"] / max(1, state["total"])
        self.pbar.configure(value=1000 * part)
        held = "  (game paused while reading)" if self.s.console and self.s.console.holding else ""
        self.plabel.configure(text="%s...  %d%%%s" % (self.busy_label, 100 * part, held))
        self.root.after(150, self._progress_tick)

    def cancel_job(self):
        if self._progress is not None:
            self._progress["cancel"] = True


    def goto(self, addr):
        self.nb.select(self.memory)
        self.memory.show(addr)

    def add_mod(self, address, vtype):
        self.nb.select(self.mods)
        self.mods.show_og()
        self.mods.og.add(address, vtype)

    def toggle_pause(self):
        want = not self.s.paused

        def done(_):
            self._update_status()
            self.say("game paused" if want else "game running again")

        self.act("Pause the game" if want else "Resume the game", lambda: self.s.pause(want), done)

    def screenshot(self):
        self.say("Taking a screenshot...")
        self.act("Screenshot", self.s.screenshot, self._show_screenshot, result=lambda p: p)

    def _show_screenshot(self, path):
        from PIL import Image, ImageTk
        image = Image.open(path)
        if image.width > 960:
            image = image.resize((960, int(image.height * 960 / image.width)))
        win = tk.Toplevel(self.root)
        win.title("Screenshot")
        photo = ImageTk.PhotoImage(image, master=win)
        label = ttk.Label(win, image=photo)
        label.image = photo
        label.pack()
        ttk.Label(win, text=path, foreground=self.pal["dim"], padding=6).pack()
        self.say("screenshot saved to %s" % path)


    def check_updates(self):
        def found(rel):
            self._update = rel
            for w in self.update_slot.winfo_children():
                w.destroy()
            if rel:
                btn = tk.Label(self.update_slot, text="UPDATE", font=HEADING, fg="#ffffff", bg=self.pal["ok"],
                               padx=12, pady=3, cursor="hand2")
                btn.pack(side="right", padx=(8, 0))
                btn.bind("<Button-1>", lambda e: self.show_update_box())
                self.log.info("OG360 %s is out, hit update at the top right" % rel["version"], key="update")
                if self.s.update_box and self._box_shown_for != rel["version"]:
                    self._box_shown_for = rel["version"]
                    self.show_update_box()
        self.tasks.run(updater.newer, found, lambda e: None)
        self.root.after(6 * 3600 * 1000, self.check_updates)

    def show_update_box(self):
        rel = self._update
        if not rel or self._update_box is not None:
            return
        self._update_box = "loading"

        def show(entries):
            if self._update_box == "loading" and self._update is rel:
                self._update_box = UpdateBox(self, rel, entries, self.do_update, self._update_box_closed)
            elif self._update_box == "loading":
                self._update_box = None

        self.tasks.run(lambda: updater.update_log(rel), show, lambda e: show([(rel["version"], rel["notes"])]))

    def _update_box_closed(self):
        self._update_box = None

    def do_update(self):
        rel = self._update
        if not rel:
            return
        if self.busy:
            return self.say("still working on: %s" % self.busy_label)

        def done(version):
            messagebox.showinfo("OG360 update", "updated to %s, restarting now" % version, parent=self.root)
            self.exit_code = updater.RESTART
            self.quit()
        self.job("updating to %s" % rel["version"], lambda progress: updater.install(rel, progress), done)

    def quit(self):
        self.s.shutdown()
        self.tasks.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.exit_code
