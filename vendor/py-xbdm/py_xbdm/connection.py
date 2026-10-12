import socket

from py_xbdm import discovery

from .exceptions import XBDMConnectionError


class XBDMConnection:
    RECV_BUFSIZE = 4096
    MAX_RESPONSE_LINE_BYTES = 64 * 1024

    def __init__(self, host = None, port=730, timeout=5.0):
        self.host = discovery.xbox_ip() if host is None else host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self._buf = b""

    @property
    def is_connected(self) -> bool:
        if self.sock is None:
            return False
        try:
            self.sock.setblocking(False)
            try:
                data = self.sock.recv(1, socket.MSG_PEEK)
                if data == b"":
                    return False
            except BlockingIOError:
                pass
            except (OSError, ConnectionError):
                return False
            finally:
                self.sock.setblocking(True)
                self.sock.settimeout(self.timeout)
            return True
        except Exception:
            return False

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as e:
            raise XBDMConnectionError(str(e))
        self._buf = b""

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        self._buf = b""

    def send(self, data: bytes):
        if not self.sock:
            raise XBDMConnectionError("Not connected")
        self.sock.sendall(data)

    def _fill_buf(self):
        chunk = self.sock.recv(self.RECV_BUFSIZE)
        if not chunk:
            raise XBDMConnectionError("Connection closed")
        self._buf += chunk

    def recv(self, bufsize: int) -> bytes:
        if not self.sock:
            raise XBDMConnectionError("Not connected")
        if self._buf:
            data = self._buf[:bufsize]
            self._buf = self._buf[bufsize:]
            return data
        return self.sock.recv(bufsize)

    def recv_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            self._fill_buf()
        data = self._buf[:size]
        self._buf = self._buf[size:]
        return data

    def recv_line(self) -> bytes:
        while True:
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line_length = newline + 1
                if line_length > self.MAX_RESPONSE_LINE_BYTES:
                    self.close()
                    raise XBDMConnectionError("XBDM response line exceeds size limit")
                line = self._buf[:line_length]
                self._buf = self._buf[line_length:]
                return line
            if len(self._buf) >= self.MAX_RESPONSE_LINE_BYTES:
                self.close()
                raise XBDMConnectionError("XBDM response line exceeds size limit")
            self._fill_buf()
