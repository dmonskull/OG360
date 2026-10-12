from tkinter import ttk

from .mods_og import OGModsTab


class ModsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=(4, 6, 4, 0))
        self.app = app
        self.og = OGModsTab(self, app)
        self.og.pack(fill="both", expand=True)

    def visible_child(self, child):
        try:
            return self.app.visible(self) and child is self.og
        except Exception:
            return False

    def show_og(self):
        pass

    def on_game(self):
        self.og.on_game()
