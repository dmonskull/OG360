from .protocol import parse_response_line
from .exceptions import XBDMCommandError, XBDMConnectionError, XBDMMemoryNotMappedError

def read_memory_text(conn, address: int, length: int) -> bytes:
    if length < 0:
        raise ValueError("memory read length cannot be negative")
    cmd = f"getmem addr=0x{address:X} length={length}\r\n"
    conn.send(cmd.encode("ascii"))

    header = conn.recv_line()
    resp = parse_response_line(header)

    if resp.code != 202:
        raise XBDMCommandError(resp.code, resp.message)

    hex_parts = []
    hex_length = 0
    expected_hex_length = length * 2
    while True:
        line = conn.recv_line().strip()
        if line == b".":
            break
        try:
            part = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise XBDMConnectionError("memory response is not ASCII") from exc
        if part and set(part) == {"?"}:
            raise XBDMConnectionError(
                f"memory at 0x{address:08X} is unavailable on the loaded title"
            )
        if len(part) % 2 or any(character not in "0123456789abcdefABCDEF" for character in part):
            raise XBDMConnectionError("memory response contains malformed hex data")
        hex_length += len(part)
        if hex_length > expected_hex_length:
            raise XBDMConnectionError("memory response exceeds requested length")
        hex_parts.append(part)

    if hex_length != expected_hex_length:
        raise XBDMConnectionError("memory response length does not match request")
    return bytes.fromhex("".join(hex_parts))

def read_memory_binary(conn, address: int, length: int) -> bytes:
    if length < 0:
        raise ValueError("memory read length cannot be negative")
    if length == 0:
        return b""
    cmd = f"getmemex addr=0x{address:X} length=0x{length:X}\r\n"
    conn.send(cmd.encode("ascii"))

    header = conn.recv_line()
    resp = parse_response_line(header)

    if resp.code != 203:
        raise XBDMConnectionError(f"unexpected reply to getmemex: {resp.code}")

    out = bytearray()
    while True:
        flags = int.from_bytes(conn.recv_exact(2), "little")
        count = flags & 0x7FFF
        if count:
            out += conn.recv_exact(count)
        if flags & 0x8000 or len(out) >= length:
            break

    if len(out) < length:
        raise XBDMMemoryNotMappedError(f"0x{address + len(out):08X}")
    return bytes(out[:length])

def write_memory(conn, address: int, data: bytes):
    cmd = f"setmem addr=0x{address:X} data={data.hex()}\r\n"
    conn.send(cmd.encode())

    header = conn.recv_line()
    resp = parse_response_line(header)

    if resp.code != 200:
        raise XBDMCommandError(resp.code, resp.message)
