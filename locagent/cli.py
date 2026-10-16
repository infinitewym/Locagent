"""命令行入口。

把“用户怎么启动 LocAgent”翻译成 runtime 能理解的对象：
解析参数、采集工作区信息、加载 .env、创建模型客户端，
最后进入单次提问（one-shot）或交互式循环。
"""

import argparse
import json
import os
import shutil
import sys
import textwrap
import time

from . import events as ev
from .config import load_project_env
from .context_manager import COMPACT_RATIO, DEFAULT_CONTEXT_BUDGET
from .providers.qwen import QwenModelClient, qwen_settings
from .tools import render_todos
from .runtime import APPROVE_ALWAYS, APPROVE_NO, APPROVE_YES, EDIT_TOOLS, LocAgent
from .workspace import WorkspaceContext, middle

# 终端实时显示时，参数和结果最多显示的字符数
CONSOLE_ARGS_LIMIT = 80
CONSOLE_TEXT_LIMIT = 160
# 审批预览里每段内容最多显示的行数
APPROVAL_PREVIEW_LINES = 12
# 粘贴多行文字时，等待后续行到达缓冲区的时间（秒）
PASTE_SETTLE_SECONDS = 0.05
# 这些开头的输入是应该在系统终端里执行的命令，不是给模型的问题
SHELL_COMMAND_PREFIXES = ("uv run locagent", "locagent ", "python -m locagent")

WELCOME_NAME = "LocAgent"
WELCOME_SUBTITLE = "local coding agent"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help     Show this help message.
    /history  Show the conversation history the model sees.
    /context  Show how much of the context budget the last request used.
    /compact  Summarize the conversation so far to free context.
    /session  Show the current session file.
    /continue Continue the last unfinished task (interrupted, failed or out of steps).
    /reset    Start a new session (the current one stays saved and can be resumed).
    /exit     Exit the agent.
    """
).strip()


def _build_model_client(args):
    # 模型名和接口地址：命令行参数 > .env > 默认值；密钥只从 .env 读取
    settings = qwen_settings(model=args.model, base_url=args.base_url)
    return QwenModelClient(
        model=settings["model"],
        base_url=settings["base_url"],
        api_key=settings["api_key"],
        temperature=args.temperature,
        timeout=args.timeout,
    )


def _brief_args(args):
    # 终端里每次工具调用显示一行：命令显示命令本身，搜索显示关键词，文件类显示路径
    if "command" in args:
        return middle(str(args["command"]), CONSOLE_ARGS_LIMIT)
    if "pattern" in args:
        return f'"{middle(str(args["pattern"]), CONSOLE_ARGS_LIMIT)}" in {args.get("path", ".")}'
    if "path" in args:
        return str(args["path"])
    return middle(json.dumps(args, ensure_ascii=False), CONSOLE_ARGS_LIMIT)


def _preview_lines(text, prefix):
    # 多行内容逐行加前缀显示，超过行数只显示开头
    lines = str(text).splitlines() or [""]
    shown = [f"      {prefix}{line}" for line in lines[:APPROVAL_PREVIEW_LINES]]
    if len(lines) > APPROVAL_PREVIEW_LINES:
        shown.append(f"      ... ({len(lines) - APPROVAL_PREVIEW_LINES} more lines)")
    return shown


def approval_preview(name, args):
    # 审批前给人看的内容：命令看命令，改文件看改前改后，写文件看开头几行
    if name == "run_shell":
        return [f"      $ {args['command']}", f"      (timeout {args.get('timeout', 20)}s)"]
    if name == "patch_file":
        return [f"      file: {args['path']}", *_preview_lines(args["old_text"], "- "), *_preview_lines(args["new_text"], "+ ")]
    if name == "write_file":
        line_count = len(str(args["content"]).splitlines())
        return [f"      file: {args['path']} ({line_count} lines)", *_preview_lines(args["content"], "| ")]
    return [f"      {middle(json.dumps(args, ensure_ascii=False), CONSOLE_TEXT_LIMIT)}"]


def _input_waiting():
    # 输入缓冲区里是否还有已经到达、没被读取的内容（粘贴多行文字时，第一行之后的行都在这里）
    if os.name == "nt":
        import msvcrt

        return msvcrt.kbhit()
    import select

    return bool(select.select([sys.stdin], [], [], 0)[0])


def _discard_pending_input():
    # 丢掉缓冲区里残留的输入，避免它被当成接下来的回答
    if os.name == "nt":
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
    elif sys.stdin.isatty():
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)


def read_user_input(prompt):
    """读取一条用户输入。粘贴多行文字时，把紧跟着到达的行合成一条，而不是拆成好几次输入。

    input() 每次只读一行；粘贴进来的其余行已经在缓冲区里，稍等片刻后把它们一并读出。
    """
    lines = [input(prompt)]
    time.sleep(PASTE_SETTLE_SECONDS)
    while _input_waiting():
        line = sys.stdin.readline()
        if not line:
            break
        lines.append(line.rstrip("\r\n"))
        time.sleep(PASTE_SETTLE_SECONDS)
    if len(lines) > 1:
        print(f"  (received {len(lines)} lines as one message)")
    return "\n".join(lines).strip()


def terminal_approver(name, args):
    """命令行的审批方式：显示要做的操作，等用户输入 y / n / a。"""
    print("\n".join(approval_preview(name, args)))
    if name in EDIT_TOOLS:
        always_hint = "a = allow all file edits in this session"
    elif name == "run_shell":
        always_hint = "a = always allow this exact command in this session"
    else:
        always_hint = f"a = always allow {name} in this session"
    # 先清掉缓冲区里的残留输入，审批只认这之后用户亲手输入的回答
    _discard_pending_input()
    while True:
        try:
            answer = input(f"    allow? [y/n/a] ({always_hint}) ").strip().lower()
        except EOFError:
            # 输入流已关闭（例如通过管道运行），按拒绝处理
            return APPROVE_NO
        if answer in ("a", "always"):
            return APPROVE_ALWAYS
        if answer in ("y", "yes"):
            return APPROVE_YES
        if answer in ("n", "no"):
            return APPROVE_NO
        # 其他输入（包括直接回车）不当作拒绝，重新询问
        print("    please type y, n or a")


def print_event(event):
    """命令行的事件订阅者：把运行过程实时打印到终端。

    只显示人关心的：模型边做边说的话、调用了什么工具、成功还是失败、运行时提示、最后的用量汇总。
    完整内容在 .locagent/runs/<run_id>.jsonl 里。
    """
    kind = event["event"]
    if kind == ev.MODEL_RESPONDED and event["tool_calls"] and event["text"].strip():
        # 带工具调用的回复里，模型顺带说明要做什么；最终回答由 main() 打印，这里不重复
        print(f"  {middle(event['text'].strip(), CONSOLE_TEXT_LIMIT)}")
    elif kind == ev.TOOL_STARTED and event["name"] == "update_todos":
        # 待办清单逐项显示，比一行 JSON 好读
        print("  > update_todos")
        for line in render_todos(event["args"].get("todos") or []).splitlines():
            print(f"      {line}")
    elif kind == ev.TOOL_STARTED:
        print(f"  > {event['name']} {_brief_args(event['args'])}")
    elif kind == ev.TOOL_FINISHED:
        # 显示的是工具真正执行的时间，不含等待审批的时间
        seconds = event["duration_ms"] / 1000
        if event["tool_status"] == "ok":
            print(f"    ok ({seconds:.1f}s)")
        else:
            # 结果原文以 "error: " 开头，前面已经显示了状态，去掉避免出现 "error: error:"
            first_line = event["result"].splitlines()[0].removeprefix("error: ") if event["result"] else ""
            print(f"    {event['tool_status']}: {middle(first_line, CONSOLE_TEXT_LIMIT)} ({seconds:.1f}s)")
    elif kind == ev.CONTEXT_REDUCED:
        before = f"last input {event['input_tokens_before']} of {event['budget']} token budget"
        if event["action"] == "compact":
            print(f"  [context] compacted {event['replaced_records']} records into a summary ({before})")
        else:
            print(f"  [context] cleared {event['cleared']} old tool results, {event['freed_chars']} chars ({before})")
    elif kind == ev.NOTICE:
        print(f"  [notice] {middle(event['content'], CONSOLE_TEXT_LIMIT)}")
    elif kind == ev.RUN_FINISHED:
        usage = event["usage"]
        print(
            f"  [{event['stop_reason']}] {event['attempts']} requests, {event['tool_steps']} tools, "
            f"{event['duration_ms'] / 1000:.1f}s | input {usage['input_tokens']}, "
            f"cache read {usage['cache_read_input_tokens']}, cache write {usage['cache_creation_input_tokens']}, "
            f"output {usage['output_tokens']}"
        )
        if event["status"] != "completed":
            print("  task not finished; type /continue to resume it")


def build_welcome(agent):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, inner)
        return f"| {body.ljust(inner)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [
        center(WELCOME_NAME),
        center(WELCOME_SUBTITLE),
        divider("-"),
        row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
        pair("MODEL", agent.model_client.model, "BRANCH", agent.workspace.branch),
        pair("APPROVAL", agent.approval_policy, "MAX STEPS", agent.max_steps),
        row(f"SESSION    {agent.session_id}" + (f" (resumed, {len(agent.history)} records)" if agent.history else "")),
        row("Type /help for commands."),
    ]
    return "\n".join([line, *rows, line])


def build_agent(args):
    # 装配顺序：工作区信息 -> 加载 .env（模型配置在里面）-> 模型客户端 -> agent
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.cwd)
    model = _build_model_client(args)
    agent = LocAgent(
        model_client=model,
        workspace=workspace,
        approval_policy=args.approval,
        approver=terminal_approver,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        context_budget=args.context_budget,
        resume=args.resume,
    )
    # 命令行订阅事件，运行过程实时显示在终端
    agent.events.subscribe(print_event)
    return agent


def add_agent_arguments(parser):
    # 命令行和网页共用的参数：工作区、模型、审批、步数、上下文预算等
    parser.add_argument("--cwd", default=".", help="Workspace directory. The agent can only access files inside it.")
    parser.add_argument("--model", default=None, help="Qwen model name. Defaults to QWEN_MODEL, then qwen3.6-plus.")
    parser.add_argument("--base-url", default=None, help="Qwen API base URL. Defaults to QWEN_API_BASE, then the Model Studio Anthropic-compatible endpoint.")
    parser.add_argument("--timeout", type=int, default=300, help="Model request timeout in seconds.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument("--max-steps", type=int, default=10, help="Maximum tool calls per request.")
    parser.add_argument("--max-new-tokens", type=int, default=8192, help="Maximum model output tokens per step (write_file content counts too).")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        help="Resume a saved session in this workspace: a session id, or no value for the most recent one.",
    )
    parser.add_argument("--context-budget", type=int, default=DEFAULT_CONTEXT_BUDGET, help="Input token budget; old tool results are cleared above 50%% and the conversation is summarized above 75%%.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="locagent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="LocAgent: a local coding agent backed by Qwen (Alibaba Cloud Model Studio, Anthropic-compatible API).",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    add_agent_arguments(parser)
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        agent = build_agent(args)
    except RuntimeError as exc:
        # 例如缺少 QWEN_API_KEY：直接提示原因，不打印调用栈
        print(str(exc), file=sys.stderr)
        return 1

    print(build_welcome(agent))
    if agent.checkpoint is not None and agent.checkpoint.unfinished:
        # 恢复的会话里最近一个任务没做完：提示可以接着做
        reason = agent.checkpoint.stop_reason or "program exited"
        print(f"\nunfinished task ({reason}): {middle(agent.checkpoint.request, CONSOLE_TEXT_LIMIT)}")
        print("type /continue to resume it")

    if args.prompt:
        # one-shot 模式：只问一次，不进入交互循环
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print(agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            except KeyboardInterrupt:
                print("\ninterrupted", file=sys.stderr)
                return 1
        return 0

    while True:
        # 交互模式：每次读取一条输入交给同一个 agent，对话历史跨轮延续
        try:
            user_input = read_user_input("\nlocagent> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue
        if user_input == "locagent" or user_input.startswith(SHELL_COMMAND_PREFIXES):
            # 在 LocAgent 里输入了启动命令：不发给模型，提示先退出
            print("this is a terminal command, but you are already inside LocAgent.")
            print("type /exit first, then run it in the terminal.")
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/history":
            print(agent.history_text())
            continue
        if user_input == "/context":
            manager = agent.context_manager
            print(f"last request input: {manager.last_input_tokens} tokens / budget {manager.budget} "
                  f"(compact above {int(manager.budget * COMPACT_RATIO)}), history records: {len(agent.history)}")
            continue
        if user_input == "/compact":
            # 压缩结果由 print_event 打印（context_reduced 事件）
            try:
                agent.compact()
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
            continue
        if user_input == "/continue":
            print()
            try:
                print(agent.continue_task())
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
            except KeyboardInterrupt:
                print("\ninterrupted", file=sys.stderr)
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print(f"new session {agent.session_id}")
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
        except KeyboardInterrupt:
            # 提问过程中按 Ctrl+C（等待模型、审批、执行工具时）：只中断这次提问，回到提示符。
            # 没有结果的工具调用会在下一次提问开始时补齐
            print("\ninterrupted", file=sys.stderr)
