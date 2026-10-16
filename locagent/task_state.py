"""一次提问（run）的运行状态。

只在内存里计数：请求了几次模型、执行了几次工具、累计用了多少 token、最后为什么停下。
不单独写文件，结束时作为 run_finished 事件的内容写进 trace。
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"

STOP_REASON_FINAL_ANSWER = "final_answer_returned"
STOP_REASON_STEP_LIMIT = "step_limit_reached"
STOP_REASON_RETRY_LIMIT = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_INTERRUPTED = "interrupted"

# 模型返回的用量字段，按这几个名字累加
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def new_run_id():
    return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]


@dataclass
class TaskState:
    run_id: str
    status: str = STATUS_RUNNING
    stop_reason: str = ""
    final_answer: str = ""
    # attempts：请求模型的次数；tool_steps：执行工具的次数。一次请求可能返回多个工具调用
    attempts: int = 0
    tool_steps: int = 0
    # 本次提问累计的 token 用量
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @classmethod
    def create(cls):
        return cls(run_id=new_run_id())

    def record_attempt(self, usage):
        self.attempts += 1
        for name in USAGE_FIELDS:
            setattr(self, name, getattr(self, name) + int(usage.get(name) or 0))

    def record_tool(self):
        self.tool_steps += 1

    def finish(self, status, stop_reason, final_answer=""):
        # status 是“停下时的状态”，stop_reason 是“为什么停下”，分开存方便统计
        self.status = status
        self.stop_reason = stop_reason
        self.final_answer = final_answer

    def usage(self):
        return {name: getattr(self, name) for name in USAGE_FIELDS}
