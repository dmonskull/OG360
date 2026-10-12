from tkinter import messagebox, ttk

from ..x360 import games
from .common import HEADING, make_tree


class GamesTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=(4, 6, 4, 0))
        self.app = app
        self._looked = False
        self._games = {}
        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Label(row, text="OG Xbox games", font=HEADING).pack(side="left")
        ttk.Button(row, text="Boot", command=self.boot).pack(side="right")
        self.fav_btn = ttk.Button(row, text="Favorite", command=self.toggle_favorite)
        self.fav_btn.pack(side="right", padx=6)
        ttk.Button(row, text="Search again", command=self.search).pack(side="right")
        self.where = ttk.Label(self, foreground=app.pal["dim"], text="")
        self.where.pack(anchor="w", pady=(4, 6))
        frame, self.tree = make_tree(self, (("fav", "★", 34, "center"), ("name", "Game", 280, "w"),
                                            ("path", "File", 420, "w")), height=16, mono=False)
        frame.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.boot())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selected())
        ttk.Label(self, foreground=app.pal["dim"], text=(
            "double click a game to boot it. favorites stay at the top. set where to search in tools, its "
            "the whole hdd by default")).pack(anchor="w", pady=(6, 0))
        self.bind("<Map>", lambda e: self._first_look())
        self.show()

    def show(self, keep=None):
        s = self.app.s
        found, favorites = s.games, s.favorite_games
        self.tree.delete(*self.tree.get_children())
        self._games = {}
        for game in found:
            self.tree.insert("", "end", iid=game.path, values=(
                "★" if game.path.lower() in favorites else "", game.name, game.path))
            self._games[game.path] = game
        where = s.games_folder or "%s (the whole hdd)" % games.HDD
        self.where.configure(text=("%d game%s in %s" % (len(found), "" if len(found) == 1 else "s", where)
                                   if found else "no games listed yet. search looks in %s" % where))
        if keep in self._games:
            self.tree.selection_set(keep)
            self.tree.see(keep)
        self._selected()

    def _picked(self):
        sel = self.tree.selection()
        return self._games.get(sel[0]) if sel else None

    def _selected(self):
        game = self._picked()
        on = game is not None and game.path.lower() in self.app.s.favorite_games
        self.fav_btn.configure(text="Unfavorite" if on else "Favorite")

    def _first_look(self):
        if not self._looked and self.app.s.connected:
            self._looked = True
            if not self.app.s.games:
                self.search()

    def search(self):
        if not self.app.s.connected:
            return self.app.say("not connected to an xbox")
        self.app.job("find og xbox games", self.app.s.find_games, lambda found: self.show(),
                     log_result=lambda found: "%d game%s" % (len(found), "" if len(found) == 1 else "s"))

    def toggle_favorite(self):
        game = self._picked()
        if not game:
            return self.app.say("pick a game first")
        self.app.s.set_favorite(game, game.path.lower() not in self.app.s.favorite_games)
        self.show(keep=game.path)

    def boot(self):
        game = self._picked()
        if not game:
            return self.app.say("pick a game first")
        s = self.app.s
        if not s.connected:
            return self.app.say("not connected to an xbox")
        running = (s.guest.xbe["title"] if s.running else
                   s.title_path.rsplit("\\", 1)[-1] if s.kind == "x360" and s.title_path else "")
        if running and not messagebox.askyesno("Boot a game", "leave %s and boot %s?" % (running, game.name),
                                               parent=self):
            return
        self.app.act("Boot %s" % game.name, lambda: s.boot_game(game),
                     lambda text: (self.app.say(text + ", OG360 finds it once its loaded"), self.app.poll_now()))

    def on_game(self):
        if self.app.s.connected and self.winfo_ismapped():
            self._first_look()
