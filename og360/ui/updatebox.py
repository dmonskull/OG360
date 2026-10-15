import tkinter as tk
from tkinter import ttk

from .. import __version__
from .common import HEADING


class UpdateBox:
    def __init__(self, app, rel, entries, on_update, on_close):
        self.root = app.root
        self.on_close = on_close
        pal = app.pal
        self.outer = tk.Frame(self.root, highlightthickness=1, highlightbackground=pal["dim"],
                              highlightcolor=pal["dim"], bd=0)
        box = ttk.Frame(self.outer, padding=(16, 10, 10, 14))
        box.pack(fill="both", expand=True)

        top = ttk.Frame(box)
        top.pack(fill="x")
        ttk.Label(top, text="OG360 %s is out" % rel["version"], font=HEADING).pack(side="left", pady=(4, 0))
        close = ttk.Label(top, text="✕", font=HEADING, foreground=pal["dim"], cursor="hand2", padding=(6, 0))
        close.pack(side="right", anchor="n")
        close.bind("<Button-1>", lambda e: self.close())
        ttk.Label(box, text="you have %s, heres whats new" % __version__,
                  foreground=pal["dim"]).pack(anchor="w", pady=(2, 8), padx=(0, 6))

        body = ttk.Frame(box)
        body.pack(fill="both", expand=True, padx=(0, 6))
        text = tk.Text(body, wrap="word", width=64, height=14, borderwidth=0, highlightthickness=0,
                       bg=pal["log_bg"], fg=pal["log_fg"], padx=12, pady=10, cursor="arrow")
        scroll = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("version", font=HEADING, spacing3=4)
        for i, (version, notes) in enumerate(entries):
            text.insert("end", ("\n" if i else "") + version + "\n", "version")
            text.insert("end", (notes or "no notes for this one") + "\n")
        text.configure(state="disabled")

        bottom = ttk.Frame(box)
        bottom.pack(fill="x", pady=(12, 0), padx=(0, 6))
        ttk.Button(bottom, text="Update now", command=lambda: (self.close(), on_update())).pack(side="right")
        ttk.Button(bottom, text="Later", command=self.close).pack(side="right", padx=6)
        ttk.Label(bottom, text="you can turn this box off in tools", foreground=pal["dim"]).pack(side="left")

        self._esc = self.root.bind("<Escape>", lambda e: self.close(), add="+")
        self.outer.place(relx=0.5, rely=0.45, anchor="center")
        self.outer.lift()

    def close(self):
        if self.outer is None:
            return
        try:
            self.root.unbind("<Escape>", self._esc)
        except tk.TclError:
            pass
        self.outer.destroy()
        self.outer = None
        self.on_close()
