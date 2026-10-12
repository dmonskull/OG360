import ctypes
import enum
import os
import re
import struct
import tempfile
import threading
import time
from typing import Optional

import numpy as np
from PIL import Image

from py_xbdm.connection import XBDMConnection
from py_xbdm.exceptions import XBDMConnectionError, XBDMFileAlreadyExistsError
from py_xbdm.protocol import parse_response_line
from py_xbdm.memory import read_memory_binary, read_memory_text, write_memory

class XBDMClient:
    MAX_MULTILINE_RESPONSE_LINES = 4096
    MAX_MULTILINE_RESPONSE_BYTES = 4 * 1024 * 1024

    VOID        = 0
    INT         = 1
    FLOAT       = 3
    BYTE        = 4
    STRING      = 2
    UINT64      = 8

    INT_ARRAY   = 5
    FLOAT_ARRAY = 6
    BYTE_ARRAY  = 7
    UINT64_ARRAY= 9

    class SignInState(enum.IntEnum):
        NOT_SIGNED_IN = 0
        SIGNED_IN_LOCALLY = 1
        SIGNED_IN_TO_XBOX_LIVE = 2
        GUEST_ACCOUNT_LOCALLY = 3
        GUEST_ACCOUNT_XBOX_LIVE = 4

    class NotifyType(enum.IntEnum):
        FRIENDONLINE = 0
        GAMEINVITE = 1
        FRIENDREQUEST = 2
        GENERIC = 3
        MULTIPENDING = 4
        PERSONALMESSAGE = 5
        SIGNEDOUT = 6
        SIGNEDIN = 7
        SIGNEDINLIVE = 8
        SIGNEDINNEEDPASS = 9
        CHATREQUEST = 10
        CONNECTIONLOST = 11
        DOWNLOADCOMPLETE = 12
        SONGPLAYING = 13
        PREFERRED_REVIEW = 14
        AVOID_REVIEW = 15
        COMPLAINT = 16
        CHATCALLBACK = 17
        REMOVEDMU = 18
        REMOVEDGAMEPAD = 19
        CHATJOIN = 20
        CHATLEAVE = 21
        GAMEINVITESENT = 22
        CANCELPERSISTENT = 23
        CHATCALLBACKSENT = 24
        MULTIFRIENDONLINE = 25
        ONEFRIENDONLINE = 26
        ACHIEVEMENT = 27
        HYBRIDDISC = 28
        MAILBOX = 29
        VIDEOCHATINVITE = 30
        DOWNLOADCOMPLETEDREADYTOPLAY = 31
        CANNOTDOWNLOAD = 32
        DOWNLOADSTOPPED = 33
        CONSOLEMESSAGE = 34
        GAMEMESSAGE = 35
        DEVICEFULL = 36
        CHATMESSAGE1 = 38 
        MULTIACHIEVEMENTS = 39 
        NUDGE = 40 
        MESSENGERCONNECTIONLOST=41 
        MESSENGERSIGNINFAILED=43 
        MESSENGERCONVERSATIONMISSED=44 
        FAMILYTIMERREMAINING=45 
        CONNECTIONLOSTRECONNECT=46 
        EXCESSIVEPLAYTIME=47 
        PARTYJOINREQUEST=49 
        PARTYINVITESENT=50 
        PARTYGAMEINVITESENT=51 
        PARTYKICKED=52 
        PARTYDISCONNECTED=53 
        PARTYCANNOTCONNECT=56 
        PARTYSOMEONEJOINED=57 
        PARTYSOMEONELEFT=58 
        GAMERPICTUREUNLOCKED=59 
        AVATARAWARDUNLOCKED=60


    def __init__(self, host=None, port=730, timeout=5.0):
        self.conn = XBDMConnection(host=host, port=port, timeout=timeout)
        self._lock = threading.RLock()
        self._host = host
        self._flush_stub_addr = 0
        self._flush_stub_size = 0

    def __enter__(self):
        with self._lock:
            self.conn.connect()
            self._host = self.conn.host
            banner = self.conn.recv_line()
            parse_response_line(banner)
            return self

    def __exit__(self, exc_type, exc, tb):
        with self._lock:
            try:
                self.conn.send(b"bye\r\n")
            except Exception:
                pass
            self.conn.close()

    def close(self):
        with self._lock:
            try:
                self.conn.send(b"bye\r\n")
            except Exception:
                pass
            self.conn.close()

    def reconnect(self, host=None):
        with self._lock:
            self.conn.close()
            h = host or self._host or self.conn.host
            self._host = h
            self.conn = XBDMConnection(h, port=self.conn.port, timeout=self.conn.timeout)
            self.conn.connect()
            banner = self.conn.recv_line()
            parse_response_line(banner)
            return self

    def _ensure_connected(self):
        if self.conn.is_connected:
            return
        if not self._host:
            raise XBDMConnectionError("Connection lost and no host to reconnect to")
        self.conn.close()
        self.conn = XBDMConnection(
            self._host, port=self.conn.port, timeout=self.conn.timeout
        )
        self.conn.connect()
        banner = self.conn.recv_line()
        parse_response_line(banner)

    def call_float(self, address: int, args, system_thread: bool = True) -> float:
        result = self.call_function(address, args, return_type=self.FLOAT, system_thread=system_thread)
        return float(result)

    def call_function(self, address: int, args, return_type: any = INT, system_thread: bool = True):
        cmd = []
        cmd.append("consolefeatures ver=2")
        cmd.append(f"type={return_type}")
        if system_thread:
            cmd.append("system")

        params = []
        params.append(f"A\\{address:X}\\A\\{len(args)}\\")

        for arg in args:
            params.append(self.encode_argument(arg))

        param_str = "".join(params)
        full_cmd = " ".join(cmd) + f' as=0 params="{param_str}"'

        response = self.send_command(full_cmd)

        while "buf_addr=" in response.message:
            buf = response.message.split("buf_addr=")[1][:8]
            response = self.send_command(f"consolefeatures buf_addr=0x{buf}")

        return response.message.strip()

    def call_int32(self, address: int, args, system_thread: bool = True) -> int:
        result = self.call_function(address, args, return_type=self.INT, system_thread=system_thread)
        return int(result, 16)

    def call_int64(self, address: int, args, system_thread: bool = True) -> int:
        result = self.call_function(address, args, return_type=self.UINT64, system_thread=system_thread)  
        return int(result, 16)

    def call_string(self, address: int, args, system_thread: bool = True) -> str:
        result = self.call_function(address, args, return_type=self.STRING, system_thread=system_thread)
        return result

    def call_void(self, address: int, args=[None], system_thread: bool = True):
        self.call_function(address, args, return_type=self.VOID, system_thread=system_thread)

    def console_name(self) -> str:
        resp = self.send_command("dbgname")
        return resp.message.strip()

    def create_directory(self, remotePath: str) -> int:
        command = "mkdir name=\"" + remotePath + "\""
        response = self.send_command(command)
        return response.code


    def debug_go(self):
        self.send_command("go")


    def debug_stop(self):
        self.send_command("stop")


    def delete_file(self, remote_path: str, is_directory: bool = False):
        cmd = f'delete name="{remote_path}"' + (" dir" if is_directory else "")
        resp = self.send_command(cmd)
        if not resp.code == 200:
            raise RuntimeError(f"Couldn't delete {remote_path}")


    def encode_argument(self, arg):
        if isinstance(arg, int):
            return f"{self.INT}\\{arg}\\"

        if isinstance(arg, float):
            return f"{self.FLOAT}\\{arg}\\"

        if isinstance(arg, str):
            raw = arg.encode("ascii")
            hexstr = raw.hex().upper()
            return f"{self.BYTE_ARRAY}/{len(raw)}\\{hexstr}\\"

        if isinstance(arg, (bytes, bytearray)):
            hexstr = arg.hex().upper()
            return f"{self.BYTE_ARRAY}/{len(arg)}\\{hexstr}\\"

        raise TypeError(f"Unsupported argument type: {type(arg)}")


    def timet_to_filetime(self, unix_time: int) -> int:
        return (unix_time + 11644473600) * 10_000_000


    def get_console_id(self) -> int:
        cmd = f"getconsoleid"
        resp = self.send_command(cmd)
        if resp.message.startswith(" consoleid="):
            resp.message = resp.message[len(" consoleid="):]
        return int(resp.message.strip(), 16)


    def get_current_title_id(self) -> int:
        cmd = f"consolefeatures ver=2 type=16 params=\"A\\0\\A\\0\\\""
        resp = self.send_command(cmd)
        return int(resp.message.strip(), 16)


    def get_current_title_path(self) -> str:
        cmd = f"xbeinfo name="
        lines = self.send_multiline_command(cmd)
        if len(lines) < 3:
            raise RuntimeError("Unexpected response format")
        line = lines[2]
        if line.startswith("name="):
            line = line[len("name="):]
        return line.strip().strip('"')


    def get_cpukey(self) -> bytes:
        cmd = f"consolefeatures ver=2 type=10 params=\"A\\0\\A\\0\\\""; 
        resp = self.send_command(cmd)
        return bytes.fromhex(resp.message).hex().upper()

    def get_directory_contents(self, remote_path: str):
        cmd = f'dirlist name="{remote_path}"'
        lines = self.send_multiline_command(cmd)
        contents = []
        for line in lines:
            entry = {}
            m_name = re.search(r'name="([^"]+)"', line)
            if m_name:
                entry['name'] = m_name.group(1)

            m_sizehi = re.search(r'sizehi=0x([0-9a-fA-F]+)', line)
            m_sizelo = re.search(r'sizelo=0x([0-9a-fA-F]+)', line)
            if m_sizehi and m_sizelo:
                entry['size'] = (int(m_sizehi.group(1), 16) << 32) | int(m_sizelo.group(1), 16)

            m_createhi = re.search(r'createhi=0x([0-9a-fA-F]+)', line)
            m_createlo = re.search(r'createlo=0x([0-9a-fA-F]+)', line)
            if m_createhi and m_createlo:
                entry['created'] = (int(m_createhi.group(1), 16) << 32) | int(m_createlo.group(1), 16)

            m_changehi = re.search(r'changehi=0x([0-9a-fA-F]+)', line)
            m_changelo = re.search(r'changelo=0x([0-9a-fA-F]+)', line)
            if m_changehi and m_changelo:
                entry['changed'] = (int(m_changehi.group(1), 16) << 32) | int(m_changelo.group(1), 16)

            entry['is_directory'] = 'directory' in line

            contents.append(entry)
        return contents

    def get_dmversion(self) -> str:
        cmd = f"dmversion"
        resp = self.send_command(cmd)
        return resp.message.strip()


    def get_drive_list(self) -> list:
        cmd = "drivelist"
        lines = self.send_multiline_command(cmd)
        drives = []
        for line in lines:
            m = re.search(r'drivename="([^"]+)"', line)
            if m:
                drives.append(m.group(1))
        return drives

    def get_gamertag(self) -> str:
        profile_id_addr = 0x81AA28E8 if not self.is_devkit() else 0x81D44460 
        return self.read_wstring(profile_id_addr + 0x15, 0x20)

    def get_kernel_version(self) -> str:
        cmd = f"consolefeatures ver=2 type=13 params=\"A\\0\\A\\0\\\""
        resp = self.send_command(cmd)
        return resp.message.strip()

    def get_modules(self) -> list:
        cmd = f"modules"
        lines = self.send_multiline_command(cmd)
        return lines

    def get_module_handle(self, module_name: str) -> int:
        try:
            address = self.resolve_function("xam.xex", 1102)
            handle = self.call_int(address, [module_name])
            if handle == 0:
                raise RuntimeError(f"Couldn't get the module handle for {module_name}")
            return handle
        except Exception as e:
            raise RuntimeError(f"Error getting module handle for {module_name}: {e}")


    def get_motherboard_type(self) -> str:
        cmd = f"consolefeatures ver=2 type=17 params=\"A\\0\\A\\0\\\""; 
        resp = self.send_command(cmd)
        return resp.message.strip()

    def get_process_id(self) -> int:
        cmd = f"getpid"
        resp = self.send_command(cmd)
        if resp.code != 200:
            raise RuntimeError(f"Failed to get process ID: {resp.code} {resp.message}")
        if resp.message.startswith(" pid="):
            resp.message = resp.message[len(" pid="):]
        return int(resp.message.strip(), 16)


    def get_signin_state(self, user_index: int) -> SignInState:
        try:
            address = self.resolve_function("xam.xex", 528)
            state_value = self.call_int32(address, [user_index])
            return self.SignInState(state_value)
        except Exception as e:
            raise RuntimeError(f"Error getting signin state for user index {user_index}: {e}")

    def is_devkit(self) -> bool:
        value = self.read_memory(0x8E038610, 1)
        if value == b'\x02':
            return True
        elif value == b'\x03':
            return False
        else:
            raise RuntimeError(f"Unexpected value when checking for devkit: {value.hex()}")

    def is_directory(self, path: str) -> bool:
        command = "dirlist name=\"" + path + "\""
        response = self.send_command(command)
        if response.code == 202:
            return True
        elif response.code == 410:
            return False
        else:
            raise RuntimeError(f"Unexpected response code: {response.code}")

    def launch_xex(self, name: str, path: str):
        cmd = "magicboot title=\"" + path + "\" directory=\"" + name + "\""
        resp = self.send_command(cmd)
        if resp.code != 200:
            raise RuntimeError(f"Failed to launch XEX: {resp.code} {resp.message}")

    def load_module(self, module_path: str) -> int:
        addr = self.resolve_function("xboxkrnl.exe", 409)
        result = self.call_int32(addr, [module_path, 8, 0, 0])
        return result

    def parse_return(response, return_type):
        try:
            _, value = response.split(" ", 1)
            value = value.strip()
        except ValueError:
            raise RuntimeError("Malformed response")

        if return_type is int:
            return int(value, 16)

        if return_type is float:
            return float(value)

        if return_type is str:
            return value

        if return_type is type(None):
            return None

        raise TypeError("Unsupported return type")


    def read_memory(self, address: int, size: int) -> bytes:
        with self._lock:
            self._ensure_connected()
            resp = read_memory_text(self.conn, address, size)
            return resp


    def read_memory_binary(self, address: int, size: int) -> bytes:
        with self._lock:
            self._ensure_connected()
            return read_memory_binary(self.conn, address, size)


    def read_float(self, address: int) -> float:
        with self._lock:
            self._ensure_connected()
            raw = read_memory_text(self.conn, address, 4)
            raw = raw[::-1]
            return struct.unpack('<f', raw)[0]


    def read_double(self, address: int) -> float:
        with self._lock:
            self._ensure_connected()
            raw = read_memory_text(self.conn, address, 8)
            raw = raw[::-1]
            return struct.unpack('<d', raw)[0]


    def read_u16(self, address: int) -> int:
        with self._lock:
            self._ensure_connected()
            raw = read_memory_text(self.conn, address, 2)
            return int.from_bytes(raw, "big")


    def read_u32(self, address: int) -> int:
        with self._lock:
            self._ensure_connected()
            raw = read_memory_text(self.conn, address, 4)
            return int.from_bytes(raw, "big")


    def read_u64(self, address: int) -> int:
        with self._lock:
            self._ensure_connected()
            raw = read_memory_text(self.conn, address, 8)
            return int.from_bytes(raw, "big")


    def read_cstring(self, address: int, max_length: int = 256) -> str:
        raw = self.read_memory(address, max_length)
        string = raw.split(b'\x00', 1)[0]
        return string.decode('utf-8')


    def read_wstring(self, address: int, max_length: int = 256) -> str:
        raw = self.read_memory(address, max_length * 2)
        if len(raw) % 2 != 0:
            raw += b'\x00'
        end = raw.find(b'\x00\x00')
        if end != -1:
            raw = raw[:end+2]
        decoded = raw.decode('utf-16le', errors='ignore')
        return decoded.rstrip('\x00').strip()


    def reboot_console(self):
        command = "magicboot cold"
        self.send_command(command)


    def receive_directory(self, remote_path: str, local_path: str):
        import os
        files = self.get_directory_contents(remote_path)

        if not os.path.exists(local_path):
            os.makedirs(local_path)

        for file in files:
            next_directory = os.path.join(local_path, file['name'])

            if file['is_directory']:
                self.receive_directory(f"{remote_path}\\{file['name']}", next_directory)
            else:
                self.receive_file(f"{remote_path}\\{file['name']}", next_directory)


    def receive_file(self, remote_path: str, local_path: str):
        with self._lock:
            self._ensure_connected()
            with open(local_path, 'wb') as file:
                cmd = f'getfile name="{remote_path}"\r\n'
                self.conn.send(cmd.encode())

                header = self.conn.recv_line() 
                if not header.startswith(b'203- binary response follows'):
                    raise RuntimeError("Couldn't receive the file")

                file_size_bytes = self.conn.recv_exact(4)
                file_size = int.from_bytes(file_size_bytes, 'little')

                bytes_received = 0
                while bytes_received < file_size:
                    chunk = self.conn.recv_exact(min(4096, file_size - bytes_received))
                    if not chunk:
                        break
                    file.write(chunk)
                    bytes_received += len(chunk)


    def resolve_function(self, module_name: str, ordinal: int) -> int:
        hex_module = ''.join(f"{ord(c):02X}" for c in module_name)

        cmd = f'consolefeatures ver=2 type=9 params="A\\0\\A\\2\\{self.BYTE_ARRAY}/{len(hex_module)//2}\\{hex_module}\\{self.INT}\\{ordinal}\\"'
        resp = self.send_command(cmd)
        return int(resp.message.strip(), 16)

    def rename_file(self, old_remote_path: str, new_remote_path: str):
        cmd = f'rename name="{old_remote_path}" newname="{new_remote_path}"'
        resp = self.send_command(cmd)
        if resp.code != 200:
            raise RuntimeError(f"Couldn't rename {old_remote_path} to {new_remote_path}")


    def parse_hex_field(self, text: str, key: str) -> int:
        m = re.search(rf"{key}=0x([0-9a-fA-F]+)", text)
        if not m:
            raise ValueError(f"Missing field: {key}")
        return int(m.group(1), 16)


    def _parse_hex_field_optional(self, text: str, key: str, default: int = 0) -> int:
        m = re.search(rf"{key}=0x([0-9a-fA-F]+)", text)
        if not m:
            return default
        return int(m.group(1), 16)

    def _xg_address_2d_tiled_offset(self, x: int, y: int, width: int, bytes_per_pixel: int) -> int:
        aligned_width = (width + 31) & ~31
        log_bpp = {1: 0, 2: 1, 4: 2, 8: 3, 16: 4}[bytes_per_pixel]

        macro_tiles_per_row = aligned_width >> 5
        outer_blocks = ((y >> 5) * macro_tiles_per_row + (x >> 5)) << 6

        inner_blocks = (((y >> 1) & 0b111) << 3) | (x & 0b111)

        outer_inner_bytes = (outer_blocks | inner_blocks) << log_bpp

        bank = (y >> 4) & 1
        pipe = ((x >> 3) & 0b11) ^ (((y >> 3) & 1) << 1)
        y_lsb = y & 1

        return (
            (outer_inner_bytes & 0xF) |
            (y_lsb << 4) |
            (((outer_inner_bytes >> 4) & 1) << 5) |
            (pipe << 6) |
            (((outer_inner_bytes >> 5) & 0b111) << 8) |
            (bank << 11) |
            ((outer_inner_bytes >> 8) << 12)
        )

    def _morton2d(self, x: int, y: int) -> int:
        def part1by1(n: int) -> int:
            n &= 0x0000FFFF
            n = (n | (n << 8)) & 0x00FF00FF
            n = (n | (n << 4)) & 0x0F0F0F0F
            n = (n | (n << 2)) & 0x33333333
            n = (n | (n << 1)) & 0x55555555
            return n

        return part1by1(x) | (part1by1(y) << 1)

    def _xg_address_2d_morton_offset(self, x: int, y: int, width: int, bytes_per_pixel: int) -> int:
        aligned_width = (width + 31) & ~31
        macro_tile_pitch = aligned_width >> 5

        macro_x = x >> 5
        macro_y = y >> 5
        macro_tile_index = macro_x + macro_y * macro_tile_pitch
        macro_tile_offset = macro_tile_index * (32 * 32 * bytes_per_pixel)

        local_x = x & 31
        local_y = y & 31
        morton = self._morton2d(local_x, local_y)

        return macro_tile_offset + morton * bytes_per_pixel

    def _infer_tiled_dimensions(self, width: int, height: int, fbsize: int, bytes_per_pixel: int):
        min_w = (width + 31) & ~31
        min_h = (height + 31) & ~31
        pixels = fbsize // bytes_per_pixel

        for w in range(min_w, min_w + 32 * 64, 32):
            if w <= 0:
                continue
            if pixels % w != 0:
                continue
            h = pixels // w
            if h >= min_h and h % 32 == 0:
                return w, h

        return min_w, min_h

    def untile_xenos_rgba(self, buffer, width, height, fbsize):
        bytes_per_pixel = 4
        log_bpp = 2
        aligned_width = (width + 31) & ~31
        macro_tiles_per_row = aligned_width >> 5

        yy, xx = np.mgrid[0:height, 0:width].astype(np.int64)

        outer_blocks = ((yy >> 5) * macro_tiles_per_row + (xx >> 5)) << 6
        inner_blocks = (((yy >> 1) & 0b111) << 3) | (xx & 0b111)
        outer_inner_bytes = (outer_blocks | inner_blocks) << log_bpp

        bank = (yy >> 4) & 1
        pipe = ((xx >> 3) & 0b11) ^ (((yy >> 3) & 1) << 1)
        y_lsb = yy & 1

        src = (
            (outer_inner_bytes & 0xF)
            | (y_lsb << 4)
            | (((outer_inner_bytes >> 4) & 1) << 5)
            | (pipe << 6)
            | (((outer_inner_bytes >> 5) & 0b111) << 8)
            | (bank << 11)
            | ((outer_inner_bytes >> 8) << 12)
        ).ravel()

        fb = np.frombuffer(buffer, dtype=np.uint8)
        max_src = len(fb) - bytes_per_pixel

        valid = src <= max_src
        safe_src = np.where(valid, src, 0)

        fb_u32 = np.frombuffer(buffer, dtype=np.uint32)
        src_u32 = (safe_src >> 2).astype(np.intp)
        out_u32 = np.where(valid, fb_u32[src_u32], 0).astype(np.uint32)

        return out_u32.view(np.uint8).tobytes()

    def untile_xenos_rgba_morton(self, buffer, width, height, fbsize):
        bytes_per_pixel = 4
        aligned_width, aligned_height = self._infer_tiled_dimensions(
            width,
            height,
            fbsize,
            bytes_per_pixel,
        )
        out = bytearray(width * height * bytes_per_pixel)

        expected_size = aligned_width * aligned_height * bytes_per_pixel
        max_size = min(len(buffer), expected_size)

        for y in range(height):
            for x in range(width):
                src = self._xg_address_2d_morton_offset(x, y, aligned_width, bytes_per_pixel)
                dst = (y * width + x) * bytes_per_pixel
                if src + bytes_per_pixel <= max_size:
                    out[dst:dst + bytes_per_pixel] = buffer[src:src + bytes_per_pixel]

        return out


    def screenshot_name(self) -> str:
        timestamp = time.strftime("%Y-%m-%d-%H-%M-%S-%MS", time.localtime())
        return f"{timestamp}.png"


    def screenshot(self, output_path: Optional[str] = None, rawmode: str = "BGRA", untile_mode: str = "xg") -> str:
        with self._lock:
            self._ensure_connected()
            self.conn.send(b"screenshot\r\n")
            line = self.conn.recv_line()
            resp = parse_response_line(line)

            if resp.code != 203:
                raise RuntimeError("Screenshot failed")

            meta = self.conn.recv_line().decode("ascii", errors="ignore")
            pitch = self.parse_hex_field(meta, "pitch")
            width = self.parse_hex_field(meta, "width")
            height = self.parse_hex_field(meta, "height")
            fbsize = self.parse_hex_field(meta, "framebuffersize")
            sw = self._parse_hex_field_optional(meta, "sw", width)
            sh = self._parse_hex_field_optional(meta, "sh", height)
            off_x = self._parse_hex_field_optional(meta, "offsetx", 0)
            off_y = self._parse_hex_field_optional(meta, "offsety", 0)

            fb = self.conn.recv_exact(fbsize)
            if len(fb) != fbsize:
                raise RuntimeError("Framebuffer size mismatch")

        pitch_pixels = pitch // 4

        if untile_mode == "xg":
            linear = self.untile_xenos_rgba(fb, pitch_pixels, height, fbsize)
        elif untile_mode == "morton":
            linear = self.untile_xenos_rgba_morton(fb, pitch_pixels, height, fbsize)
        else:
            raise ValueError(f"Unknown untile_mode: {untile_mode}")

        img = Image.frombuffer(
            "RGBA",
            (pitch_pixels, height),
            bytes(linear),
            "raw",
            rawmode,
            0,
            1,
        ).convert("RGB")

        if pitch_pixels > width:
            img = img.crop((0, 0, width, height))

        if off_x != 0 or off_y != 0:
            canvas = Image.new("RGB", (sw, sh), (0, 0, 0))
            canvas.paste(img, (off_x, off_y))
            img = canvas

        if (sw, sh) != (width, height):
            img = img.resize((sw, sh), Image.NEAREST)

        if output_path is None:
            pictures_dir = os.path.join(os.path.expanduser("~"), "Pictures")
            if os.path.isdir(pictures_dir):
                output_path = os.path.join(pictures_dir, "Solace 360 Screenshots")
            else:
                output_path = os.path.join(tempfile.gettempdir(), "Solace 360 Screenshots")

        os.makedirs(output_path, exist_ok=True)
        file_path = os.path.join(output_path, self.screenshot_name())
        if os.name == "posix" and "ANDROID_ROOT" in os.environ:
            file_path = os.path.join(os.getenv('EXTERNAL_STORAGE'), "/storage/emulated/0", 'Pictures', 'Solace 360 Screenshots', self.screenshot_name())
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

        img.save(file_path, format="PNG")
        return file_path


    def send_command(self, command: str):
        with self._lock:
            self._ensure_connected()
            self.conn.send(command.encode() + b"\r\n")
            line = self.conn.recv_line()
            return parse_response_line(line)


    def send_binary(self, data: bytes):
        with self._lock:
            self._ensure_connected()
            self.conn.send(data)


    def send_multiline_command(self, command: str) -> list:
        with self._lock:
            self._ensure_connected()
            lines = []
            self.conn.send(command.encode() + b"\r\n")
            line = self.conn.recv_line()
            response = parse_response_line(line)

            if response.code == 202:
                total_bytes = 0
                while True:
                    line = self.conn.recv_line()
                    total_bytes += len(line)
                    if (
                        len(lines) >= self.MAX_MULTILINE_RESPONSE_LINES
                        or total_bytes > self.MAX_MULTILINE_RESPONSE_BYTES
                    ):
                        self.conn.close()
                        raise XBDMConnectionError("XBDM multiline response exceeds size limit")
                    decoded_line = line.decode("ascii").strip()
                    if decoded_line == ".":
                        break
                    lines.append(decoded_line)

            return lines


    def set_system_time(self, unix_time: int):
        filetime = self.timet_to_filetime(unix_time)

        clock_hi = (filetime >> 32) & 0xFFFFFFFF
        clock_lo = filetime & 0xFFFFFFFF

        command = (
            f"setsystime "
            f"clockhi=0x{clock_hi:08X} "
            f"clocklo=0x{clock_lo:08X}"
        )

        response = self.send_command(command)
        if response.code != 200:
            raise Exception(f"Failed to set system time with error code {response.code}\nMessage: {response.message}")


    def send_directory(self, local_path: str, remote_path: str, overwrite: bool = False):
        for entry in os.listdir(local_path):
            local_entry_path = os.path.join(local_path, entry)
            remote_entry_path = f"{remote_path}\\{entry}"

            if os.path.isdir(local_entry_path):
                try:
                    self.create_directory(remote_entry_path)
                except Exception:
                    pass
                self.send_directory(local_entry_path, remote_entry_path, overwrite=overwrite)
            else:
                self.send_file(local_entry_path, remote_entry_path, overwrite=overwrite)


    def send_file(self, local_path: str, remote_path: str, overwrite: bool = False):
        with self._lock:
            self._ensure_connected()
            with open(local_path, 'rb') as file:
                file.seek(0, 2)
                file_size = file.tell()
                file.seek(0, 0)

                cmd = f'sendfile name="{remote_path}" length=0x{file_size:X}\r\n'
                self.conn.send(cmd.encode())

                header = self.conn.recv_line()
                if not header.startswith(b'204- send binary data'):
                    if header.startswith(b'410-'):
                        if overwrite:
                            self.delete_file(remote_path)
                            file.seek(0, 0)
                            cmd = f'sendfile name="{remote_path}" length=0x{file_size:X}\r\n'
                            self.conn.send(cmd.encode())
                            header = self.conn.recv_line()
                            if not header.startswith(b'204- send binary data'):
                                raise RuntimeError("Couldn't send the file after overwrite")
                        else:
                            raise XBDMFileAlreadyExistsError(remote_path)
                    else:
                        raise RuntimeError("Couldn't send the file")

                while True:
                    chunk = file.read(4096)
                    if not chunk:
                        break
                    self.conn.send(chunk)

                final_response = self.conn.recv_line()
                if not final_response.startswith(b'200- OK'):
                    raise RuntimeError("File transfer failed")

    def shutdown_console(self):
        command = "consolefeatures ver=2 type=11 params=\"A\\0\\A\\0\\\""
        response = self.send_command(command)
        if response.code != 200:
            raise Exception(f"Failed to shutdown console with error code {response.code}\nMessage: {response.message}")


    def synchronize_time(self):
        now = int(time.time())
        self.set_system_time(now)

    def unload_module(self, module: str):
        address = self.resolve_function("xam.xex", 1102)
        handle = self.call_int32(address, [module])
        if handle != 0:
            self.write_u16(handle + 0x40, 1)
            addr_unload = self.resolve_function("xboxkrnl.exe", 417)
            self.call_int32(addr_unload, [handle])

    def write_boolean(self, address: int, value: bool):
        with self._lock:
            self._ensure_connected()
            byte_value = 1 if value else 0
            write_memory(self.conn, address, byte_value.to_bytes(1, 'big'))

    def write_double(self, address: int, value: float):
        with self._lock:
            self._ensure_connected()
            data = struct.pack('<d', value)
            data = data[::-1]
            write_memory(self.conn, address, data)

    def write_float(self, address: int, value: float):
        with self._lock:
            self._ensure_connected()
            data = struct.pack('<f', value)
            data = data[::-1]
            write_memory(self.conn, address, data)

    def write_float_vector2(self, address: int, x: float, y: float):
        with self._lock:
            self._ensure_connected()
            data = struct.pack('<ff', x, y)
            data = data[::-1]
            write_memory(self.conn, address, data)

    def write_float_vector3(self, address: int, x: float, y: float, z: float):
        with self._lock:
            self._ensure_connected()
            data = struct.pack('<fff', x, y, z)
            data = data[::-1]
            write_memory(self.conn, address, data)

    def write_branch(self, address: int, destination: int, linked: bool = False) -> int:
        offset = destination - address
        if offset < -0x2000000 or offset > 0x1FFFFFC:
            raise ValueError(
                f"Target 0x{destination:08X} is out of range for relative branch "
                f"from 0x{address:08X} (offset 0x{offset:08X}, max ±32MB)"
            )
        opcode = 18
        lk = 1 if linked else 0
        instr = (opcode << 26) | (offset & 0x03FFFFFC) | lk
        self.write_memory(address, instr.to_bytes(4, 'big'))
        return 4

    def patch_in_jump(self, address: int, destination: int, linked: bool = False, scratch_reg: int = 11) -> int:
        r = scratch_reg & 0x1F
        lis_base  = (15 << 26) | (r << 21)
        addi_base = (14 << 26) | (r << 21) | (r << 16)
        mtctr     = (31 << 26) | (r << 21) | (9 << 16) | (467 << 1)

        func = [0, 0, 0, 0]
        hi = (destination >> 16) & 0xFFFF
        lo = destination & 0xFFFF
        if lo & 0x8000:
            hi = (hi + 1) & 0xFFFF
        func[0] = lis_base + hi
        func[1] = addi_base + lo
        func[2] = mtctr
        func[3] = 0x4E800420
        if linked:
            func[3] += 1

        buffer = bytearray(16)
        for i in range(4):
            part = func[i].to_bytes(4, 'big')
            buffer[i*4:(i+1)*4] = part

        self.write_memory(address, buffer)
        return 16

    def write_memory(self, address: int, data: bytes):
        with self._lock:
            self._ensure_connected()
            write_memory(self.conn, address, data)

    def write_string(self, address: int, string: str):
        with self._lock:
            self._ensure_connected()
            data = string.encode('utf-8')
            write_memory(self.conn, address, data)

    def write_u8(self, address: int, value: int):
        with self._lock:
            self._ensure_connected()
            write_memory(self.conn, address, value.to_bytes(1, 'big'))

    def write_u16(self, address: int, value: int):
        with self._lock:
            self._ensure_connected()
            write_memory(self.conn, address, value.to_bytes(2, 'big'))

    def write_u32(self, address: int, value: int) -> None:
        with self._lock:
            self._ensure_connected()
            write_memory(self.conn, address, value.to_bytes(4, 'big'))

    def write_u64(self, address: int, value: int) -> None:
        with self._lock:
            self._ensure_connected()
            write_memory(self.conn, address, value.to_bytes(8, 'big'))

    def write_u16_array(self, address: int, values: list):
        with self._lock:
            self._ensure_connected()
            data = bytearray()
            for value in values:
                data.extend(value.to_bytes(2, 'big'))
            write_memory(self.conn, address, data)

    def write_u32_array(self, address: int, values: list):
        with self._lock:
            self._ensure_connected()
            data = bytearray()
            for value in values:
                data.extend(value.to_bytes(4, 'big'))
            write_memory(self.conn, address, data)

    def write_u64_array(self, address: int, values: list):
        with self._lock:
            self._ensure_connected()
            data = bytearray()
            for value in values:
                data.extend(value.to_bytes(8, 'big'))
            write_memory(self.conn, address, data)

    def write_wstring(self, address: int, string: str):
        with self._lock:
            self._ensure_connected()
            data = string.encode('utf-16le')
            write_memory(self.conn, address, data)

    def xnotify(self, text: str, type: NotifyType = NotifyType.CONSOLEMESSAGE) -> None:
        cmd = f'consolefeatures ver=2 type=12 params="A\\0\\A\\2\\{self.BYTE_ARRAY}/{len(text)}\\{text.encode("ascii").hex().upper()}\\{self.INT}\\{type}\\"'
        self.send_command(cmd)

    def zero_memory(self, address: int, size: int):
        with self._lock:
            self._ensure_connected()
            zero_data = bytes(size)
            write_memory(self.conn, address, zero_data)
