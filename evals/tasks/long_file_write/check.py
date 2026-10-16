"""判分：squares.py 是字面量写出的 800 行数据，内容正确。"""

from pathlib import Path

text = Path("squares.py").read_text(encoding="utf-8")
for word in ("range(", "for ", "while "):
    assert word not in text, f"squares.py is generated with {word.strip()}"

from squares import ROWS

assert len(ROWS) == 800, f"expected 800 rows, got {len(ROWS)}"
for index, row in enumerate(ROWS, start=1):
    assert row == {"n": index, "square": index * index}, f"row {index} is wrong: {row}"
print("ok")
