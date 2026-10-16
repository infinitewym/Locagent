"""判分：用隐藏用例检查三个函数，agent 写的测试能跑通，说明文档存在并带调用示例。"""

import subprocess
import sys
from pathlib import Path

from expense import add_record, by_name, total

records = []
assert add_record(records, "lunch", 20) is records, "add_record should return the same list"
add_record(records, "taxi", 15.5)
add_record(records, "lunch", 10)
assert len(records) == 3, "add_record should append"
assert total(records) == 45.5, "total"
assert by_name(records, "lunch") == 30, "by_name"
assert by_name(records, "none") == 0, "by_name with unknown name"
for bad in (0, -1):
    try:
        add_record([], "x", bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"amount {bad} should raise ValueError")

assert Path("test_expense.py").is_file(), "test_expense.py missing"
result = subprocess.run([sys.executable, "-m", "unittest", "test_expense"], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[-800:]
assert "Ran 0 tests" not in result.stderr, "test_expense.py has no tests"

usage = Path("expense_usage.md")
assert usage.is_file(), "expense_usage.md missing"
assert "add_record" in usage.read_text(encoding="utf-8"), "expense_usage.md has no add_record example"
print("ok")
