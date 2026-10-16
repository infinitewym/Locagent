"""工具执行器：所有工具调用的统一入口。

执行前依次把关（工具是否开放、是否存在、参数是否合法、是否重复调用、是否需要审批），
执行时对写文件类工具做快照和备份，执行后判断成败、失败时回滚并附上重试提示。
所有结果都从 _finish 出去，在这里统一做密钥脱敏，模型和 trace 拿到的都是脱敏后的内容。
返回统一结构 ToolExecutionResult：给模型看的文本 + 给程序用的 metadata。
"""

import hashlib
import re
import time
from dataclasses import dataclass

from . import tools as toolkit
from .security import redact_text
from .workspace import IGNORED_PATH_NAMES, clip


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


# 每种失败附带一句提示，告诉模型"重试有没有意义、该怎么调整"。
# 只有超时属于临时性失败，原样调大超时后重试是合理的；其余都需要先改变做法。
FAILURE_HINTS = {
    "tool_not_allowed": (False, "this tool is unavailable in this run; use one of the listed tools"),
    "unknown_tool": (False, "use one of the listed tools"),
    "invalid_arguments": (False, "fix the arguments (see example) before calling again"),
    "repeated_identical_call": (False, "change the tool or arguments, or return a final answer"),
    "approval_denied": (False, "the user denied this action; choose a different approach"),
    "tool_failed": (False, "read the error and change the call before retrying"),
    "tool_partial_success": (False, "files changed before the failure; inspect affected files before retrying"),
    "command_failed": (False, "the command exited non-zero; read stdout/stderr and fix the cause before running it again"),
    "tool_timeout": (True, "the command timed out; retry with a larger timeout or a narrower command"),
}

# run_shell 结果第一行是 exit_code: N
EXIT_CODE_PATTERN = re.compile(r"^exit_code: (-?\d+)")

# 这两个工具在参数里就写明了要改哪个文件，失败时可以精确恢复原样
ROLLBACK_TOOLS = {"write_file", "patch_file"}


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    diff_summary=None,
    rolled_back=False,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
        "retryable": FAILURE_HINTS[tool_error_code][0] if tool_error_code else False,
        "rolled_back": bool(rolled_back),
    }
    return result


def _ms(started):
    return int((time.monotonic() - started) * 1000)


def _with_hint(content, tool_error_code):
    # 失败结果末尾追加一行 hint，成功结果原样返回
    if not tool_error_code:
        return content
    retryable, advice = FAILURE_HINTS[tool_error_code]
    prefix = "retryable" if retryable else "not retryable as-is"
    return f"{content}\nhint: {prefix}; {advice}"


def capture_workspace_snapshot(root):
    # 记录工作区里每个文件的 sha256，执行前后各拍一次，对比就知道改了哪些文件
    snapshot = {}
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_PATH_NAMES for part in relative_parts):
            continue
        if not path.is_file():
            continue
        try:
            snapshot[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            # 被其他程序占用或无权限读取的文件不参与对比
            continue
    return snapshot


def diff_workspace_snapshots(before, after):
    changed_paths = []
    summaries = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        changed_paths.append(path)
        if path not in before:
            summaries.append(f"created:{path}")
        elif path not in after:
            summaries.append(f"deleted:{path}")
        else:
            summaries.append(f"modified:{path}")
    return changed_paths, summaries


class ToolExecutor:
    def __init__(self, context, tools, approve, history):
        # context：工作区根目录和路径校验；tools：本次开放的工具（build_tool_registry 的结果）；
        # approve：审批函数 approve(name, args) -> bool，只对高风险工具调用；
        # history：agent 的对话历史列表（同一个对象，agent 追加记录后这里能直接看到），用于判断重复调用
        self.context = context
        self.tools = tools
        self.approve = approve
        self.history = history

    def _repeated_call(self, name, args):
        # 最常见的坏循环：没有拿到新信息，却反复发起同一个调用。
        # 最近两次工具调用都和这次完全相同（工具名和参数都一样）时拦截
        tool_events = [item for item in self.history if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        return all(item["name"] == name and item["args"] == args for item in tool_events[-2:])

    def _finish(self, name, args, content, metadata, duration_ms=0, approval_ms=0):
        # 所有返回路径都从这里出去：先把密钥替换成 <redacted>，失败时再在末尾追加提示。
        # 耗时分两项记录：duration_ms 是工具真正执行的时间，approval_ms 是等待人审批的时间，
        # 分开统计，性能数据才不会被人的反应速度干扰
        metadata["duration_ms"] = duration_ms
        metadata["approval_ms"] = approval_ms
        content = redact_text(content)
        return ToolExecutionResult(content=_with_hint(content, metadata["tool_error_code"]), metadata=metadata)

    def _backup_target(self, name, args):
        # 执行前保存目标文件的原始内容；文件原本不存在则记为 None
        if name not in ROLLBACK_TOOLS:
            return None
        path = self.context.path(args["path"])
        return {"path": path, "original": path.read_bytes() if path.is_file() else None}

    @staticmethod
    def _restore_target(backup):
        # 把目标文件恢复成执行前的样子：原来有就写回原内容，原来没有就删除
        path = backup["path"]
        if backup["original"] is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(backup["original"])

    def execute(self, name, args):
        args = args or {}
        if name not in self.tools:
            # 区分两种情况：工具存在但这次没开放，还是根本不存在
            error_code = "tool_not_allowed" if name in toolkit.legal_tool_names() else "unknown_tool"
            message = (
                f"error: tool '{name}' is not allowed in this run"
                if error_code == "tool_not_allowed"
                else f"error: unknown tool '{name}'"
            )
            return self._finish(
                name,
                args,
                message,
                _metadata("rejected", tool_error_code=error_code, risk_level="high", read_only=False),
            )

        tool = self.tools[name]
        try:
            toolkit.validate_tool(self.context, name, args)
        except Exception as exc:
            example = toolkit.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return self._finish(
                name,
                args,
                message,
                _metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        if self._repeated_call(name, args):
            return self._finish(
                name,
                args,
                f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                _metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        approval_ms = 0
        if tool["risky"]:
            approval_started = time.monotonic()
            allowed = self.approve(name, args)
            approval_ms = _ms(approval_started)
            if not allowed:
                return self._finish(
                    name,
                    args,
                    f"error: approval denied for {name}",
                    _metadata(
                        "rejected",
                        tool_error_code="approval_denied",
                        security_event_type="approval_denied",
                        risk_level="high",
                        read_only=False,
                    ),
                    approval_ms=approval_ms,
                )

        run_started = time.monotonic()
        root = self.context.root
        before_snapshot = capture_workspace_snapshot(root) if tool["risky"] else {}
        backup = self._backup_target(name, args)
        try:
            content = clip(tool["run"](args))
            after_snapshot = capture_workspace_snapshot(root) if tool["risky"] else before_snapshot
            affected_paths, diff_summary = diff_workspace_snapshots(before_snapshot, after_snapshot)
            status, error_code = "ok", ""
            if name == "run_shell" and int(EXIT_CODE_PATTERN.match(content).group(1)) != 0:
                # 命令执行了但失败了；如果失败前已经改了文件，记为部分成功
                status = "partial_success" if affected_paths else "error"
                error_code = "command_failed"
            metadata = _metadata(
                status,
                tool_error_code=error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=bool(affected_paths),
                diff_summary=diff_summary,
            )
            return self._finish(name, args, content, metadata, _ms(run_started), approval_ms)
        except Exception as exc:
            after_snapshot = capture_workspace_snapshot(root) if tool["risky"] else before_snapshot
            affected_paths, diff_summary = diff_workspace_snapshots(before_snapshot, after_snapshot)
            message = f"error: tool {name} failed: {exc}"
            rolled_back = False
            if backup is not None and affected_paths:
                # 写文件类工具中途失败：把目标文件恢复原样，再重新对比一次
                self._restore_target(backup)
                rolled_back = True
                after_snapshot = capture_workspace_snapshot(root)
                affected_paths, diff_summary = diff_workspace_snapshots(before_snapshot, after_snapshot)
                message += f"\nrolled back: {backup['path'].relative_to(root)} restored to its previous state"
            workspace_changed = bool(affected_paths)
            if isinstance(exc, TimeoutError):
                error_code = "tool_timeout"
            else:
                error_code = "tool_partial_success" if workspace_changed else "tool_failed"
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code=error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                diff_summary=diff_summary,
                rolled_back=rolled_back,
            )
            return self._finish(name, args, message, metadata, _ms(run_started), approval_ms)
