"""Agent 主循环。

一次用户提问对应一次 run()：反复“请求模型 -> 执行工具 -> 记录结果”，
直到模型不再调用工具、直接给出回答，或者达到步数上限。
每个关键节点都发出事件（写进会话文件 .locagent/sessions/<session_id>.jsonl、显示到终端）。
"""

import time

from . import events as ev
from .checkpoint import Checkpoint
from .workspace import clip
from .task_state import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_STOPPED,
    STOP_REASON_FINAL_ANSWER,
    STOP_REASON_INTERRUPTED,
    STOP_REASON_MODEL_ERROR,
    STOP_REASON_RETRY_LIMIT,
    STOP_REASON_STEP_LIMIT,
    TaskState,
)
# 工具步数用完后，最后一次请求附带的提示
FINALIZATION_NOTICE = (
    "the tool budget is exhausted. Do not call another tool. "
    "Use the evidence already present in the tool results and give your final answer now."
)
# 输出被 max_tokens 截断时的提示：截断的工具调用参数不完整，不能执行
TRUNCATED_NOTICE = (
    "your previous response was cut off by the output token limit and was discarded. "
    "Write large files in smaller parts (write_file for the first part, then patch_file to extend), "
    "or keep the answer shorter."
)
EMPTY_NOTICE = "your previous response was empty. Call a tool or give your answer."
# 只更新待办清单的工具不计入工具步数：它不读写工作区，每做完一项就打一次勾，计入的话会挤占真正干活的步数。
# 防止无限循环靠 max_attempts（模型请求次数上限），不受影响
BOOKKEEPING_TOOLS = ("update_todos",)
# 收尾请求不允许调用工具。历史里已有工具调用块，接口要求必须带 tools 参数，所以不能去掉 tools，只能用 tool_choice 禁止
NO_TOOLS = {"type": "none"}
STEP_LIMIT_ANSWER = "Stopped after reaching the step limit without a final answer."
RETRY_LIMIT_ANSWER = "Stopped after too many truncated or empty model responses."
# 工具结果的完整内容在 history_recorded 事件里，tool_finished 只带一段预览，避免同一内容在会话文件里存两遍
RESULT_PREVIEW_LIMIT = 300


def _ms(started):
    return int((time.monotonic() - started) * 1000)


class AgentLoop:
    def __init__(self, agent):
        self.agent = agent
        self.task = None
        self.checkpoint = None
        self.started = 0.0

    def _check_stop(self):
        # 网页点了停止：和 Ctrl+C 走同一条路径（run() 里按 interrupted 结束）
        if self.agent.stop_event.is_set():
            raise KeyboardInterrupt

    def _emit(self, event_type, **data):
        self.agent.events.emit(event_type, self.task.run_id, **data)

    def _save_checkpoint(self):
        # 检查点写进会话文件；恢复会话时取最后一个。待办清单在保存时从 agent 复制一份
        self.checkpoint.todos = [dict(item) for item in self.agent.todos]
        self.agent.checkpoint = self.checkpoint
        self._emit(ev.CHECKPOINT, checkpoint=self.checkpoint.to_dict())

    def _notice(self, content):
        # 运行时给模型的提示：记进历史（下一轮模型能看到），同时发出事件
        self.agent.record({"role": "runtime", "content": content})
        self._emit(ev.NOTICE, content=content)

    def _request_model(self, tool_choice=None):
        started = time.monotonic()
        response = self.agent.request_model(tool_choice)
        self.task.record_attempt(response.usage)
        # 记下这次请求的实际输入 token 数，下次提问开始前据此判断是否需要清理或压缩
        self.agent.context_manager.observe(response.usage)
        # 工具调用的参数在 assistant 历史记录里，这里只记 id 和工具名；
        # 文字保留全文：输出被截断的回复不进历史，只有这里能看到
        self._emit(
            ev.MODEL_RESPONDED,
            attempt=self.task.attempts,
            stop_reason=response.stop_reason,
            text=response.text,
            tool_calls=[{"id": call.id, "name": call.name} for call in response.tool_calls],
            usage=response.usage,
            duration_ms=_ms(started),
        )
        return response

    def _record_response(self, response):
        # 调用工具时模型顺带写的那句话就是它的进度说明，记进检查点
        if response.tool_calls:
            self.checkpoint.record_progress(response.text)
        # 模型的文字和工具调用作为一条 assistant 记录保存
        self.agent.record(
            {
                "role": "assistant",
                "content": response.text,
                "tool_calls": [{"id": call.id, "name": call.name, "args": call.args} for call in response.tool_calls],
            }
        )

    def _run_tool_calls(self, tool_calls):
        # 接口可能一次返回多个工具调用，按顺序全部执行；
        # 每个调用都必须有对应的工具结果，否则下一次请求会被接口拒绝
        for call in tool_calls:
            # 没执行的工具调用，下次提问开始时由 runtime 补上“被中断”的结果
            self._check_stop()
            # 统计的工具步数和步数上限口径一致：不含 update_todos
            if call.name not in BOOKKEEPING_TOOLS:
                self.task.record_tool()
            # 先发“开始”事件再执行：需要审批时，终端先显示要做什么，再弹出审批提示
            self._emit(ev.TOOL_STARTED, tool_use_id=call.id, name=call.name, args=call.args)
            result = self.agent.execute_tool(call.name, call.args)
            self.agent.record(
                {
                    "role": "tool",
                    "tool_use_id": call.id,
                    "name": call.name,
                    "args": call.args,
                    "content": result.content,
                    "is_error": result.metadata["tool_status"] != "ok",
                }
            )
            self._emit(
                ev.TOOL_FINISHED,
                tool_use_id=call.id,
                name=call.name,
                result=clip(result.content, RESULT_PREVIEW_LIMIT),
                # metadata 里带有 duration_ms（执行耗时，不含审批等待）和 approval_ms（审批等待）
                **result.metadata,
            )
            # 每执行完一步就更新检查点：中途被中断，也知道已经做到哪一步
            self.checkpoint.record_step(
                call.name, call.args, result.metadata["tool_status"], result.metadata["affected_paths"], self.agent.context
            )
            self._save_checkpoint()

    def _skip_tool_calls(self, tool_calls, reason):
        # 不执行的工具调用也要回一个结果，保持调用和结果一一对应
        for call in tool_calls:
            self.agent.record(
                {
                    "role": "tool",
                    "tool_use_id": call.id,
                    "name": call.name,
                    "args": call.args,
                    "content": f"error: not executed: {reason}",
                    "is_error": True,
                }
            )

    def _loop(self):
        """主循环本体，返回 (最终回答, 状态, 停止原因)。"""
        agent = self.agent
        # 计入步数上限的工具调用次数（不含 update_todos）
        tool_steps = 0
        attempts = 0
        # attempts 统计所有模型调用次数（包括被截断、空回复需要重来的），防止无限重试
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)

        # 主循环可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：把历史转成消息发给模型
        # 2. 决策：模型返回工具调用，或者直接回答
        # 3. 行动：如果有工具调用，交给 ToolExecutor 执行
        # 4. 记录：把模型回复和工具结果写回 history，下一轮请求就能看到
        while tool_steps < agent.max_steps and attempts < max_attempts:
            attempts += 1
            self._check_stop()
            response = self._request_model()

            if response.stop_reason == "max_tokens":
                # 截断的回复（尤其是写了一半的工具参数）丢弃，只记录提示让模型重来
                self._notice(TRUNCATED_NOTICE)
                continue

            self._record_response(response)

            if response.tool_calls:
                tool_steps += sum(1 for call in response.tool_calls if call.name not in BOOKKEEPING_TOOLS)
                self._run_tool_calls(response.tool_calls)
                continue

            if response.text.strip():
                # 没有工具调用、只有文字：这就是最终回答
                return response.text.strip(), STATUS_COMPLETED, STOP_REASON_FINAL_ANSWER

            self._notice(EMPTY_NOTICE)

        if tool_steps < agent.max_steps:
            agent.record({"role": "assistant", "content": RETRY_LIMIT_ANSWER, "tool_calls": []})
            return RETRY_LIMIT_ANSWER, STATUS_STOPPED, STOP_REASON_RETRY_LIMIT

        # 工具步数用完：再请求一次，要求模型根据已有结果直接回答
        self._notice(FINALIZATION_NOTICE)
        response = self._request_model(NO_TOOLS)
        if response.stop_reason != "max_tokens":
            self._record_response(response)
            if response.tool_calls:
                self._skip_tool_calls(response.tool_calls, "tool budget exhausted")
            elif response.text.strip():
                # 收尾回答通常是“做到哪了、还剩什么”，任务并没有做完：记为步数用完而停止，
                # 检查点保持未完成，用户可以 /continue 接着做
                return response.text.strip(), STATUS_STOPPED, STOP_REASON_STEP_LIMIT
        agent.record({"role": "assistant", "content": STEP_LIMIT_ANSWER, "tool_calls": []})
        return STEP_LIMIT_ANSWER, STATUS_STOPPED, STOP_REASON_STEP_LIMIT

    def _finish(self, status, stop_reason, final_answer):
        # 无论怎么结束，都更新检查点并发出结束事件：会话文件里这次提问的最后一行就是汇总
        self.task.finish(status, stop_reason, final_answer)
        self.checkpoint.status = status
        self.checkpoint.stop_reason = stop_reason
        self._save_checkpoint()
        self._emit(
            ev.RUN_FINISHED,
            status=status,
            stop_reason=stop_reason,
            final_answer=final_answer,
            attempts=self.task.attempts,
            tool_steps=self.task.tool_steps,
            usage=self.task.usage(),
            duration_ms=_ms(self.started),
        )

    def _prepare_context(self):
        # 提问开始前检查上下文长度，需要时清理旧工具结果或压缩成摘要（压缩会请求一次模型）
        result = self.agent.context_manager.prepare()
        if result is None:
            return
        if result["action"] == "compact":
            self.task.record_attempt(result["usage"])
        self._emit(ev.CONTEXT_REDUCED, **result)

    def run(self, user_message, resume_from=None):
        # resume_from：/continue 接着做时传入上一个检查点。新检查点沿用它的原始请求、已做步骤、进度和文件记录，
        # 再次中断后还能接着做，之前的步骤也不会丢；普通提问时为 None，任务就是用户这句话本身
        agent = self.agent
        agent.stop_event.clear()
        self.started = time.monotonic()
        self.task = TaskState.create()
        if resume_from is None:
            self.checkpoint = Checkpoint(run_id=self.task.run_id, request=user_message)
        else:
            self.checkpoint = Checkpoint.from_dict({**resume_from.to_dict(), "run_id": self.task.run_id, "status": "running", "stop_reason": ""})
        # 这次提问期间追加的历史记录都带上 run_id
        agent.current_run_id = self.task.run_id
        # 每次提问的第一个事件：用户请求和运行配置，复盘时知道是在什么条件下跑的
        self._emit(ev.RUN_STARTED, user_request=user_message, config=agent.run_config())
        # 一开始就写一次检查点：即使第一步还没做完程序就崩溃了，恢复时也知道有个任务没做完
        self._save_checkpoint()

        try:
            self._prepare_context()
            agent.start_request(user_message)
            final, status, stop_reason = self._loop()
        except KeyboardInterrupt:
            # Ctrl+C 或网页停止：记下中断状态再继续往外抛，由命令行回到提示符、网页结束本次运行
            self._finish(STATUS_STOPPED, STOP_REASON_INTERRUPTED, "")
            raise
        except RuntimeError as exc:
            # 模型请求失败（鉴权、限流、超时等）
            self._emit(ev.MODEL_FAILED, error=str(exc))
            self._finish(STATUS_FAILED, STOP_REASON_MODEL_ERROR, "")
            raise
        finally:
            agent.current_run_id = None
        self._finish(status, stop_reason, final)
        return final
