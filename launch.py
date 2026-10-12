from contextlib import contextmanager
from datetime import datetime
import glob
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
NEEDS = "import sys, tkinter, numpy, PIL; assert sys.version_info >= (3, 9)"
HAS_TK = "import sys, tkinter, venv, ensurepip; assert sys.version_info >= (3, 9)"
MODERN_TK = ("import tkinter; v = tuple(int(x) for x in tkinter.Tcl().call('info', 'patchlevel').split('.')[:3]); "
             "assert v >= (8, 6, 13)")
OLD_TK_NOTE = ("heads up: this pythons tk is older than 8.6.13. on newer macos a click might only focus the "
               "window. for smooth clicks get the latest python from https://www.python.org/downloads/macos/ "
               "and start OG360 again")
RESTART = 75
FAILED = '.setup-failed'


class SetupError(Exception):
    pass


def clean_environment():
    env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', '__PYVENV_LAUNCHER__'):
        env.pop(key, None)
    return env


def run(command, timeout=60):
    try:
        result = subprocess.run(command, cwd=str(ROOT), env=clean_environment(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = subprocess.CompletedProcess(command, 1, str(exc))
    with (ROOT / 'setup.log').open('a', encoding='utf-8') as log:
        log.write('\n' + datetime.now().isoformat() + ' ' + repr(command) + '\n')
        log.write(result.stdout or '')
    return result


def works(python, check):
    return run([str(python), '-I', '-c', check]).returncode == 0


def interpreter_candidates():
    candidates = [os.environ.get('OG360_PYTHON'), getattr(sys, '_base_executable', sys.executable), sys.executable]
    folders = os.environ.get('PATH', '').split(os.pathsep)
    if sys.platform == 'darwin':
        folders = sorted(glob.glob('/Library/Frameworks/Python.framework/Versions/3.*/bin'), reverse=True) + folders
        folders += ['/usr/local/bin', '/opt/homebrew/bin', '/usr/bin']
    if os.name == 'nt':
        launcher = shutil.which('py')
        if launcher:
            try:
                listed = subprocess.run([launcher, '-0p'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        universal_newlines=True, errors='replace', timeout=15,
                                        env=clean_environment())
                candidates += re.findall(r'([A-Za-z]:\\[^\r\n]*?\.exe)', listed.stdout)
            except (OSError, subprocess.TimeoutExpired):
                pass
        folders += glob.glob(os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Python', 'Python*'))
        folders += glob.glob(os.path.join(os.environ.get('ProgramFiles', ''), 'Python*'))
    for folder in folders:
        if not folder:
            continue
        for filename in sorted(glob.glob(os.path.join(folder, 'python*')), reverse=True):
            if re.fullmatch(r'python(?:3(?:\.\d+)?)?(?:\.exe)?', Path(filename).name):
                candidates.append(filename)
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            continue
        if path.is_file() and key not in seen:
            seen.add(key)
            yield path


def environment_python(folder):
    return folder / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def modern_tk(python):
    return sys.platform != 'darwin' or works(python, MODERN_TK)


def failed_before():
    try:
        return set((ROOT / FAILED).read_text(encoding='utf-8').split('\n')) - {''}
    except OSError:
        return set()


def make_environment(base, folder, local):
    print("setting up numpy and pillow (first start only)...", flush=True)
    backup = None
    if folder.exists():
        if folder.is_symlink() or not (folder / 'pyvenv.cfg').is_file():
            raise SetupError("the .venv folder isnt a python environment. move it somewhere else and start again")
        backup = ROOT / '.setup-backups' / ('venv-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.parent.mkdir(exist_ok=True)
        folder.rename(backup)
    try:
        if run([str(base), '-I', '-m', 'venv', str(folder)], timeout=180).returncode:
            raise SetupError("couldnt make the local environment, see setup.log")
        installed = run([str(local), '-I', '-m', 'pip', 'install', '--disable-pip-version-check',
                         '--only-binary=:all:', '--timeout', '15', '--retries', '1',
                         '-r', str(ROOT / 'requirements.txt')], timeout=600)
        if installed.returncode or not works(local, NEEDS):
            raise SetupError("couldnt install numpy and pillow. check your internet and setup.log then start again")
    except SetupError:
        shutil.rmtree(str(folder), ignore_errors=True)
        if backup is not None:
            backup.rename(folder)
        with (ROOT / FAILED).open('a', encoding='utf-8') as f:
            f.write(str(base) + '\n')
        raise
    return local


@contextmanager
def setup_lock():
    handle = (ROOT / '.setup.lock').open('a+b')
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SetupError("another launcher is setting up this folder. wait for it then start again") from exc
        yield
    finally:
        handle.close()


def find_python():
    folder = ROOT / '.venv'
    local = environment_python(folder)
    candidates = list(interpreter_candidates())
    for python in [local] + candidates:
        if python.is_file() and works(python, NEEDS) and modern_tk(python):
            return python
    skip = failed_before()
    base = next((c for c in candidates if str(c) not in skip and works(c, HAS_TK) and modern_tk(c)), None)
    if base is not None:
        try:
            return make_environment(base, folder, local)
        except SetupError as exc:
            print("couldnt set up %s (%s), trying an older tk" % (base, exc), flush=True)
    for python in [local] + candidates:
        if python.is_file() and works(python, NEEDS):
            print(OLD_TK_NOTE, flush=True)
            return python
    base = next((c for c in candidates if works(c, HAS_TK)), None)
    if base is None:
        raise SetupError("no python 3.9+ with tkinter found. get python from python.org (on linux: python3-tk and "
                         "python3-venv) then start again")
    if not modern_tk(base):
        print(OLD_TK_NOTE, flush=True)
    return make_environment(base, folder, local)


def main():
    try:
        with setup_lock():
            python = find_python()
        env = clean_environment()
        env['OG360_LAUNCHER'] = '1'
        code = subprocess.call([str(python), '-m', 'og360'] + sys.argv[1:], cwd=str(ROOT), env=env)
        if code == RESTART:
            os.execv(sys.executable, [sys.executable, str(ROOT / 'launch.py')] + sys.argv[1:])
        return code
    except (SetupError, OSError) as exc:
        print("\nOG360 couldnt start: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
