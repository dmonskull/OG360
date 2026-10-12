from .exceptions import XBDMCommandError


class XBDMResponse:
    def __init__(self, code, message):
        self.code = code
        self.message = message

    @property
    def ok(self):
        return 200 <= self.code < 300


def parse_response_line(line: bytes, overide: bool = False) -> XBDMResponse:
    if line.endswith(b"\r\n"):
        record = line[:-2]
    elif line.endswith(b"\n"):
        record = line[:-1]
    else:
        record = line
    try:
        text = record.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("XBDM response is not ASCII") from exc
    if len(text) < 4 or not text[:3].isdigit() or text[3] != "-":
        raise ValueError("malformed XBDM response status line")

    code = int(text[:3])
    message = text[4:]

    if code >= 400 and not overide:
        raise XBDMCommandError.from_code(code, message)

    return XBDMResponse(code, message)
