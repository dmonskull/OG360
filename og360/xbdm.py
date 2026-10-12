import contextlib
import errno
import io
import itertools
import os
import queue
import socket
import sys
import threading
from concurrent.futures import Future

from .log import QUERIES, NullLog, describe, size_text, verb_of

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "py-xbdm")
_PY_XBDM = os.environ.get("PY_XBDM_PATH") or VENDOR
if _PY_XBDM not in sys.path:
    sys.path.insert(0, _PY_XBDM)

from py_xbdm import discovery as _discovery
from py_xbdm.client import XBDMClient
from py_xbdm.exceptions import (XBDMCommandError, XBDMConnectionError,
                                XBDMMemoryNotMappedError)

PORT = int(os.environ.get("OG360_XBDM_PORT", 730))

XbdmError = XBDMCommandError
Unreadable = XBDMMemoryNotMappedError


class Cancelled(Exception):
    pass


class ConsoleLost(ConnectionError):
    pass


_NETWORK_ERRNOS = {errno.ECONNREFUSED, errno.ECONNRESET, errno.ECONNABORTED, errno.EPIPE,
                   errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ENETDOWN,
                   errno.EHOSTDOWN, errno.ENOTCONN, errno.EBADF}


def is_connection_error(error):
    if isinstance(error, (ConnectionError, XBDMConnectionError, EOFError, TimeoutError,
                          socket.timeout, socket.gaierror, socket.herror)):
        return True
    if isinstance(error, ValueError) and "XBDM response" in str(error):
        return True
    return isinstance(error, OSError) and error.errno in _NETWORK_ERRNOS


def split_host(host):
    host = (host or "").strip()
    if host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if port.isdigit():
            return name, int(port)
    return host, PORT


def _args_text(args):
    out = []
    for a in args or []:
        if isinstance(a, str):
            out.append('"%s"' % a)
        elif isinstance(a, int) and a >= 0x10000:
            out.append("0x%X" % a)
        else:
            out.append(repr(a))
    return ", ".join(out)


class Client(XBDMClient):
    def __init__(self, host, timeout=8.0, log=None):
        name, port = split_host(host)
        if not name:
            raise ValueError("no console address")
        super().__init__(host=name, port=port, timeout=timeout)
        self.host = host
        self.timeout = timeout
        self.log = log or NullLog()
        self.jrpc = None
        self._label = None

    def connect(self):
        self.__enter__()
        self.log.exchange("connect " + self.host, "201- connected")
        return self


    def _note(self, command, code, message, rows=None):
        reply = "%d- %s" % (code, str(message).strip()) if code is not None else str(message)
        if rows is not None:
            reply = "%d line%s" % (len(rows), "" if len(rows) == 1 else "s")
        detail = rows[:200] if rows else None
        ok = code is None or code < 400
        if self._label:
            command, self._label = self._label, None
        if ok and verb_of(command) in QUERIES:
            self.log.query(command, reply, detail)
        else:
            self.log.exchange(command, reply, ok=ok, detail=detail)

    def _failed(self, command, error):
        if isinstance(error, XBDMCommandError):
            self._note(command, error.code, error.message)
        else:
            self._label = None
            self.log.exchange(command, describe(error), ok=False)


    def send_command(self, command):
        try:
            resp = super().send_command(command)
        except Exception as e:
            self._failed(command, e)
            raise
        self._note(command, resp.code, resp.message)
        return resp

    def send_multiline_command(self, command):
        try:
            rows = super().send_multiline_command(command)
        except Exception as e:
            self._failed(command, e)
            raise
        self._note(command, None, "", rows)
        return rows

    def read_memory(self, address, size):
        try:
            data = super().read_memory(address, size)
        except XBDMConnectionError as e:
            if "unavailable on the loaded title" not in str(e):
                raise
            self.log.exchange("getmem addr=0x%X length=%d" % (address, size), "not readable", ok=False)
            self.reconnect()
            raise XBDMMemoryNotMappedError("0x%08X" % address) from e
        self.log.read(size)
        return data

    def read_memory_binary(self, address, size):
        data = super().read_memory_binary(address, size)
        self.log.read(size)
        return data

    def write_memory(self, address, data):
        command = "setmem addr=0x%X data=%s" % (address, bytes(data).hex())
        try:
            super().write_memory(address, data)
        except Exception as e:
            self._failed(command, e)
            raise
        self.log.exchange(command, "200- OK")

    def send_file(self, local_path, remote_path, overwrite=False):
        command = 'sendfile name="%s" length=0x%X' % (remote_path, os.path.getsize(local_path))
        try:
            super().send_file(local_path, remote_path, overwrite)
        except Exception as e:
            self._failed(command, e)
            raise
        self.log.exchange(command, "200- OK (%s sent)" % size_text(os.path.getsize(local_path)))

    def receive_file(self, remote_path, local_path):
        command = 'getfile name="%s"' % remote_path
        try:
            super().receive_file(remote_path, local_path)
        except Exception as e:
            self._failed(command, e)
            raise
        self.log.exchange(command, "203- binary (%s received)" % size_text(os.path.getsize(local_path)))

    def screenshot(self, output_path=None, rawmode="BGRA", untile_mode="xg"):
        self.conn.timeout = 30.0
        if self.conn.sock:
            self.conn.sock.settimeout(30.0)
        try:
            path = super().screenshot(output_path, rawmode, untile_mode)
        except Exception as e:
            self._failed("screenshot", e)
            raise
        finally:
            self.conn.timeout = self.timeout
            if self.conn.sock:
                self.conn.sock.settimeout(self.timeout)
        self.log.exchange("screenshot", "203- binary (frame saved)")
        return path

    def call_function(self, address, args, return_type=XBDMClient.INT, system_thread=True):
        self._label = "JRPC2 call 0x%08X(%s)%s" % (address, _args_text(args), " on a system thread" if system_thread else "")
        try:
            return super().call_function(address, args, return_type, system_thread)
        finally:
            self._label = None

    def _typed_read(self, name, size, address, *rest):
        value = getattr(super(), name)(address, *rest)
        self.log.read(size)
        return value

    def read_u16(self, address):
        return self._typed_read("read_u16", 2, address)

    def read_u32(self, address):
        return self._typed_read("read_u32", 4, address)

    def read_u64(self, address):
        return self._typed_read("read_u64", 8, address)

    def read_float(self, address):
        return self._typed_read("read_float", 4, address)

    def read_double(self, address):
        return self._typed_read("read_double", 8, address)

    def _typed_write(self, name, shown, address, *values):
        command = "setmem addr=0x%X (%s %s)" % (address, name[6:], shown)
        try:
            getattr(super(), name)(address, *values)
        except Exception as e:
            self._failed(command, e)
            raise
        self.log.exchange(command, "200- OK")

    def write_u8(self, address, value):
        self._typed_write("write_u8", value, address, value)

    def write_u16(self, address, value):
        self._typed_write("write_u16", value, address, value)

    def write_u32(self, address, value):
        self._typed_write("write_u32", "0x%X" % value, address, value)

    def write_u64(self, address, value):
        self._typed_write("write_u64", "0x%X" % value, address, value)

    def write_float(self, address, value):
        self._typed_write("write_float", value, address, value)

    def write_double(self, address, value):
        self._typed_write("write_double", value, address, value)

    def write_boolean(self, address, value):
        self._typed_write("write_boolean", value, address, value)

    def write_string(self, address, string):
        self._typed_write("write_string", repr(string), address, string)


    def command(self, text):
        resp = self.send_command(text)
        return resp.code, resp.message.strip()

    def lines(self, text):
        return self.send_multiline_command(text)

    def read(self, address, size):
        return self.read_memory_binary(address, size)

    def write(self, address, data):
        data = bytes(data)
        for off in range(0, len(data), 0x80):
            self.write_memory(address + off, data[off:off + 0x80])

    def has_jrpc(self):
        if self.jrpc is None:
            try:
                self.get_kernel_version()
                self.jrpc = True
            except XBDMCommandError:
                self.jrpc = False
        return self.jrpc


class Console:
    BIG = 0x40000

    def __init__(self, host, log=None, timeout=8.0, on_lost=None, name="og360-console"):
        self.host = host
        self.log = log or NullLog()
        self.timeout = timeout
        self.on_lost = on_lost
        self.pause_big_reads = True
        self.stopped = False
        self.holding = False
        self.alive = True
        self._owe_go = False
        self._client = None
        self._closed = False
        self._queue = queue.PriorityQueue()
        self._order = itertools.count()
        self._thread = threading.Thread(target=self._work, name=name, daemon=True)
        self._thread.start()


    def _submit(self, fn, priority=0, reconnect=False):
        future = Future()
        if self._closed or not (self.alive or reconnect):
            future.set_exception(ConsoleLost("not connected to the xbox"))
            return future
        self._queue.put((priority, next(self._order), fn, future, self.log.current()))
        return future

    def run(self, fn, priority=0):
        return self._submit(fn, priority).result()

    def reconnect(self):
        return self._submit(lambda c: c.console_name(), reconnect=True).result()

    def _work(self):
        while True:
            _, _, fn, future, action = self._queue.get()
            if fn is None:
                break
            if not future.set_running_or_notify_cancel():
                continue
            with self.log.bound(action):
                try:
                    if self._client is None:
                        client = Client(self.host, self.timeout, self.log)
                        try:
                            client.connect()
                        except BaseException:
                            with contextlib.suppress(Exception):
                                client.conn.close()
                            raise
                        self._client = client
                        self.alive = True
                        if self._owe_go:
                            with contextlib.suppress(XbdmError):
                                client.command("go")
                            self._owe_go = False
                    future.set_result(fn(self._client))
                except BaseException as e:
                    if is_connection_error(e):
                        lost = e if isinstance(e, ConsoleLost) else ConsoleLost(
                            "the xbox stopped answering (%s)" % describe(e))
                        self._lost(lost)
                        future.set_exception(lost)
                        self._fail_waiting(lost)
                    else:
                        future.set_exception(e)
        self._fail_waiting(ConsoleLost("disconnected"))
        self._drop()

    def _lost(self, error):
        self._drop()
        if self.alive:
            self.alive = False
            if self.on_lost:
                with contextlib.suppress(Exception):
                    self.on_lost(error)

    def _drop(self):
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _fail_waiting(self, error):
        waiting = []
        while True:
            try:
                waiting.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for item in waiting:
            if item[2] is None:
                self._queue.put(item)
            elif item[3].set_running_or_notify_cancel():
                item[3].set_exception(error)

    def close(self):
        self._closed = True
        self._queue.put((-1, next(self._order), None, None, None))


    def read(self, address, size):
        if size <= 0:
            return b""
        return self.run(lambda c: c.read(address, size))

    def write(self, address, data):
        self.run(lambda c: c.write(address, data))

    def command(self, text):
        return self.run(lambda c: c.command(text))

    def lines(self, text):
        return self.run(lambda c: c.lines(text))

    def name(self):
        return self.run(lambda c: c.console_name())

    def regions(self):
        out = []
        for line in self.lines("walkmem"):
            f = dict(p.split("=", 1) for p in line.split() if "=" in p)
            with contextlib.suppress(KeyError, ValueError):
                out.append((int(f["base"], 16), int(f["size"], 16)))
        return out

    def pause(self, want):
        try:
            self.run(lambda c: c.debug_stop() if want else c.debug_go())
        except XbdmError as e:
            if e.code not in (408, 426):
                raise
        self.stopped = bool(want)

    @contextlib.contextmanager
    def quiet(self, size, force=False):
        mine = False
        if (force or self.pause_big_reads and size >= self.BIG) and not self.stopped and not self.holding:
            try:
                self.command("stop")
                mine = self.holding = True
            except XbdmError:
                pass
        try:
            yield
        finally:
            if mine:
                self.holding = False
                try:
                    self.command("go")
                except XbdmError:
                    pass
                except Exception:
                    self._owe_go = True

    def read_many(self, spans, progress=None):
        total = sum(size for _, size in spans)
        priority = 1 if total >= self.BIG else 0
        step = 0x20000 if self.holding or self.stopped else 0x8000
        outs = [bytearray(size) for _, size in spans]
        pieces = []
        for i, (address, size) in enumerate(spans):
            for off in range(0, size, step):
                n = min(step, size - off)
                pieces.append((i, off, n, self._submit(lambda c, a=address + off, n=n: c.read(a, n), priority)))
        bad = set()
        done = 0
        try:
            for i, off, n, future in pieces:
                try:
                    outs[i][off:off + n] = future.result()
                except (Unreadable, XbdmError):
                    bad.add(i)
                done += n
                if progress:
                    progress(done, total or 1)
        except BaseException:
            for piece in pieces:
                piece[3].cancel()
            raise
        return [None if i in bad else bytes(o) for i, o in enumerate(outs)]

    def screenshot(self, folder):
        return self.run(lambda c: c.screenshot(folder))


def discover():
    found = []
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            found = list(_discovery.discover_xbdm_with_names(find_all=True))
        except Exception:
            found = []
        if not found:
            try:
                found = [(ip, "") for ip in _discovery.discover_xbdm(find_all=True)]
            except Exception:
                found = []
    seen, out = set(), []
    for ip, name in found:
        if ip not in seen:
            seen.add(ip)
            out.append((ip, name or ""))
    return sorted(out)
