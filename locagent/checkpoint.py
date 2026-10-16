"""任务检查点：任务做到一半中断后，记录进度并接着做。

会话文件已经保存了完整历史（第 8 步），恢复会话后模型能看到之前做过什么。
检查点补上的是“这个任务是否做完、停在哪、之后文件有没有被改过”：
- 每执行完一步工具、每次提问结束（包括中断、出错、步数用完），都写一个 checkpoint 事件进会话文件
- 恢复会话时取最后一个检查点；状态不是 completed 就说明任务没做完，可以用 /continue 接着做
- 接着做的时候，把检查点整理成一段说明发给模型：原始请求、停止原因、模型最后写的进度、
  已经做过的步骤，以及中断之后哪些文件被改过（需要重新读）

参考 pico 的 checkpoint：保留“记录进度、检查文件是否变化”的思路；
pico 的 runtime_identity 校验和多种恢复状态这里不需要，会话文件本来就在同一个工作区里。
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from .tools import render_todos
from .workspace import clip

STATUS_RUNNING = "running"
# 进度说明、每一步参数的显示长度，以及最多保留的步骤数
PROGRESS_LIMIT = 300
STEP_ARGS_LIMIT = 80
MAX_STEPS_KEPT = 30
# 这些工具的 path 参数指向的文件需要记录内容指纹：读过的文件被改了，之前读到的内容就过时了
PATH_TOOLS = ("read_file", "write_file", "patch_file")

CONTINUE_INSTRUCTIONS = (
    "Continue this unfinished task from where it stopped. "
    "Do not repeat steps that already succeeded, unless the files they depend on changed."
)


def file_fingerprint(path):
    # 文件内容的指纹；文件不存在时为空字符串
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Checkpoint:
    run_id: str
    # 任务的原始请求；/continue 接着做时沿用同一个请求和已做的步骤，再次中断还能继续
    request: str
    status: str = STATUS_RUNNING
    stop_reason: str = ""
    # 模型最近一次在调用工具前写的进度说明
    progress: str = ""
    # 已经执行的步骤，每条形如 "patch_file {"path": "calc.py", ...}: ok"
    steps: list = field(default_factory=list)
    # 涉及到的文件及其内容指纹，{相对路径: sha256}
    files: dict = field(default_factory=dict)
    # 模型用 update_todos 维护的待办清单，保存检查点时从 agent 复制过来
    todos: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    def to_dict(self):
        return asdict(self)

    @property
    def unfinished(self):
        return self.status != "completed"

    def record_progress(self, text):
        # 模型这一轮写了文字才更新，没写则保留上一次的进度
        text = " ".join(str(text).split())
        if text:
            self.progress = text[:PROGRESS_LIMIT]

    def record_step(self, name, args, tool_status, affected_paths, context):
        # 更新待办清单不算任务步骤，清单本身单独保存在 todos 里
        if name == "update_todos":
            return
        brief = clip(json.dumps(args, ensure_ascii=False), STEP_ARGS_LIMIT)
        self.steps.append(f"{name} {brief}: {tool_status}")
        del self.steps[:-MAX_STEPS_KEPT]
        paths = list(affected_paths)
        if name in PATH_TOOLS and "path" in args:
            paths.append(str(args["path"]))
        for raw_path in paths:
            # 越界或无效的路径已经被工具拒绝过，这里跳过
            try:
                path = context.path(raw_path)
            except ValueError:
                continue
            self.files[path.relative_to(context.root).as_posix()] = file_fingerprint(path)

    def changed_files(self, context):
        # 和记录时相比内容变了（或被删除）的文件
        return sorted(
            relative for relative, fingerprint in self.files.items()
            if file_fingerprint(context.root / relative) != fingerprint
        )

    def continue_message(self, context):
        """生成 /continue 时发给模型的说明。"""
        lines = [
            "<checkpoint>",
            f"Original request: {self.request}",
            f"Stopped because: {self.stop_reason or 'the program exited before the task finished'}",
            f"Last progress note: {self.progress or '-'}",
            "Todo list:",
            render_todos(self.todos),
            "Steps already taken (oldest first):",
            *(f"- {step}" for step in self.steps),
        ]
        if not self.steps:
            lines.append("- none")
        changed = self.changed_files(context)
        if changed:
            lines.append("Files changed since these steps (re-read them before relying on earlier content):")
            lines.extend(f"- {path}" for path in changed)
        lines.append("</checkpoint>")
        lines.append(CONTINUE_INSTRUCTIONS)
        return "\n".join(lines)
