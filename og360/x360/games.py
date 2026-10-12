import re

HDD = "Hdd:\\"
DEPTH = 3
SKIP = {"content", "cache", "compatibility"}


class Game:
    def __init__(self, folder, xbe):
        self.folder, self.xbe = folder, xbe

    @property
    def path(self):
        return join(self.folder, self.xbe)

    @property
    def name(self):
        return self.folder.rstrip("\\").rsplit("\\", 1)[-1]


def join(folder, name):
    return folder.rstrip("\\") + "\\" + name


def entries(lines):
    out = []
    for line in lines:
        m = re.search(r'name="([^"]+)"', line)
        if m:
            out.append((m.group(1), line.rstrip().endswith("directory")))
    return out


def find(list_folder, start=HDD, progress=None):
    games, queue, done = [], [(start, 0)], 0
    while queue:
        folder, depth = queue.pop(0)
        try:
            listed = entries(list_folder(folder))
        except Exception:
            if folder == start:
                raise
            listed = []
        done += 1
        if progress:
            progress(done, done + len(queue))
        files = [n for n, is_folder in listed if not is_folder and not n.startswith("._")]
        xbes = sorted((n for n in files if n.lower().endswith(".xbe")), key=lambda n: (n.lower() != "default.xbe", n.lower()))
        if xbes:
            games.append(Game(folder.rstrip("\\"), xbes[0]))
            continue
        if depth >= DEPTH or any(n.lower() == "default.xex" for n in files):
            continue
        queue.extend((join(folder, n), depth + 1) for n, is_folder in listed
                     if is_folder and n.lower() not in SKIP and not n.startswith(("$", ".")))
    return sorted(games, key=lambda g: g.name.lower())


def boot(command, game):
    return command('magicboot title="%s" directory="%s"' % (game.path, game.folder))
