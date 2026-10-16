"""判分：max_conn 全部改名（单词边界匹配，不误伤 max_conn_retry）；配置值不变；测试和 main.py 正常。"""

import json
import re
import subprocess
import sys
from pathlib import Path

for path in Path(".").rglob("*"):
    if not path.is_file() or ".locagent" in path.parts or "__pycache__" in path.parts:
        continue
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\bmax_conn\b", text), f"max_conn still in {path}"

config = json.loads(Path("config.json").read_text(encoding="utf-8"))
assert config.get("max_connections") == 8, "config.json max_connections should be 8"
assert config.get("max_conn_retry") == 3, "max_conn_retry must stay unchanged"
assert "max_connections_retry" not in Path("config_loader.py").read_text(encoding="utf-8"), "max_conn_retry was renamed by mistake"

result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[-800:]
result = subprocess.run([sys.executable, "main.py"], capture_output=True, text=True)
assert result.returncode == 0, result.stderr[-800:]
assert "total: 359, retry: 3" in result.stdout, f"main.py output changed: {result.stdout[-200:]!r}"
print("ok")
