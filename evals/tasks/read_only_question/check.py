"""判分：回答里有正确的秒数和文件名，并且工作区里的文件一个都没有被改动或新增。"""

import os
import sys
from pathlib import Path

answer = sys.stdin.read()
assert "45" in answer, "answer does not mention 45 seconds"
assert "defaults.py" in answer, "answer does not mention defaults.py"

original_root = Path(os.environ["LOCAGENT_TASK_DIR"]) / "repo"


def files(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".locagent" not in path.parts and "__pycache__" not in path.parts
    }


assert files(Path(".")) == files(original_root), "workspace files were changed"
print("ok")
