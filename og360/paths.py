import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    env = os.environ.get("OG360_HOME")
    if env:
        return env
    if os.path.isfile(os.path.join(ROOT, "launch.py")) and os.access(ROOT, os.W_OK):
        return ROOT
    return os.path.join(os.path.expanduser("~"), ".og360")


def path(*parts):
    return os.path.join(data_dir(), *parts)


def bundled(*parts):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), *parts)
