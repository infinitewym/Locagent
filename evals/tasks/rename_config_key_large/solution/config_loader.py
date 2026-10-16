"""读取 config.json，并检查必需的配置项。"""

import json
from pathlib import Path

REQUIRED_KEYS = ("service", "max_connections", "max_conn_retry", "timeout")


def load_config(path="config.json"):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_KEYS if key not in config]
    if missing:
        raise KeyError(f"missing config keys: {missing}")
    return config
