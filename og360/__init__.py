import struct

__version__ = "1.0.6"


class Game:
    def __init__(self, host=None):
        import json
        from . import paths
        from .log import NullLog
        from .og.caller import FunctionCaller
        from .og.guest import Guest
        from .xbdm import Console, discover
        if not host:
            try:
                with open(paths.path("config.json")) as f:
                    host = json.load(f).get("host")
            except (OSError, ValueError):
                host = None
        if not host:
            found = discover()
            if not found:
                raise RuntimeError("no console found on the network, give it the ip")
            host = found[0][0]
        self.log = NullLog()
        self.console = Console(host, self.log)
        self.guest = Guest(self.console)
        try:
            self.guest.detect()
        except BaseException:
            self.console.close()
            raise
        self.caller = FunctionCaller(self.console, self.log)

    @property
    def title(self):
        return self.guest.xbe["title"]

    @property
    def title_id(self):
        return self.guest.xbe["title_id"]

    @property
    def sections(self):
        return self.guest.xbe["sections"]

    def host_address(self, addr):
        return self.guest.host(self.guest.resolve(addr))

    def read(self, addr, size):
        return self.guest.read(self.guest.resolve(addr), size)

    def write(self, addr, data):
        self.guest.write(self.guest.resolve(addr), bytes(data))

    def dump(self, addr, size):
        return self.guest.read_ranges([(self.guest.resolve(addr), size)])[0]

    def _get(self, fmt, addr):
        return struct.unpack(fmt, self.read(addr, struct.calcsize(fmt)))[0]

    def _put(self, fmt, addr, value):
        self.write(addr, struct.pack(fmt, value))

    def read_u8(self, a): return self._get("<B", a)
    def read_i8(self, a): return self._get("<b", a)
    def read_u16(self, a): return self._get("<H", a)
    def read_i16(self, a): return self._get("<h", a)
    def read_u32(self, a): return self._get("<I", a)
    def read_i32(self, a): return self._get("<i", a)
    def read_float(self, a): return self._get("<f", a)
    def read_double(self, a): return self._get("<d", a)
    def read_bool(self, a): return self.read_u8(a) != 0

    def read_text(self, a, limit=256):
        return self.guest.read(self.guest.resolve(a), limit, fill=b"\0").split(b"\0")[0].decode("latin-1")

    def write_u8(self, a, v): self._put("<B", a, v)
    def write_i8(self, a, v): self._put("<b", a, v)
    def write_u16(self, a, v): self._put("<H", a, v)
    def write_i16(self, a, v): self._put("<h", a, v)
    def write_u32(self, a, v): self._put("<I", a, v)
    def write_i32(self, a, v): self._put("<i", a, v)
    def write_float(self, a, v): self._put("<f", a, v)
    def write_double(self, a, v): self._put("<d", a, v)
    def write_bool(self, a, v): self.write_u8(a, 1 if v else 0)
    def write_text(self, a, s): self.write(a, s.encode("latin-1") + b"\0")

    def call(self, address, *arguments, convention="cdecl", expected=None, string=None, when="auto"):
        return self.caller.call(address, *arguments, convention=convention, expected=expected, string=string,
                                title_id=int(self.title_id, 16), when=when)

    def pause(self):
        self.console.pause(True)

    def resume(self):
        self.console.pause(False)

    def close(self):
        self.console.close()
