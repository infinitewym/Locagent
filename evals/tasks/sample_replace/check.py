"""判分：只有单独一行的 beta 被改掉（读文件时统一换行符，不考核 CRLF）。"""

from pathlib import Path

text = Path("sample.txt").read_text(encoding="utf-8")
expected = "alpha\nbeta-locked\ngamma\ndelta\nalphabet beta-test\n"
assert text == expected, f"unexpected content: {text!r}"
print("ok")
