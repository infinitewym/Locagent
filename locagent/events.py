"""事件机制。

主循环在每个关键节点发出一个事件（开始提问、上下文清理或压缩、模型回复、工具开始、工具结束、运行时提示、结束提问等），
由订阅者各自处理：会话记录写进 .locagent/sessions/<session_id>.jsonl，命令行订阅者打印到终端。
对话历史每追加一条记录也发一个 history_recorded 事件，恢复会话时靠它重建历史。
主循环只管发事件，不关心谁在听；以后做网页版，只要再加一个“推送到浏览器”的订阅者。
"""

from .workspace import now

# 事件类型。不属于某次提问的事件（会话开始、手动压缩）run_id 为 None
SESSION_STARTED = "session_started"
SESSION_RESUMED = "session_resumed"
HISTORY_RECORDED = "history_recorded"
RUN_STARTED = "run_started"
MODEL_RESPONDED = "model_responded"
MODEL_FAILED = "model_failed"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
NOTICE = "notice"
CONTEXT_REDUCED = "context_reduced"
CHECKPOINT = "checkpoint"
RUN_FINISHED = "run_finished"


class EventBus:
    def __init__(self):
        self.handlers = []

    def subscribe(self, handler):
        # handler(event)：event 是字典，至少包含 event（类型）、run_id、time
        self.handlers.append(handler)

    def emit(self, event_type, run_id, **data):
        event = {"event": event_type, "run_id": run_id, "time": now(), **data}
        for handler in self.handlers:
            handler(event)
        return event
