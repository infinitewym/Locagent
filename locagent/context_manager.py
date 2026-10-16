"""上下文管理：历史太长时，决定清掉什么、压缩什么。

历史记录会一直累积，输入 token 越来越多、越来越贵，最终超过模型上限。
这里在每次用户提问开始前检查上一次请求的实际输入 token 数，按两级处理：

1. 超过预算的 50%：清理旧工具结果。
   上一次提问之前的工具结果（读到的文件内容、命令输出）替换成一行占位说明，
   工具调用本身保留，tool_use 和 tool_result 仍然一一对应；需要时模型可以重新调用工具。
2. 超过预算的 75%：压缩对话。
   请模型把之前的全部对话总结成一段摘要，历史只保留这段摘要。

只在提问开始时处理，不在一次提问的中途处理：改动之前的消息会让缓存失效，
放在提问边界上，每次处理只损失一次缓存，之后又能逐轮命中。
参考 pico 的思路：当前请求永远不裁剪，优先裁剪体积大、价值低的旧工具结果。
"""

import json

from .workspace import clip

DEFAULT_CONTEXT_BUDGET = 64000
CLEAR_RATIO = 0.5
COMPACT_RATIO = 0.75
# 内容短于这个长度的工具结果不清理，清理了也省不了多少
CLEAR_MIN_CHARS = 300
# 生成摘要时，每条历史内容最多带给模型的字符数，以及摘要的最大输出 token 数
SUMMARY_ITEM_LIMIT = 2000
SUMMARY_MAX_TOKENS = 2048

SUMMARY_SYSTEM = (
    "You summarize a conversation between a user and a coding agent so the agent can continue the work "
    "without the full history. Write the summary in the same language the user used."
)
SUMMARY_INSTRUCTIONS = """Summarize the conversation above. Include:
1. What the user asked for, in order, and which requests are finished.
2. Files that were read, created or changed, with the key details of each change.
3. Commands that were run and their important results (errors, test results).
4. The current state of the work and anything still unfinished.
5. Facts the agent must remember: paths, names, decisions, user preferences.
Be concise and concrete. Do not invent anything that is not in the conversation."""


def clear_tool_record(item):
    # 把一条工具结果替换成一行占位说明。恢复会话时按同样的方式重放，所以单独成函数
    brief = clip(json.dumps(item["args"], ensure_ascii=False), 120)
    item["content"] = f"[old result cleared to save context: {item['name']} {brief}. Call the tool again if you need it.]"
    item["cleared"] = True


def input_tokens(usage):
    # 一次请求的实际输入 = 未缓存部分 + 命中缓存部分 + 写入缓存部分
    return sum(int(usage.get(name) or 0) for name in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


class ContextManager:
    def __init__(self, agent, budget=DEFAULT_CONTEXT_BUDGET):
        self.agent = agent
        self.budget = budget
        # 最近一次请求的实际输入 token 数，由主循环在每次模型回复后更新
        self.last_input_tokens = 0

    def observe(self, usage):
        self.last_input_tokens = input_tokens(usage)

    def prepare(self):
        """每次提问开始前调用。需要处理时执行并返回处理情况，不需要时返回 None。"""
        before = self.last_input_tokens
        if before > self.budget * COMPACT_RATIO:
            result = self.compact()
        elif before > self.budget * CLEAR_RATIO:
            result = self.clear_old_tool_results()
            if result["cleared"] == 0:
                # 没有可清理的旧结果（例如只有一次提问），等超过压缩阈值时再压缩
                return None
        else:
            return None
        return {**result, "input_tokens_before": before, "budget": self.budget}

    def clear_old_tool_results(self):
        history = self.agent.history
        # 上一次提问（最后一条 user 记录）之前的工具结果才清理；最近一次提问的结果保留，用户常会接着追问
        last_user = max((index for index, item in enumerate(history) if item["role"] == "user"), default=0)
        cleared_indices = []
        freed_chars = 0
        for index, item in enumerate(history[:last_user]):
            if item["role"] != "tool" or item.get("cleared") or len(item["content"]) < CLEAR_MIN_CHARS:
                continue
            freed_chars += len(item["content"])
            clear_tool_record(item)
            cleared_indices.append(index)
        # cleared_indices 写进事件，恢复会话时按位置重放同样的清理
        return {"action": "clear_tool_results", "cleared": len(cleared_indices), "cleared_indices": cleared_indices, "freed_chars": freed_chars}

    def _transcript(self):
        # 把历史整理成给摘要用的纯文本，每条内容截断到 SUMMARY_ITEM_LIMIT
        lines = []
        for item in self.agent.history:
            role = item["role"]
            if role == "assistant":
                if item["content"]:
                    lines.append(f"[assistant] {clip(item['content'], SUMMARY_ITEM_LIMIT)}")
                for call in item["tool_calls"]:
                    lines.append(f"[tool_call {call['name']}] {clip(json.dumps(call['args'], ensure_ascii=False), SUMMARY_ITEM_LIMIT)}")
            elif role == "tool":
                lines.append(f"[tool_result {item['name']}] {clip(item['content'], SUMMARY_ITEM_LIMIT)}")
            else:
                lines.append(f"[{role}] {clip(item['content'], SUMMARY_ITEM_LIMIT)}")
        return "\n".join(lines)

    def compact(self):
        """请模型总结全部历史，用一条摘要记录替换掉历史。"""
        history = self.agent.history
        if not history:
            return {"action": "compact", "replaced_records": 0, "summary": "", "summary_chars": 0, "usage": {}}
        prompt = f"<conversation>\n{self._transcript()}\n</conversation>\n\n{SUMMARY_INSTRUCTIONS}"
        response = self.agent.model_client.complete(
            SUMMARY_SYSTEM,
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            [],
            SUMMARY_MAX_TOKENS,
        )
        summary = response.text.strip()
        replaced = len(history)
        # 原地替换：ToolExecutor 持有的是同一个列表
        history.clear()
        history.append({"role": "summary", "content": summary})
        self.last_input_tokens = 0
        # 摘要全文写进 context_reduced 事件：压缩之后模型只看得到这段摘要，排查“模型忘了什么”时要能查到
        return {"action": "compact", "replaced_records": replaced, "summary": summary, "summary_chars": len(summary), "usage": response.usage}
