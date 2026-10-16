"""Agent 运行时核心。

LocAgent 就是包在模型外面的控制层：保存对话历史、把历史转成接口需要的消息、
请求模型、把工具调用交给 ToolExecutor 执行，并持有会话记录（SessionStore）和事件总线（EventBus）。
主循环本身在 agent_loop.py。
"""

import json
import threading
from pathlib import Path

from . import security
from . import tools as toolkit
from .agent_loop import AgentLoop
from .checkpoint import Checkpoint
from .context_manager import DEFAULT_CONTEXT_BUDGET, ContextManager
from . import events as ev
from .events import EventBus
from .prompt_prefix import build_prompt_prefix
from .session_store import SessionStore, new_session_id, replay
from .tool_context import ToolContext
from .tool_executor import ToolExecutor
from .workspace import WorkspaceContext, clip

# 审批选项：approver 返回其中之一
APPROVE_YES = "yes"
APPROVE_NO = "no"
APPROVE_ALWAYS = "always"
# 选了 always 之后的记忆范围：改文件的两个工具算同一类，一次放行，本次会话所有编辑都不再询问；
# run_shell 按完整命令记，只有一模一样的命令不再询问，因为不同命令的风险差别太大
EDIT_TOOLS = ("write_file", "patch_file")
# /history 显示时每条内容最多显示的字符数
HISTORY_DISPLAY_LIMIT = 300

__all__ = ["LocAgent"]


class LocAgent:
    def __init__(
        self,
        model_client,
        workspace,
        approval_policy="ask",
        approver=None,
        max_steps=10,
        max_new_tokens=8192,
        context_budget=DEFAULT_CONTEXT_BUDGET,
        allowed_tools=None,
        resume=None,
    ):
        # resume：要恢复的会话 id，"latest" 表示最近一次的会话，None 表示开始新会话
        # approval_policy：ask 逐个询问；auto 全部同意；never 全部拒绝。
        # approver(name, args) -> "yes" / "no" / "always"，由调用方提供：命令行在终端里问，以后网页版在页面上问
        if approval_policy == "ask" and approver is None:
            raise ValueError("approval_policy 'ask' needs an approver")
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.cwd)
        self.approval_policy = approval_policy
        self.approver = approver
        # 本次会话里选过 always 的审批记录，退出即清空
        self.always_allowed = set()
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        # 对话历史，按时间顺序追加，是发给模型的 messages 的唯一来源。每条记录是下面五种之一：
        #   {"role": "user", "content": 用户原话, "status": 提问时的工作区状态}
        #   {"role": "assistant", "content": 模型的文字, "tool_calls": [{"id", "name", "args"}]}
        #   {"role": "tool", "tool_use_id", "name", "args", "content": 工具结果, "is_error": 是否失败, "cleared": 是否已被清理}
        #   {"role": "runtime", "content": 运行时给模型的提示，例如输出被截断}
        #   {"role": "summary", "content": 压缩对话后得到的摘要，替代之前的全部历史}
        # 每追加一条都会写进会话文件，退出后可以用 --resume 恢复
        self.history = []
        self.context = ToolContext(self.root, shell_env=security.shell_env())
        # 模型用 update_todos 维护的待办清单，和 ToolContext 共用同一个列表
        self.todos = self.context.todos
        self.tools = toolkit.build_tool_registry(self.context, allowed_tools)
        self.tool_definitions = toolkit.tool_definitions(self.tools)
        self.tool_executor = ToolExecutor(self.context, self.tools, self.approve, self.history)
        self.prefix = build_prompt_prefix(self.workspace, self.tools)
        # 启动时刚采集过工作区信息，第一次提问不用重复采集
        self._workspace_fresh = True
        # 历史太长时清理旧工具结果或压缩成摘要，见 context_manager.py
        self.context_manager = ContextManager(self, budget=context_budget)
        # 当前正在进行的提问的 run_id，由主循环设置；不在提问中时为 None
        self.current_run_id = None
        # 最近一个任务的检查点，由主循环更新；没做完时可以用 continue_task() 接着做
        self.checkpoint = None
        # 请求停止当前任务（网页的“停止”按钮）。命令行用 Ctrl+C，效果相同：
        # 主循环在每次请求模型前、每个工具执行前检查，置位后按“被中断”结束，之后可以 /continue
        self.stop_event = threading.Event()
        # 会话记录：<工作区>/.locagent/sessions/<session_id>.jsonl，所有事件都写进去。
        # 其他订阅者（例如命令行实时显示）由调用方通过 agent.events.subscribe() 添加
        self.session_store = SessionStore(self.root / ".locagent" / "sessions")
        self.events = EventBus()
        self.events.subscribe(lambda event: self.session_store.append(self.session_id, event))
        if resume:
            self._resume(resume)
        else:
            self._start_session()

    def _start_session(self):
        self.session_id = new_session_id()
        self.events.emit(ev.SESSION_STARTED, None, config=self.run_config())

    def _resume(self, session_id):
        # 读出会话文件，重放事件还原历史；本次会话的 always 审批记录不恢复，重新启动后需要重新确认
        if session_id == "latest":
            session_id = self.session_store.latest()
            if session_id is None:
                raise RuntimeError("no saved session in this workspace")
        history, last_input_tokens, checkpoint = replay(self.session_store.load(session_id))
        self.checkpoint = Checkpoint.from_dict(checkpoint) if checkpoint else None
        if self.checkpoint is not None:
            self.todos[:] = [dict(item) for item in self.checkpoint.todos]
        self.session_id = session_id
        # 原地填充：ToolExecutor 持有的是同一个列表
        self.history.extend(history)
        self.context_manager.last_input_tokens = last_input_tokens
        self.events.emit(ev.SESSION_RESUMED, None, records=len(history), config=self.run_config())

    @property
    def session_path(self):
        return self.session_store.path(self.session_id)

    def run_config(self):
        # 本次运行的配置，写进 trace 第一行（run_started 事件）
        return {
            "workspace": str(self.root),
            "model": self.model_client.model,
            "approval_policy": self.approval_policy,
            "max_steps": self.max_steps,
            "max_new_tokens": self.max_new_tokens,
            "context_budget": self.context_manager.budget,
            "tools": list(self.tools),
        }

    def start_request(self, user_message):
        # 每次用户提问的开头：补齐被中断的工具调用、刷新工作区信息、记录用户消息。
        # 上一次提问期间 agent 可能改了文件，git 状态会变
        self._close_interrupted_tool_calls()
        if not self._workspace_fresh:
            self.workspace = WorkspaceContext.build(self.root)
            self.prefix = build_prompt_prefix(self.workspace, self.tools)
        self._workspace_fresh = False
        # 工作区状态在提问时定下来，和用户原话存在同一条记录里，之后不再变化，保证历史消息稳定可缓存
        self.record({"role": "user", "content": user_message, "status": self.workspace.status_text()})

    def _close_interrupted_tool_calls(self):
        # 接口要求每个工具调用在下一次请求里都有对应的结果，否则直接报错。
        # 上一次提问如果在审批或执行工具时被 Ctrl+C 中断，最后一条 assistant 记录里会有调用没有结果，
        # 这里补上“未执行”的结果，保证每次提问前历史都是完整的。
        # 中断后不会再有新的 assistant 记录，所以只需要检查最后一条
        last = next((item for item in reversed(self.history) if item["role"] == "assistant"), None)
        if last is None:
            return
        answered = {item["tool_use_id"] for item in self.history if item["role"] == "tool"}
        for call in last["tool_calls"]:
            if call["id"] not in answered:
                self.record(
                    {
                        "role": "tool",
                        "tool_use_id": call["id"],
                        "name": call["name"],
                        "args": call["args"],
                        "content": "error: not executed: interrupted by the user",
                        "is_error": True,
                    }
                )

    def messages(self):
        """把 history 转成接口需要的 messages（Anthropic 格式）。

        规则：
        - user 记录 -> user 消息，开头附上提问时的工作区状态
        - assistant 记录 -> assistant 消息：文字块 + tool_use 块
        - tool 记录 -> user 消息里的 tool_result 块，用 tool_use_id 对应到调用
        - runtime 记录 -> user 消息里的文字块
        - summary 记录 -> user 消息里的文字块，用 <conversation_summary> 包起来
        相邻的同角色内容合并成一条消息（例如一次调用多个工具时，多个 tool_result 放在同一条 user 消息里）。
        历史的长度控制在 ContextManager 里完成（直接修改 history），这里只负责格式转换。
        """
        messages = []

        def add(role, block):
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": role, "content": [block]})

        for item in self.history:
            role = item["role"]
            if role == "user":
                add("user", {"type": "text", "text": f"{item['status']}\n\n{item['content']}"})
            elif role == "assistant":
                if item["content"]:
                    add("assistant", {"type": "text", "text": item["content"]})
                for call in item["tool_calls"]:
                    add("assistant", {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["args"]})
            elif role == "tool":
                add(
                    "user",
                    {"type": "tool_result", "tool_use_id": item["tool_use_id"], "content": item["content"], "is_error": item["is_error"]},
                )
            elif role == "summary":
                add("user", {"type": "text", "text": f"<conversation_summary>\n{item['content']}\n</conversation_summary>"})
            else:
                add("user", {"type": "text", "text": f"Runtime notice: {item['content']}"})
        return messages

    def request_model(self, tool_choice=None):
        # 模型请求失败（鉴权、限流、超时）时直接抛 RuntimeError，由命令行层打印原因。
        # tool_choice 为 {"type": "none"} 时本次不允许调用工具（步数用完后的收尾请求）
        return self.model_client.complete(
            self.prefix.text, self.messages(), self.tool_definitions, self.max_new_tokens, tool_choice=tool_choice
        )

    def history_text(self):
        # 给人看的历史（/history 命令），每条内容截短显示
        if not self.history:
            return "- empty"
        lines = []
        for item in self.history:
            role = item["role"]
            if role == "assistant":
                if item["content"]:
                    lines.append(f"[assistant] {clip(item['content'], HISTORY_DISPLAY_LIMIT)}")
                for call in item["tool_calls"]:
                    lines.append(f"[tool_call:{call['name']}] {clip(json.dumps(call['args'], ensure_ascii=False), HISTORY_DISPLAY_LIMIT)}")
            elif role == "tool":
                mark = "error" if item["is_error"] else "ok"
                lines.append(f"[tool_result:{item['name']}:{mark}] {clip(item['content'], HISTORY_DISPLAY_LIMIT)}")
            else:
                lines.append(f"[{role}] {clip(item['content'], HISTORY_DISPLAY_LIMIT)}")
        return "\n".join(lines)

    def record(self, item):
        # 追加一条历史，同时写进会话文件（history_recorded 事件），恢复会话时据此重建
        self.history.append(item)
        self.events.emit(ev.HISTORY_RECORDED, self.current_run_id, record=item)

    def compact(self):
        # 手动压缩（/compact）：不在某次提问里，事件的 run_id 为 None
        before = self.context_manager.last_input_tokens
        result = self.context_manager.compact()
        self.events.emit(ev.CONTEXT_REDUCED, None, **result, input_tokens_before=before, budget=self.context_manager.budget)
        return result

    def reset(self):
        # 开始新的会话：清空历史和 always 审批记录，之后的事件写进新的会话文件。
        # 历史原地清空：ToolExecutor 持有的是同一个列表
        self.history.clear()
        self.always_allowed.clear()
        self.context_manager.last_input_tokens = 0
        self.checkpoint = None
        self.todos.clear()
        self._start_session()

    def ask(self, user_message):
        return AgentLoop(self).run(user_message)

    def continue_task(self):
        # 接着做最近一个没做完的任务：把检查点整理成说明发给模型，沿用原始请求作为任务
        if self.checkpoint is None or not self.checkpoint.unfinished:
            raise RuntimeError("no unfinished task to continue")
        message = self.checkpoint.continue_message(self.context)
        return AgentLoop(self).run(message, resume_from=self.checkpoint)

    def execute_tool(self, name, args):
        return self.tool_executor.execute(name, args)

    @staticmethod
    def _approval_key(name, args):
        # always 记忆的粒度：编辑类工具按类别，run_shell 按完整命令
        if name in EDIT_TOOLS:
            return "edit"
        if name == "run_shell":
            return f"run_shell:{args['command']}"
        return name

    def approve(self, name, args):
        # ToolExecutor 在执行高风险工具前调用，返回是否允许
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        key = self._approval_key(name, args)
        if key in self.always_allowed:
            return True
        answer = self.approver(name, args)
        if answer == APPROVE_ALWAYS:
            self.always_allowed.add(key)
        return answer in (APPROVE_YES, APPROVE_ALWAYS)
