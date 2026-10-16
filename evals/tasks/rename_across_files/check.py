"""判分：所有 .py 文件里不再出现 calc_total，新名字可以导入，main.py 输出不变。"""

import subprocess
import sys
from pathlib import Path

for path in Path(".").rglob("*.py"):
    if ".locagent" in path.parts:
        continue
    assert "calc_total" not in path.read_text(encoding="utf-8"), f"calc_total still in {path}"

import pricing

assert hasattr(pricing, "compute_total"), "pricing.compute_total missing"
result = subprocess.run([sys.executable, "main.py"], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[-800:]
output = result.stdout.replace("\r\n", "\n")
assert output == "28.05\na: 9.0\nb: 10.0\n", f"main.py output changed: {output!r}"
print("ok")
