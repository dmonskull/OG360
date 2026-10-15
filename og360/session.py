import json
import os
import threading
import time

from . import paths
from .log import describe
from .og.caller import FunctionCaller
from .og.guest import Guest, NotRunning
from .og.mods import ModTable
from .xbdm import Console, Unreadable, XbdmError, discover
from .x360 import capture, games, network
from .x360.base import running_title

AREAS = (
    ("data", "Game data (xbe data sections)"),
    ("heap", "Heap (memory the game made)"),
    ("all", "All game memory (data + heap)"),
    ("image", "Whole xbe (code + data)"),
    ("custom", "Custom range"),
)
SPEED_STOPPED = 750 * 1024
SPEED_RUNNING = 75 * 1024
RETRY_SAVED = 5
DISCOVERY_WAITS = (0, 15, 30, 60)


class Session:
    def __init__(self, log):
        self.log = log
        self.config = {}
        try:
            with open(paths.path("config.json")) as f:
                self.config = json.load(f)
        except (OSError, ValueError):
            pass
        self.console = None
        self.guest = None
        self.mods = None
        self.caller = None
        self.host = None
        self.console_name = ""
        self.problem = "not connected yet"
        self.found = []
        self.kind = None
        self.title_path = ""
        self.jrpc = None
        self.searching = False
        self._xbe_host = None
        self._lock = threading.RLock()
        self._searches = 0
        self._next_search = 0.0
        self._next_retry = 0.0
        self._title_key = None
        self.on_lost = None


    def _save_config(self):
        os.makedirs(paths.data_dir(), exist_ok=True)
        with open(paths.path("config.json"), "w") as f:
            json.dump(self.config, f, indent=2)

    @property
    def saved_host(self):
        return (self.config.get("host") or "").strip()

    @property
    def pause_big_reads(self):
        return bool(self.config.get("pause_big_reads", True))

    @pause_big_reads.setter
    def pause_big_reads(self, on):
        self.config["pause_big_reads"] = bool(on)
        self._save_config()
        if self.console:
            self.console.pause_big_reads = bool(on)

    @property
    def update_box(self):
        return bool(self.config.get("update_box", True))

    @update_box.setter
    def update_box(self, on):
        self.config["update_box"] = bool(on)
        self._save_config()


    def connect(self, host, how="typed"):
        host = (host or "").strip()
        if not host or any(c.isspace() for c in host):
            raise ValueError("type the xbox ip (like 192.168.1.50)")
        with self._lock:
            console = Console(host, self.log, on_lost=self._lost)
            console.pause_big_reads = self.pause_big_reads
            try:
                name = console.name()
            except Exception as e:
                console.close()
                self.problem = "no answer from %s (%s)" % (host, describe(e))
                raise ValueError(self.problem) from None
            self._drop_consoles()
            self.console, self.host, self.console_name = console, host, name
            self.guest = Guest(console)
            self.mods = ModTable(self.guest, self.log)
            self.mods.game_folders = list(self.config.get("game_folders", []))
            self.caller = FunctionCaller(console, self.log)
            self.mods.caller = self.caller
            self.kind = None
            self._xbe_host = None
            self._title_key = None
            self.jrpc = None
            self.problem = ""
            if self.config.get("host") != host:
                self.config["host"] = host
                self._save_config()
            self.log.ok("connected to %s (%s)%s" % (name, host, {"saved": ", the saved one",
                                                                 "found": ", found on the network"}.get(how, "")),
                        key=("connection", "connected", host))
            self._detect()
        return name

    def _drop_consoles(self):
        if self.mods:
            self.mods.unfreeze_all()
        if self.console:
            self.console.close()
        self.console = None

    def _lost(self, error):
        self.problem = "lost connection to %s (%s)" % (self.host, describe(error))
        self.log.warn("lost connection to %s: %s. trying again" % (self.host, describe(error)),
                      key=("connection", "lost", self.host))
        self._next_retry = time.time() + 1
        if self.mods:
            had = self.mods.unfreeze_all()
            if had:
                self.log.warn("locks turned off: %s" % ", ".join(had), key="unfreeze")
        if self.on_lost:
            self.on_lost()

    def search(self):
        self.searching = True
        try:
            with self.log.action("find consoles on the network") as act:
                found = discover()
                self.found = found
                if found:
                    act.done(", ".join("%s (%s)" % (ip, name or "?") for ip, name in found))
                else:
                    act.done("no console answered", level="warn")
            return found
        finally:
            self.searching = False

    def check(self):
        with self._lock:
            now = time.time()
            if self.console is None:
                return self._auto_connect(now)
            if not self.console.alive:
                if now < self._next_retry:
                    return False
                self._next_retry = now + RETRY_SAVED
                try:
                    self.console.reconnect()
                except Exception:
                    return False
                self.problem = ""
                self.log.ok("reconnected to %s (%s)" % (self.console_name, self.host),
                            key=("connection", "reconnected", self.host))
                self._xbe_host = None
                self._title_key = None
            try:
                self._watch()
            except Exception as e:
                if self.console and self.console.alive:
                    self.problem = "the xbox didnt answer (%s)" % describe(e)
            return self.running

    def _auto_connect(self, now):
        saved = self.saved_host
        if saved and self._searches == 0 and now >= self._next_search:
            self._searches = 1
            try:
                with self.log.action("connect to the saved console %s" % saved) as act:
                    name = self.connect(saved, "saved")
                    act.done(name)
                return self.running
            except ValueError:
                self.log.warn("saved console %s didnt answer, looking on the network" % saved,
                              key=("connection", "saved-down", saved))
        if now < self._next_search:
            return False
        wait = DISCOVERY_WAITS[min(self._searches, len(DISCOVERY_WAITS) - 1)]
        self._searches += 1
        self._next_search = now + max(wait, 10)
        found = self.search()
        if len(found) == 1:
            try:
                self.connect(found[0][0], "found")
                return self.running
            except ValueError:
                pass
        if len(found) > 1:
            self.problem = "found %d consoles. pick one next to console and hit connect" % len(found)
            self.log.warn(self.problem, key=("connection", "several"))
        elif not found:
            self.problem = ("no console found. type its ip next to console (looking again in %d s)" % int(self._next_search - time.time()))
            self.log.warn("no console found. type its ip next to console, still looking", key=("connection", "none"))
        return False


    def _watch(self):
        if self.kind == "og" and self._xbe_host is not None:
            try:
                if self.console.read(self._xbe_host, 4) == b"XBEH":
                    return
            except (Unreadable, XbdmError):
                pass
        elif self.kind in ("x360", "dashboard"):
            path = self.console.run(running_title)
            if path == self.title_path and path:
                return
        self._detect()

    def _detect(self):
        why = ""
        try:
            self.guest.detect()
            ok = bool(self.guest.xbe)
        except NotRunning as e:
            ok, why = False, str(e)
        except (Unreadable, XbdmError) as e:
            ok, why = False, describe(e)
        if ok:
            x = self.guest.xbe
            self.kind = "og"
            self.title_path = ""
            self._xbe_host = self.guest.host(0x10000)
            self.problem = ""
            key = ("og", x["title_id"], self.guest.ram_base)
            if self.mods.title_id != x["title_id"]:
                self.mods.unfreeze_all()
                self.mods.load_for(x["title_id"], x["title"])
            if key != self._title_key:
                self._title_key = key
                self.log.info("%s (%s) is running in %s" % (x["title"], x["title_id"], self.guest.emulator),
                              key="title")
                self._keep_network()
            return
        self._xbe_host = None
        if self.guest.emulator:
            self.kind = "og"
            self.title_path = ""
            self.problem = (why[0].upper() + why[1:] + "." if why else
                            "%s is running, the game hasnt loaded yet" % self.guest.emulator)
            key = ("og-wait", self.guest.emulator)
        else:
            self.title_path = self.console.run(running_title)
            if "dash.xex" in self.title_path.lower() or not self.title_path:
                self.kind = "dashboard"
                self.problem = "the xbox is on the dashboard, start a game"
            else:
                self.kind = "x360"
                self.problem = ""
            key = (self.kind, self.title_path)
        if key != self._title_key:
            self._title_key = key
            if self.kind == "x360":
                self.log.info("xbox 360 game running: %s" % self.title_path, key="title")
            else:
                self.log.info(self.problem, key="title")
            self._keep_network()

    def _in_xefu(self):
        return self.kind == "og" or "\\compatibility\\" in (self.title_path or "").lower()

    def _keep_network(self):
        try:
            try:
                card = self.console.run(network.card)
            except LookupError:
                card = None
            if card is None or (card.kind == "free" and not self.console.run(lambda c: c.has_jrpc())):
                self._install_network_fix()
            elif card.kind == "elsewhere":
                self.log.info("%s answers games that take the network card (xam export #109) so the xbox stays online" % (card.module or "Another module"), key="network")
            elif card.kind == "taken":
                self.log.info("something else has the network card right now so OG360 left it alone", key="network")
            elif card.kind == "free":
                with self.log.action("keep the xbox on the network in og games") as act:
                    self.console.run(lambda c: network.claim(c, card))
                    act.done("the xbox holds its own network card now so og games cant take it (till the xbox restarts)")
        except Exception as e:
            if self.console and self.console.alive:
                self.log.warn("couldnt make sure og games leave the xbox on the network: %s"
                              % describe(e), key="network")

    def _install_network_fix(self):
        state = self.console.run(network.look)
        if state.kind == "open" and self._in_xefu():
            self.log.info("left the network fix alone, an og game is already running and adding it now freezes "
                          "the xbox. open OG360 before you start a game to keep the xbox online", key="network")
        elif state.kind == "open":
            with self.log.action("keep the xbox on the network in og games") as act:
                self.console.run(lambda c: network.install(c, state))
                act.done("games now get their answer from 0x%08X instead of taking the network card (till the xbox "
                         "restarts)" % state.spot)
        elif state.kind == "elsewhere":
            self.log.info("%s answers games that take the network card (xam export #109) so the xbox stays online" % (state.owner or "Another module"), key="network")

    @property
    def running(self):
        return bool(self.kind == "og" and self.guest and self.guest.ram_base and self.guest.xbe and not self.problem)

    @property
    def connected(self):
        return bool(self.console and self.console.alive)

    def need_game(self):
        if not self.running:
            raise NotRunning(self.problem or "no og xbox game running")
        return self.guest

    def summary(self):
        if not self.console:
            return self.problem or "not connected"
        where = "%s (%s)" % (self.console_name, self.host)
        if not self.console.alive:
            return self.problem
        if self.running:
            x = self.guest.xbe
            return "%s  ·  %s  ·  %s  ·  %s" % (x["title"], x["title_id"], self.guest.emulator, where)
        if self.kind == "x360":
            return "Xbox 360: %s  ·  %s" % (self.title_path.rsplit("\\", 2)[-2] if "\\" in self.title_path
                                                else self.title_path, where)
        return "%s  ·  %s" % (self.problem or "Connected", where)


    def regions(self, area, lo="", hi=""):
        g = self.need_game()
        custom = (g.resolve(lo), g.resolve(hi)) if area == "custom" else None
        return g.regions(area, custom)

    def areas(self):
        g = self.need_game()
        out = []
        for name, label in AREAS:
            if name == "custom":
                out.append((name, label))
                continue
            try:
                size = sum(n for _, n in g.regions(name))
            except Exception:
                size = 0
            speed = SPEED_STOPPED if self.pause_big_reads or self.paused else SPEED_RUNNING
            out.append((name, "%s  -  %.1f MB, about %d s" % (label, size / 2 ** 20, max(1, round(size / speed)))))
        return out

    def describe(self, addr):
        g = self.need_game()
        phys = g.translate(addr)
        return {"addr": addr, "phys": phys, "host": None if phys is None else g.ram_base + phys,
                "section": g.section_of(addr)}

    @property
    def paused(self):
        return bool(self.console and self.console.stopped)

    def pause(self, want):
        if not self.connected:
            raise ValueError("not connected")
        self.console.pause(want)

    def screenshot(self):
        if not self.connected:
            raise ValueError("not connected")
        folder = paths.path("screenshots")
        build = self.console.run(capture.emulator)
        if capture.xbdm_safe(build):
            return self.console.screenshot(folder)
        self.log.info("screenshots in %s are copied from screen memory (xbdms own screenshot can freeze the xbox on "
                      "this emulator)" % build, key=("screenshot", build))
        return capture.capture(self.console, folder)

    def dump(self, regions, progress=None):
        g = self.need_game()
        folder = paths.path("dumps")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = []
        for (va, size), buf in zip(regions, g.read_ranges(regions, progress)):
            name = os.path.join(folder, "%s_%s_%08X-%08X.bin" % (g.xbe["title_id"], stamp, va, va + size))
            with open(name, "wb") as f:
                f.write(buf)
            out.append(name)
        return out


    @property
    def games_folder(self):
        return (self.config.get("games_folder") or "").strip()

    @games_folder.setter
    def games_folder(self, folder):
        folder = (folder or "").strip()
        if folder != self.games_folder:
            self.config["games_folder"] = folder
            self.config.pop("games_found", None)
            self._save_config()

    @property
    def games(self):
        favorites = self.favorite_games
        found = [games.Game(folder, xbe) for folder, xbe in self.config.get("games_found", [])]
        return sorted(found, key=lambda g: (g.path.lower() not in favorites, g.name.lower()))

    @property
    def favorite_games(self):
        return {p.lower() for p in self.config.get("favorite_games", [])}

    def set_favorite(self, game, on):
        favorites = [p for p in self.config.get("favorite_games", []) if p.lower() != game.path.lower()]
        if on:
            favorites.append(game.path)
        self.config["favorite_games"] = favorites
        self._save_config()

    def find_games(self, progress=None):
        if not self.connected:
            raise ValueError("not connected")
        start = self.games_folder or games.HDD
        found = games.find(lambda folder: self.console.lines('dirlist name="%s"' % folder), start, progress)
        self.config["games_found"] = [[g.folder, g.xbe] for g in found]
        self._save_config()
        return self.games

    def boot_game(self, game):
        if not self.connected:
            raise ValueError("not connected")
        with self._lock:
            released = self.mods.release_all() if self.mods else []
            if released:
                self.log.info("put back before booting %s: %s" % (game.name, ", ".join(released)))
            if self.console.stopped or self.console.holding:
                self.console.pause(False)
            self._xbe_host = self._title_key = None
            self.kind = None
            self.problem = "Starting %s..." % game.name
            games.boot(self.console.command, game)
        return "%s is starting" % game.name

    def remember_game_folders(self):
        if self.mods and self.mods.game_folders != self.config.get("game_folders", []):
            self.config["game_folders"] = list(self.mods.game_folders)
            self._save_config()

    def shutdown(self):
        try:
            if self.mods:
                self.mods.all_off()
            if self.console and self.console.alive and (self.console.stopped or self.console.holding):
                self.console.pause(False)
        except Exception:
            pass
        if self.console:
            self.console.close()
