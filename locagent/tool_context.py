"""传给工具函数的最小上下文。

工具函数不直接接触 agent，只通过这里拿到它需要的东西：
工作区根目录、把模型给的路径解析成工作区内绝对路径的方法、执行命令时用的环境变量，
以及模型维护的待办清单（update_todos 工具读写，和 agent 共用同一个列表）。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolContext:
    root: Path
    # run_shell 执行命令时使用的环境变量（已按白名单过滤，见 security.shell_env）
    shell_env: dict = field(default_factory=dict)
    # 待办清单，每项 {"content": 内容, "status": pending / in_progress / completed}
    todos: list = field(default_factory=list)

    def __post_init__(self):
        # 统一成绝对路径，后面比较"是否在工作区内"才可靠
        self.root = Path(self.root).resolve()

    def path(self, raw_path):
        path = Path(str(raw_path))
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在工作区根目录之下：
        # 既能挡住 "../" 逃逸，也能挡住符号链接解析后跳出工作区。
        # 注意 run_shell 不受这个限制，命令本身可以访问工作区外，所以它必须经过审批
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved
