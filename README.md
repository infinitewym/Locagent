# LocAgent

一个在本地运行的编程 agent。它能读写工作区里的文件、搜索代码、运行命令和测试，并根据结果继续修改，直到任务完成。模型使用通义千问（阿里云百炼的 Anthropic 兼容接口），通过接口原生的工具调用来使用工具。

提供三种使用方式：

- **命令行**：`uv run locagent`
- **网页界面**：`uv run locagent-web`
- **评测**：`uv run locagent-eval`。用固定的任务集反复运行，给出可以前后对比的分数。

## 功能

- **工具**：`list_files`、`read_file`、`search`、`write_file`、`patch_file`、`run_shell`、`update_todos`。文件类工具只能访问工作区内的路径。
- **审批**：写文件、改文件、运行命令前需要确认，可以选允许、拒绝，或者本会话内总是允许。
- **安全**：
  - 运行命令时只传入白名单里的环境变量，命令读不到 API key 等密钥。
  - 工具结果里如果出现密钥，会被替换掉。
  - 改文件失败时自动回滚。
- **上下文管理**：按实际输入 token 分两级处理。超过预算的 50% 时，清理旧的工具结果；超过 75% 时，把历史压缩成摘要。也可以手动压缩。
- **会话保存与恢复**：每个会话保存为一个 JSONL 文件（`.locagent/sessions/`），用 `--resume` 恢复。
- **检查点**：任务被中断、出错或者步数用完时，会记下进度、已做的步骤、相关文件的指纹和待办清单，之后可以用 `/continue` 接着做。
- **可观测**：运行中的每个关键节点都会发出事件。命令行和网页实时显示这些事件，同时写入会话文件，方便复盘。
- **提示词缓存**：利用接口的显式缓存，在评测中平均缓存命中率约 75%~80%，单个长任务可以到 90% 以上。

## 安装

需要 Python 3.10 及以上，以及 [uv](https://docs.astral.sh/uv/)。

```
git clone https://github.com/infinitewym/locagent.git
cd locagent
uv sync
```

复制配置文件，然后在 `.env` 里填写阿里云百炼的 API key：

```
copy .env.example .env
```

| 变量 | 说明 |
|---|---|
| `QWEN_API_KEY` | 百炼 API key（必填） |
| `QWEN_API_BASE` | 接口地址，默认 `https://dashscope.aliyuncs.com/apps/anthropic` |
| `QWEN_MODEL` | 模型名，默认 `qwen3.6-plus` |
| `SECRET_ENV_NAMES` | 额外需要隐藏的环境变量名，用逗号分隔 |

`.env` 已经写在 `.gitignore` 里，不会被提交。

## 使用

### 命令行

```
uv run locagent --cwd 工作区路径
```

`--cwd` 指定 agent 能访问的工作区目录。不填时使用当前目录。

常用参数：

| 参数 | 说明 |
|---|---|
| `--max-steps` | 每次提问最多调用多少次工具，默认 10。update_todos 不计入 |
| `--approval` | 审批方式：`ask`（逐个询问，默认）、`auto`（全部同意）、`never`（全部拒绝） |
| `--resume` | 恢复会话：不带值时恢复最近一次，也可以指定会话 id |
| `--context-budget` | 上下文预算（输入 token 数），默认 64000 |
| `--max-new-tokens` | 单次回复的最大输出 token 数，默认 8192 |

交互模式下的命令：

| 命令 | 作用 |
|---|---|
| `/help` | 显示帮助 |
| `/history` | 查看模型看到的对话历史 |
| `/context` | 查看上一次请求占用了多少上下文 |
| `/compact` | 手动把历史压缩成摘要 |
| `/continue` | 接着做上一个没完成的任务 |
| `/session` | 显示当前会话文件 |
| `/reset` | 开始新会话（当前会话会保留，可以恢复） |
| `/exit` | 退出 |

审批时输入 `y` 允许、`n` 拒绝、`a` 本会话总是允许。运行中按 Ctrl+C 中断，之后可以用 `/continue` 接着做。

### 网页界面

```
uv run locagent-web --cwd 工作区路径
```

然后用浏览器打开 http://127.0.0.1:8765 。命令行的参数在这里也都可以用，另外有 `--host` 和 `--port` 两个参数。

- **左栏**：会话列表，没做完的会话会标"未完成"。可以新建会话，也可以切换到其他会话。
- **中间**：
  - 对话过程实时显示：模型的进度说明、可以展开的工具调用卡片、最终回答（支持 Markdown）。
  - 需要审批时会弹出审批卡片，改文件时用颜色区分改前和改后的内容。
- **右栏**：待办清单、上下文用量、本会话的 token 统计，以及"压缩上下文""接着做"两个按钮。
- **停止按钮**：运行中可以随时点击，效果和命令行按 Ctrl+C 一样。

后端使用 FastAPI，用 SSE 把事件推送给浏览器。页面刷新或者断线重连后，会先收到当前会话的完整记录，所以显示内容和刷新前一致。

默认只监听 `127.0.0.1`，只有本机能访问。agent 可以修改文件、运行命令，所以不要把它暴露到局域网或公网。

### 评测

```
uv run locagent-eval --check-solutions
uv run locagent-eval --trials 3
uv run locagent-eval --trials 1 --tasks sample_replace bom_edit
```

- `--check-solutions`：不调用模型，只检查任务本身是否有效。原始代码应该判分不通过，套上参考答案应该判分通过。
- `--trials 3`：每个任务跑 3 次。
- `--tasks`：只跑指定的任务。

每个任务放在 `evals/tasks/<任务id>/` 下：

- `task.json`：请求内容、步数上限、类型（capability 能力题 / regression 回归题）。
- `repo/`：工作区的初始文件。
- `check.py`：判分脚本。它放在工作区外面，agent 看不到。
- `solution/`：参考答案。

每次试验都从一份干净的副本开始，审批全部自动同意。判分只看运行结束后的文件和最终回答，不看中间走了哪些步骤。结果写在 `artifacts/evals/<时间>/` 下，内容包括：

- 每次试验的工作区和会话记录。
- `results.jsonl`：每次试验一行明细。
- `summary.json`：汇总，包括通过率、pass@k、pass^k、各任务的平均步数和 token 用量、失败类型，以及"声称完成但判分失败"的次数。

目前有 12 个任务：8 个回归题，4 个能力题。能力题包括只给现象的跨文件调试、在旧代码上加功能、涉及 13 个文件的改名，以及按文档实现函数。最近一次全量评测（qwen3.6-plus，每个任务跑 3 次）36 次全部通过。

## 项目结构

```
locagent/
  cli.py               命令行入口：参数、终端显示、审批交互
  runtime.py           LocAgent：对话历史、消息转换、审批、会话
  agent_loop.py        主循环：请求模型 -> 执行工具 -> 记录结果
  tools.py             工具定义和实现
  tool_executor.py     工具执行：参数校验、审批、失败回滚、密钥脱敏
  tool_context.py      工具可用的上下文（工作区根目录、环境变量、待办清单）
  prompt_prefix.py     system prompt
  workspace.py         工作区信息（git 状态、项目文档）
  context_manager.py   上下文清理与压缩
  session_store.py     会话文件的读写与重放
  checkpoint.py        任务检查点
  events.py            事件总线
  task_state.py        单次提问的计数和状态
  security.py          环境变量白名单、密钥识别与替换
  config.py            读取 .env
  evaluation.py        评测运行器
  providers/
    qwen.py            通义千问接口（重试、显式缓存、截断识别）
    clients.py         测试用的假模型
    types.py           ModelResponse、ToolCall
  web/
    server.py          网页后端（FastAPI + SSE）
    static/            网页前端（index.html、style.css、app.js）
evals/tasks/           评测任务
docs/                  评测中发现的问题和修复记录
```

## 许可证

[MIT](LICENSE)
