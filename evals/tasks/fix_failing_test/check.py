"""判分：测试文件没被改动，并且隐藏的额外用例也通过（防止只针对已有测试打补丁）。"""

import os
import subprocess
import sys
from pathlib import Path

original = Path(os.environ["LOCAGENT_TASK_DIR"]) / "repo" / "test_stats.py"
assert Path("test_stats.py").read_text(encoding="utf-8") == original.read_text(encoding="utf-8"), "test_stats.py was modified"

from stats import mean, median

assert mean([4]) == 4, "mean([4])"
assert mean([1, 2]) == 1.5, "mean([1, 2])"
assert mean([]) == 0, "mean([])"
assert median([5]) == 5, "median([5])"
assert median([4, 1, 3, 2]) == 2.5, "median([4, 1, 3, 2])"
assert median([10, 1, 7, 3, 5, 9]) == 6, "median([10, 1, 7, 3, 5, 9])"

result = subprocess.run([sys.executable, "-m", "unittest", "test_stats"], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[-800:]
print("ok")
