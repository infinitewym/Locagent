"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的“仓库第一印象”。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import subprocess
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份“导航包”。
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
IGNORED_PATH_NAMES = {".git", ".locagent", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


class WorkspaceContext:
    def __init__(self, cwd, branch, default_branch, status, recent_commits, project_docs):
        # cwd 就是工作区根目录：启动时指定哪个目录，agent 就只在这个目录里工作
        self.cwd = cwd
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs

    @classmethod
    def build(cls, cwd):
        cwd = Path(cwd).resolve()

        def git(args, fallback=""):
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        docs = {}
        for name in DOC_NAMES:
            path = cwd / name
            if path.is_file():
                docs[name] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)

        # 只有工作区本身就是 git 仓库根目录时才收集 git 信息；
        # 工作区是某个仓库的子目录时，git 会报告外层仓库的状态，和当前工作区无关
        toplevel = git(["rev-parse", "--show-toplevel"])
        if not toplevel or Path(toplevel).resolve() != cwd:
            return cls(
                cwd=str(cwd),
                branch="-",
                default_branch="-",
                status="not a git repository",
                recent_commits=[],
                project_docs=docs,
            )

        return cls(
            cwd=str(cwd),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(
                lambda branch: branch[len("origin/") :] if branch.startswith("origin/") else branch
            )(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
        )

    def text(self):
        # 这段文本放进 prompt prefix，只包含稳定的内容。
        # git status 会随 agent 写文件而变化，单独由 status_text() 输出，放在 prefix 之后，
        # 否则每次改文件都会让 prefix 变化，缓存失效
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        # 逐行拼接：文档内容本身是多行文本，放进 dedent 模板会让模板行的缩进去不掉
        return "\n".join(
            [
                "Workspace:",
                f"- root: {self.cwd}",
                f"- branch: {self.branch}",
                f"- default_branch: {self.default_branch}",
                "- recent_commits:",
                commits,
                "- project_docs:",
                docs,
            ]
        )

    def status_text(self):
        # 工作区里会变化的部分：未提交的改动。附在每次用户提问的开头，不放进 system prompt
        return f"<workspace_status>\n{self.status}\n</workspace_status>"

    def fingerprint(self):
        # 只覆盖 text() 里的稳定内容：指纹不变，prefix 就不变
        payload = {
            "cwd": self.cwd,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "recent_commits": list(self.recent_commits),
            "project_docs": dict(self.project_docs),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
