"""判分：端口改了，其他内容不变；文件开头保留且只保留一个 BOM。"""

from pathlib import Path

BOM = b"\xef\xbb\xbf"
data = Path("settings.ini").read_bytes()
assert data.startswith(BOM), "BOM was removed"
assert not data[len(BOM):].startswith(BOM), "BOM was duplicated"
text = data[len(BOM):].decode("utf-8").replace("\r\n", "\n")
expected = "[server]\nhost = 127.0.0.1\nport = 8080\n\n[log]\nlevel = info\nport_note = 日志端口不变\n"
assert text == expected, f"unexpected content: {text!r}"
print("ok")
