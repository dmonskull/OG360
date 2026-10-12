import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
import zipfile

from . import __version__, paths

REPO = "dmonskull/OG360"
API = "https://api.github.com/repos/%s/releases/latest"
RESTART = 75
KEEP = {"config.json", "logs", "reports", "screenshots", "dumps", "games", "mods", ".venv", "setup.log",
        ".setup.lock", ".setup-backups", ".setup-failed", ".update", ".DS_Store"}


class UpdateError(Exception):
    pass


def release_info():
    try:
        with open(paths.bundled("release.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def version_tuple(text):
    return tuple(int(n) for n in re.findall(r"\d+", str(text))[:4])


def _request(url):
    return urllib.request.Request(url, headers={"User-Agent": "OG360/" + __version__,
                                                "Accept": "application/vnd.github+json"})


def _curl(url, dest=None):
    cmd = ["curl", "-fsSL", "--max-time", "600", "-H", "User-Agent: OG360/" + __version__, url]
    if dest:
        cmd[1:1] = ["-o", dest]
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode:
        raise UpdateError("couldnt reach github (%s)" % out.stderr.decode(errors="replace").strip())
    return out.stdout


def _fetch(url, timeout=15):
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError:
        raise
    except (urllib.error.URLError, ssl.SSLError) as e:
        if "CERTIFICATE" in str(e).upper() and shutil.which("curl"):
            return _curl(url)
        raise UpdateError("couldnt reach github (%s)" % e)


def latest():
    repo = (release_info() or {}).get("repo") or REPO
    try:
        data = json.loads(_fetch(API % repo))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise UpdateError("github said %s" % e.code)
    tag = data.get("tag_name") or ""
    if not version_tuple(tag):
        return None
    zips = [a for a in data.get("assets") or [] if str(a.get("name", "")).lower().endswith(".zip")]
    zips.sort(key=lambda a: "og360" not in a["name"].lower())
    url = zips[0]["browser_download_url"] if zips else data.get("zipball_url")
    if not url:
        return None
    return {"version": tag.lstrip("vV"), "tag": tag, "notes": (data.get("body") or "").strip(), "url": url,
            "size": zips[0].get("size", 0) if zips else 0}


def newer():
    if not release_info():
        return None
    rel = latest()
    if rel and version_tuple(rel["version"]) > version_tuple(__version__):
        return rel
    return None


def _download(url, dest, size, progress):
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or size or 0)
            done = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, max(total, done))
    except (urllib.error.URLError, ssl.SSLError) as e:
        if "CERTIFICATE" in str(e).upper() and shutil.which("curl"):
            _curl(url, dest)
        else:
            raise UpdateError("download failed (%s)" % e)


def _unpack(zpath, into):
    top = os.path.realpath(into)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            target = os.path.realpath(os.path.join(top, info.filename))
            if target != top and not target.startswith(top + os.sep):
                raise UpdateError("the zip has a path outside its folder: %s" % info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise UpdateError("the zip has a link in it: %s" % info.filename)
            z.extract(info, top)
            perms = (info.external_attr >> 16) & 0o777
            if perms and not info.is_dir():
                os.chmod(target, perms)


def _package(folder):
    for here, dirs, files in os.walk(folder):
        if "launch.py" in files and os.path.isfile(os.path.join(here, "og360", "__init__.py")):
            return here
        if here.count(os.sep) - folder.count(os.sep) >= 2:
            dirs[:] = []
    raise UpdateError("the download doesnt look like an OG360 release")


def _version_of(package):
    with open(os.path.join(package, "og360", "__init__.py"), encoding="utf-8") as f:
        m = re.search(r"__version__\s*=\s*[\"']([^\"']+)", f.read())
    return m.group(1) if m else ""


def install(rel, progress=None):
    if not release_info():
        raise UpdateError("this is the working copy, it doesnt update itself")
    root = paths.ROOT
    work = os.path.join(root, ".update")
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    zpath = os.path.join(work, "download.zip")
    _download(rel["url"], zpath, rel.get("size", 0), progress)
    fresh = os.path.join(work, "new")
    os.makedirs(fresh)
    try:
        _unpack(zpath, fresh)
    except zipfile.BadZipFile:
        raise UpdateError("the download isnt a zip")
    package = _package(fresh)
    version = _version_of(package)
    installed = _version_of(root) or __version__
    if version_tuple(version) <= version_tuple(installed):
        raise UpdateError("the download is %s, not newer than this copy (%s)" % (version or "?", installed))
    names = [n for n in os.listdir(package) if n not in KEEP and n != "__pycache__"]
    previous = os.path.join(work, "previous")
    os.makedirs(previous)
    moved_out, moved_in = [], []
    try:
        for n in names:
            if os.path.lexists(os.path.join(root, n)):
                os.replace(os.path.join(root, n), os.path.join(previous, n))
                moved_out.append(n)
        for n in names:
            os.replace(os.path.join(package, n), os.path.join(root, n))
            moved_in.append(n)
    except OSError as e:
        for n in moved_in:
            os.replace(os.path.join(root, n), os.path.join(package, n))
        for n in moved_out:
            os.replace(os.path.join(previous, n), os.path.join(root, n))
        raise UpdateError("couldnt put the new files in place, nothing changed (%s)" % e)
    os.remove(zpath)
    shutil.rmtree(fresh, ignore_errors=True)
    return version
