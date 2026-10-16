"""判分：隐藏用例检查优先级、排序、旧数据兼容；旧测试不能删，补了新测试且全部通过。"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from todo.store import TodoStore

with tempfile.TemporaryDirectory() as temp:
    path = Path(temp) / "todo.json"
    store = TodoStore(path)
    assert store.add("x")["priority"] == "medium", "default priority should be medium"
    store.add("y", "low")
    store.add("z", "high")
    store.add("w", "high")
    store.done(1)
    assert [item["title"] for item in store.pending()] == ["z", "w", "y"], [item["title"] for item in store.pending()]
    for bad in ("urgent", "HIGH", ""):
        try:
            store.add("bad", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"priority {bad!r} should raise ValueError")
    reloaded = TodoStore(path)
    assert [item.get("priority") for item in reloaded.items][:4] == ["medium", "low", "high", "high"], "priority not saved"

    old = Path(temp) / "old.json"
    old.write_text(json.dumps([
        {"id": 1, "title": "old-low", "done": False, "priority": "low"},
        {"id": 2, "title": "legacy", "done": False},
        {"id": 3, "title": "old-high", "done": False, "priority": "high"},
    ]), encoding="utf-8")
    assert [item["title"] for item in TodoStore(old).pending()] == ["old-high", "legacy", "old-low"], "legacy records should count as medium"

text = Path("tests/test_store.py").read_text(encoding="utf-8")
for name in ("test_add_and_reload", "test_done", "test_done_unknown_id"):
    assert f"def {name}" in text, f"original test {name} was removed"
assert len(re.findall(r"def test_", text)) >= 5, "new tests were not added"
result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[-800:]
print("ok")
