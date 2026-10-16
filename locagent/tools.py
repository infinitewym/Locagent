"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
只读工具（list_files、read_file、search）和只改内存状态的 update_todos 不需要审批；
会改动工作区或执行命令的工具（write_file、patch_file、run_shell）标记为 risky，执行前需要审批。
"""

import codecs
import difflib
import json
import os
import re
import signal
import subprocess
from functools import partial

from .workspace import IGNORED_PATH_NAMES

# 每个工具的定义：description 和 input_schema 会原样作为接口的 tools 参数发给模型，
# input_schema 是标准 JSON Schema，模型按它生成参数；risky 决定执行前是否需要审批
BASE_TOOL_SPECS = {
    "list_files": {
        "description": "List files and directories in a workspace directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to the workspace root.", "default": "."},
            },
        },
        "risky": False,
    },
    "read_file": {
        "description": "Read a UTF-8 file by line range. Each line is shown as 'N: text'; the 'N: ' prefix is not part of the file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the workspace root."},
                "start": {"type": "integer", "description": "First line to read, 1-based.", "default": 1},
                "end": {"type": "integer", "description": "Last line to read, inclusive.", "default": 200},
            },
            "required": ["path"],
        },
        "risky": False,
    },
    "search": {
        "description": "Search text in workspace files and return matching lines as 'path:line: text'. Case-insensitive. Use this instead of run_shell for finding code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text to search for."},
                "path": {"type": "string", "description": "File or directory to search in, relative to the workspace root.", "default": "."},
                "regex": {"type": "boolean", "description": "Treat pattern as a Python regular expression.", "default": False},
            },
            "required": ["pattern"],
        },
        "risky": False,
    },
    "write_file": {
        "description": "Create or overwrite a text file with the given content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the workspace root."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
        "risky": True,
    },
    "patch_file": {
        "description": "Replace one exact text block in a file. old_text must occur exactly once and must be raw file text without the 'N: ' prefix shown by read_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the workspace root."},
                "old_text": {"type": "string", "description": "Exact text to replace."},
                "new_text": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_text", "new_text"],
        },
        "risky": True,
    },
    "update_todos": {
        "description": (
            "Replace your todo list for the current task. Use it for tasks with 3 or more steps: "
            "list the steps, keep exactly one item in_progress while working, and mark items completed as soon as they are done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The full todo list; it replaces the previous one.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "What to do."},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
        "risky": False,
    },
    "run_shell": {
        "description": (
            "Run a single-line shell command in the workspace root and return exit_code, stdout and stderr. "
            "Use it to run code, tests and linters. For multi-line code, write a script with write_file first, then run it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Single-line command to run."},
                "timeout": {"type": "integer", "description": "Timeout in seconds, 1 to 120.", "default": 20},
            },
            "required": ["command"],
        },
        "risky": True,
    },
}

# search 最多返回的匹配行数、每行最多显示的字符数、跳过的大文件阈值
SEARCH_MAX_MATCHES = 200
SEARCH_LINE_LIMIT = 200
SEARCH_MAX_FILE_BYTES = 1_000_000
RUN_SHELL_MAX_TIMEOUT = 120
TODO_STATUSES = ("pending", "in_progress", "completed")
# 待办清单显示时每种状态的标记
TODO_MARKS = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
# 命令输出过长时保留开头和结尾：报错堆栈、测试汇总通常在最后，不能被截掉
SHELL_OUTPUT_HEAD = 1000
SHELL_OUTPUT_TAIL = 2800


def legal_tool_names():
    return set(BASE_TOOL_SPECS)


# 参数出错时附在错误信息里的正确示例
TOOL_EXAMPLES = {
    "list_files": {"path": "."},
    "read_file": {"path": "README.md", "start": 1, "end": 80},
    "search": {"pattern": "def main", "path": "."},
    "write_file": {"path": "hello.py", "content": "print('hello')\n"},
    "patch_file": {"path": "hello.py", "old_text": "hello", "new_text": "hi"},
    "run_shell": {"command": "python -m pytest -q", "timeout": 60},
    "update_todos": {"todos": [{"content": "Read calc.py", "status": "completed"}, {"content": "Fix add()", "status": "in_progress"}]},
}


def build_tool_registry(context, allowed_tools=None):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    # allowed_tools 为 None 表示全部开放；否则只开放列出的工具（评测任务会用到）
    names = list(BASE_TOOL_SPECS) if allowed_tools is None else list(allowed_tools)
    unknown = [name for name in names if name not in BASE_TOOL_SPECS]
    if unknown:
        raise ValueError(f"unknown tool: {', '.join(unknown)}")
    return {
        name: {**BASE_TOOL_SPECS[name], "run": partial(_TOOL_RUNNERS[name], context)}
        for name in names
    }


def tool_example(name):
    example = TOOL_EXAMPLES.get(name)
    return json.dumps(example, ensure_ascii=False) if example else ""


def tool_definitions(tools):
    # 转成接口 tools 参数需要的格式
    return [
        {"name": name, "description": tool["description"], "input_schema": tool["input_schema"]}
        for name, tool in tools.items()
    ]


def _file_encoding(path):
    # Windows 上记事本、PowerShell 写的 UTF-8 文件开头常带 BOM（不可见的 \ufeff）。
    # 这类文件用 utf-8-sig 读写：读的时候去掉 BOM，模型看到的第一行和文件实际内容一致；写回时保留 BOM
    with path.open("rb") as handle:
        return "utf-8-sig" if handle.read(3) == codecs.BOM_UTF8 else "utf-8"


def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        if not str(args.get("pattern", "")):
            raise ValueError("pattern must not be empty")
        path = context.path(args.get("path", "."))
        if not path.exists():
            raise ValueError("path does not exist")
        if args.get("regex"):
            re.compile(args["pattern"])
        return

    if name == "run_shell":
        command = str(args.get("command", ""))
        if not command.strip():
            raise ValueError("command must not be empty")
        if os.name == "nt" and "\n" in command.strip():
            # cmd.exe 把换行当作命令结束，只执行第一行，后面的内容被丢掉，退出码却是 0，
            # 模型会误以为执行成功。直接拒绝，让模型改为先写脚本再运行
            raise ValueError(
                "cmd.exe runs only the first line of a multi-line command; "
                "write the code to a .py file with write_file, then run it with python"
            )
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > RUN_SHELL_MAX_TIMEOUT:
            raise ValueError(f"timeout must be in [1, {RUN_SHELL_MAX_TIMEOUT}]")
        return

    if name == "update_todos":
        todos = args.get("todos")
        if not isinstance(todos, list):
            raise ValueError("todos must be a list")
        for item in todos:
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                raise ValueError("each todo needs a non-empty content")
            if item.get("status") not in TODO_STATUSES:
                raise ValueError(f"status must be one of {', '.join(TODO_STATUSES)}")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding=_file_encoding(path))
        count = text.count(old_text)
        if count != 1:
            raise ValueError(_old_text_count_message(old_text, count, text))
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


# read_file 输出每行的格式是 f"{行号:>4}: {内容}"，模型常把这个前缀一起抄进 old_text
READ_FILE_LINE_PREFIX = re.compile(r"^\s*\d+: ")


# 匹配不到时找“最相近的一行”的相似度下限，低于它说明 old_text 和文件差别太大，不给提示
CLOSEST_LINE_CUTOFF = 0.6


def _closest_line_hint(old_text, text):
    # old_text 里第一行在文件中找不到的内容，和文件里最相近的一行对照显示。
    # 模型常把中文引号“”写成英文引号、改动空格或缩进，肉眼难看出来，并排给出就能直接看到差在哪个字符
    lines = text.splitlines()
    for wanted in old_text.splitlines():
        if not wanted.strip() or wanted in lines:
            continue
        close = difflib.get_close_matches(wanted, lines, n=1, cutoff=CLOSEST_LINE_CUTOFF)
        if not close:
            return ""
        number = lines.index(close[0]) + 1
        # 模型常常写不出原文里的某些字符（例如中文引号），只告诉差别不够：
        # 2026-10-16 评测中它为了绕开这一行，改用 write_file 重写整个文件，顺带改坏了原文。
        # 所以同时给出可行的做法：换一段不包含这一行的更短 old_text
        return (
            f"; this old_text line is not in the file: {wanted!r}; closest file line {number}: {close[0]!r}"
            "; copy that line exactly, or if you cannot reproduce it, choose a shorter old_text that does not include it"
            " (do not rewrite the whole file to avoid it)"
        )
    return ""


def _old_text_count_message(old_text, count, text):
    message = f"old_text must occur exactly once, found {count}"
    if count == 0 and READ_FILE_LINE_PREFIX.match(old_text):
        # 匹配不到且开头像行号前缀时，直接说明原因，模型不用再猜
        message += "; old_text starts with a read_file line-number prefix like '   2: ', copy only the text after the prefix"
    elif count == 0:
        message += _closest_line_hint(old_text, text)
    return message


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding=_file_encoding(path), errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    # 读写用同一种编码：原文件带 BOM，改完写回时也保留 BOM
    encoding = _file_encoding(path)
    text = path.read_text(encoding=encoding)
    count = text.count(old_text)
    if count != 1:
        raise ValueError(_old_text_count_message(old_text, count, text))
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding=encoding)
    return f"patched {path.relative_to(context.root)}"


def _search_files(path, root):
    # 要搜索的文件：给的是文件就只搜它，给的是目录就递归，跳过 .git、.locagent 等目录
    if path.is_file():
        return [path]
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(root).parts)
    )


def tool_search(context, args):
    pattern = str(args["pattern"])
    matcher = re.compile(pattern, re.IGNORECASE) if args.get("regex") else None
    needle = pattern.lower()
    matches = []
    for file_path in _search_files(context.path(args.get("path", ".")), context.root):
        if file_path.stat().st_size > SEARCH_MAX_FILE_BYTES:
            continue
        data = file_path.read_bytes()
        if b"\0" in data:
            # 含空字节的视为二进制文件，不搜索
            continue
        relative = file_path.relative_to(context.root).as_posix()
        text = data.decode("utf-8-sig" if data.startswith(codecs.BOM_UTF8) else "utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            found = matcher.search(line) if matcher else needle in line.lower()
            if found:
                matches.append(f"{relative}:{number}: {line.strip()[:SEARCH_LINE_LIMIT]}")
                if len(matches) >= SEARCH_MAX_MATCHES:
                    return "\n".join(matches) + f"\n...[stopped at {SEARCH_MAX_MATCHES} matches; narrow the pattern or path]"
    return "\n".join(matches) or "(no matches)"


def _kill_process_tree(process):
    # 超时后结束整个进程树：shell=True 时直接 kill 只会结束外层的 shell，里面的 python 等子进程还会继续运行
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
    else:
        os.killpg(process.pid, signal.SIGKILL)


def tool_run_shell(context, args):
    command = str(args["command"])
    timeout = int(args.get("timeout", 20))
    if os.name == "nt":
        # Windows 的 cmd 默认用 GBK 输出，先切到 UTF-8 代码页，dir、type 等内置命令的中文才不会乱码
        command = f"chcp 65001 >nul && {command}"
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=context.root,
        # 只传过滤后的环境变量，命令里读不到 QWEN_API_KEY 等密钥
        env=context.shell_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # 非 Windows 系统上让命令自成一个进程组，超时时可以整组结束
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        process.communicate()
        raise TimeoutError(f"command timed out after {timeout}s") from None
    stdout = stdout.decode("utf-8", errors="replace").strip()
    stderr = stderr.decode("utf-8", errors="replace").strip()
    output = f"exit_code: {process.returncode}\nstdout:\n{stdout or '(empty)'}\nstderr:\n{stderr or '(empty)'}"
    if len(output) <= SHELL_OUTPUT_HEAD + SHELL_OUTPUT_TAIL:
        return output
    omitted = len(output) - SHELL_OUTPUT_HEAD - SHELL_OUTPUT_TAIL
    return output[:SHELL_OUTPUT_HEAD] + f"\n...[{omitted} chars omitted]...\n" + output[-SHELL_OUTPUT_TAIL:]


def render_todos(todos):
    # 待办清单的文字形式：返回给模型、显示在终端、写进 /continue 的说明
    return "\n".join(f"{TODO_MARKS[item['status']]} {item['content']}" for item in todos) or "(empty)"


def tool_update_todos(context, args):
    # 原地替换：agent 持有的是同一个列表，检查点保存时直接读取
    context.todos[:] = [{"content": str(item["content"]).strip(), "status": item["status"]} for item in args["todos"]]
    return "todo list updated:\n" + render_todos(context.todos)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "run_shell": tool_run_shell,
    "update_todos": tool_update_todos,
}
