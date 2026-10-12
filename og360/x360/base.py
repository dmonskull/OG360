import re


def running_title(client):
    try:
        for line in client.send_multiline_command("xbeinfo name="):
            m = re.search(r'name="([^"]+)"', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""
