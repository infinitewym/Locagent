"""会话记录：一个会话一个 JSONL 文件，同时用于复盘和恢复。

参考 Claude Code 的做法：<工作区>/.locagent/sessions/<session_id>.jsonl，一行一个事件，逐条追加。
文件里有两类内容：
- history_recorded 事件：每往对话历史追加一条记录，就写一行，内容完整
- 其他事件：session_started、run_started、model_responded、tool_finished、context_reduced 等，
  记录过程和用量，每个事件带 run_id，可以按提问区分

恢复会话（--resume）时按顺序重放：history_recorded 依次追加，context_reduced 重做同样的清理或压缩，
得到和退出前一样的历史；最后一个 checkpoint 事件就是最近一个任务的进度。不需要另存一份快照。
"""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import events as ev
from .context_manager import clear_tool_record, input_tokens


def new_session_id():
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]


class SessionStore:
    def __init__(self, root):
        # root 是 <工作区>/.locagent/sessions
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # 在 .locagent 下放一个只含 * 的 .gitignore，会话记录不会出现在用户仓库的 git status 里
        gitignore = self.root.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")

    def path(self, session_id):
        return self.root / f"{session_id}.jsonl"

    def append(self, session_id, event):
        # 逐条追加：运行中途被中断或程序崩溃，已经发生的事件也都保存下来了
        with self.path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False))
            handle.write("\n")

    def latest(self):
        # 最近修改过的会话，即最后一次使用的会话
        files = sorted(self.root.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None

    def load(self, session_id):
        path = self.path(session_id)
        if not path.exists():
            raise RuntimeError(f"session not found: {session_id}")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def replay(events):
    """按顺序重放事件，还原对话历史、最近一次请求的输入 token 数和最后一个任务检查点。"""
    history = []
    last_input_tokens = 0
    checkpoint = None
    for event in events:
        kind = event["event"]
        if kind == ev.HISTORY_RECORDED:
            history.append(event["record"])
        elif kind == ev.CONTEXT_REDUCED:
            if event["action"] == "compact":
                history = [{"role": "summary", "content": event["summary"]}]
                last_input_tokens = 0
            else:
                for index in event["cleared_indices"]:
                    clear_tool_record(history[index])
        elif kind == ev.MODEL_RESPONDED:
            last_input_tokens = input_tokens(event["usage"])
        elif kind == ev.CHECKPOINT:
            checkpoint = event["checkpoint"]
    return history, last_input_tokens, checkpoint
