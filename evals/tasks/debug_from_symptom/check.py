"""判分：数据文件没改；report.py 输出正确总价；隐藏用例检查两个函数本身（防止只为这份数据凑数）。"""

import os
import subprocess
import sys
from pathlib import Path

original = Path(os.environ["LOCAGENT_TASK_DIR"]) / "repo" / "data" / "items.csv"
assert Path("data/items.csv").read_bytes() == original.read_bytes(), "data/items.csv was modified"

result = subprocess.run([sys.executable, "report.py"], capture_output=True, text=True, encoding="utf-8")
assert result.returncode == 0, result.stderr[-800:]
assert "2537.00" in result.stdout, f"wrong report: {result.stdout!r}"

from inventory.parser import parse_price
from inventory.summary import total_value

assert parse_price("3.5") == 3.5, "parse_price('3.5')"
assert parse_price("2,000") == 2000, "parse_price('2,000')"
assert parse_price("1,234,567.89") == 1234567.89, "parse_price('1,234,567.89')"
items = [{"name": "a", "price": 2.5, "qty": 2}, {"name": "b", "price": 1, "qty": 3}]
assert total_value(items) == 8, "total_value should include every item"
assert total_value([]) == 0, "total_value([])"
print("ok")
