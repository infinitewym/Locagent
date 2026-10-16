"""测试和离线实验用的假模型。

真实模型接入在 qwen.py。agent 只依赖
complete(system, messages, tools, max_new_tokens, tool_choice=None) -> ModelResponse 这个接口，
所以假模型只要按顺序返回预先写好的 ModelResponse 即可。
"""

import copy


class FakeModelClient:
    def __init__(self, outputs):
        self.model = "fake"
        self.outputs = list(outputs)
        # 每次调用收到的完整请求，方便查看模型每轮看到了什么
        self.requests = []
        self.last_completion_metadata = {}

    def complete(self, system, messages, tools, max_new_tokens, tool_choice=None):
        self.requests.append({"system": system, "messages": copy.deepcopy(messages), "tools": tools, "tool_choice": tool_choice})
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)
