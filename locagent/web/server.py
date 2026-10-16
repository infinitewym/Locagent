"""网页界面的后端：FastAPI 服务。

网页和命令行一样，只是 LocAgent 的另一个“事件订阅者 + 审批者”，runtime 不需要为它改动：
- 事件订阅者：agent 发出的每个事件推送给浏览器（SSE，服务端推送），页面据此实时显示过程
- 审批者：高风险工具需要审批时，页面弹出审批卡片，用户点按钮后结果通过 POST 回传

推送用 SSE 而不是 WebSocket：数据主要是服务端单向推给页面，用户操作（提问、审批、停止）走普通 POST，
SSE 断线后浏览器会自动重连，实现更简单。

agent 的运行是同步阻塞的（请求模型、执行工具、等待审批），放在单独的线程里跑；
事件从这个线程通过 loop.call_soon_threadsafe 交给异步的推送队列。

一个服务进程对应一个工作区（--cwd）和一个当前会话，可以在页面上切换到工作区里的其他会话。

启动：uv run locagent-web --cwd sandbox，然后浏览器打开 http://127.0.0.1:8765
"""

import argparse
import asyncio
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import events as ev
from ..cli import _build_model_client, add_agent_arguments, approval_preview
from ..config import load_project_env
from ..context_manager import CLEAR_RATIO, COMPACT_RATIO
from ..runtime import APPROVE_ALWAYS, APPROVE_NO, APPROVE_YES, LocAgent
from ..workspace import WorkspaceContext

STATIC_DIR = Path(__file__).resolve().parent / "static"
# SSE 心跳间隔（秒）：长时间没有事件时发一行注释，防止连接被中间设备当作空闲断开
KEEPALIVE_SECONDS = 15
# 会话列表里标题（第一次提问）的最大长度
SESSION_TITLE_LIMIT = 60
# 审批等待时检查“停止”的间隔（秒）
APPROVAL_POLL_SECONDS = 0.2
APPROVAL_ANSWERS = (APPROVE_YES, APPROVE_NO, APPROVE_ALWAYS)


def _for_page(event):
    # history_recorded 是给恢复会话用的，页面只需要其中的工具结果全文（工具卡片展开时显示），其他不推送
    if event["event"] != ev.HISTORY_RECORDED:
        return True
    return event["record"]["role"] == "tool"


def _session_summary(path):
    # 会话列表的一行：标题取第一次提问，未完成取最后一个检查点的状态
    title = ""
    unfinished = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event["event"] == ev.RUN_STARTED and not title:
            title = event["user_request"].strip().splitlines()[0][:SESSION_TITLE_LIMIT]
        elif event["event"] == ev.CHECKPOINT:
            unfinished = event["checkpoint"]["status"] != "completed"
    return {
        "id": path.stem,
        "title": title or "(empty session)",
        "updated": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "unfinished": unfinished,
    }


class WebHost:
    """持有 agent，把它的事件推给所有打开的页面，并把页面的操作交给 agent。"""

    def __init__(self, args):
        self.args = args
        self.loop = None
        # 每个打开的页面一个推送队列
        self.clients = set()
        # 当前会话里推送给页面的事件（已过滤），新打开的页面先收到这份快照再接收实时事件
        self.events = []
        # 保护 events 和 clients：快照和实时事件的先后顺序必须一致，否则页面上会重复或缺失
        self.lock = threading.Lock()
        self.busy = False
        # 等待用户回答的审批：{审批 id: {"name", "args", "preview", "done": threading.Event, "answer"}}
        self.approvals = {}
        self.agent = None
        self._open(args.resume)

    # ---------- agent 和会话 ----------

    def _open(self, resume):
        # 创建 agent：resume 为 None 时开始新会话，否则恢复指定会话（"latest" 为最近一次）
        workspace = WorkspaceContext.build(self.args.cwd)
        agent = LocAgent(
            model_client=_build_model_client(self.args),
            workspace=workspace,
            approval_policy=self.args.approval,
            approver=self.approve,
            max_steps=self.args.max_steps,
            max_new_tokens=self.args.max_new_tokens,
            context_budget=self.args.context_budget,
            resume=resume,
        )
        with self.lock:
            self.agent = agent
            self.events = [event for event in agent.session_store.load(agent.session_id) if _for_page(event)]
        agent.events.subscribe(self._on_event)
        self._push_snapshot()

    def _on_event(self, event):
        # agent 线程里调用：记进快照，推给所有页面
        if not _for_page(event):
            return
        with self.lock:
            self.events.append(event)
            self._push_locked({"type": "event", "event": event})

    def state(self):
        agent = self.agent
        checkpoint = agent.checkpoint
        return {
            "workspace": str(agent.root),
            "model": agent.model_client.model,
            "session_id": agent.session_id,
            "busy": self.busy,
            "approval_policy": agent.approval_policy,
            "max_steps": agent.max_steps,
            "budget": agent.context_manager.budget,
            "clear_ratio": CLEAR_RATIO,
            "compact_ratio": COMPACT_RATIO,
            "last_input_tokens": agent.context_manager.last_input_tokens,
            "unfinished": checkpoint is not None and checkpoint.unfinished,
            "todos": list(agent.todos),
        }

    def sessions(self):
        root = self.agent.session_store.root
        files = sorted(root.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        current = self.agent.session_id
        return [{**_session_summary(path), "current": path.stem == current} for path in files]

    def new_session(self):
        self._ensure_idle()
        # reset() 会发出 session_started 事件；先清空快照，页面收到新快照后重新渲染
        with self.lock:
            self.events = []
        self.agent.reset()
        self._push_snapshot()

    def open_session(self, session_id):
        self._ensure_idle()
        if not self.agent.session_store.path(session_id).exists():
            raise HTTPException(404, f"session not found: {session_id}")
        self._open(session_id)

    # ---------- 推送 ----------

    def _push_locked(self, message):
        # 调用方已持有 self.lock。队列属于事件循环，跨线程只能通过 call_soon_threadsafe 放入
        for queue in self.clients:
            self.loop.call_soon_threadsafe(queue.put_nowait, message)

    def push(self, message):
        with self.lock:
            self._push_locked(message)

    def _snapshot_locked(self):
        pending = [
            {"id": approval_id, "name": item["name"], "args": item["args"], "preview": item["preview"]}
            for approval_id, item in self.approvals.items()
            if item["answer"] is None
        ]
        return {"type": "snapshot", "state": self.state(), "events": list(self.events), "approvals": pending}

    def _push_snapshot(self):
        with self.lock:
            self._push_locked(self._snapshot_locked())

    def connect(self):
        # 新页面：注册队列，并把快照作为第一条消息，之后才会收到实时事件
        queue = asyncio.Queue()
        # 记下事件循环：agent 线程往推送队列里放消息时要用到；在页面连接前产生的事件只进快照，不需要它
        self.loop = asyncio.get_running_loop()
        with self.lock:
            self.clients.add(queue)
            queue.put_nowait(self._snapshot_locked())
        return queue

    def disconnect(self, queue):
        with self.lock:
            self.clients.discard(queue)

    # ---------- 运行 ----------

    def _ensure_idle(self):
        if self.busy:
            raise HTTPException(409, "agent is busy; stop the current task first")

    def start(self, job):
        # 在后台线程里执行一次 agent 操作（提问、接着做、压缩），同一时间只允许一个
        with self.lock:
            if self.busy:
                raise HTTPException(409, "agent is busy; stop the current task first")
            self.busy = True
            self._push_locked({"type": "state", "state": self.state()})
        threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job):
        try:
            job()
        except KeyboardInterrupt:
            # 停止：主循环已经记下 interrupted，页面通过 run_finished 事件看到
            pass
        except RuntimeError as exc:
            # 模型请求失败等：主循环已发出 model_failed 事件，这里再把原因单独推一次，压缩失败时也能看到
            self.push({"type": "error", "message": str(exc)})
        finally:
            with self.lock:
                self.busy = False
                self._push_locked({"type": "state", "state": self.state()})

    def ask(self, text):
        self.start(lambda: self.agent.ask(text))

    def continue_task(self):
        if self.agent.checkpoint is None or not self.agent.checkpoint.unfinished:
            raise HTTPException(409, "no unfinished task to continue")
        self.start(self.agent.continue_task)

    def compact(self):
        self.start(self.agent.compact)

    def stop(self):
        # 主循环在下一次请求模型或执行工具前停下；正在等待的审批按“停止”处理
        self.agent.stop_event.set()
        with self.lock:
            for item in self.approvals.values():
                if item["answer"] is None:
                    item["done"].set()

    # ---------- 审批 ----------

    def approve(self, name, args):
        """agent 线程里调用：推送审批卡片，阻塞等待页面回答。"""
        approval_id = uuid.uuid4().hex[:12]
        # 复用命令行的审批预览，去掉终端里用于对齐的缩进
        preview = "\n".join(line.removeprefix("      ") for line in approval_preview(name, args))
        item = {"name": name, "args": args, "preview": preview, "done": threading.Event(), "answer": None}
        with self.lock:
            self.approvals[approval_id] = item
            self._push_locked({"type": "approval_request", "id": approval_id, "name": name, "args": args, "preview": preview})
        # 分段等待，便于响应停止
        while not item["done"].wait(APPROVAL_POLL_SECONDS):
            pass
        with self.lock:
            del self.approvals[approval_id]
        if item["answer"] is None:
            # 等待期间点了停止：和命令行在审批时按 Ctrl+C 一样
            raise KeyboardInterrupt
        return item["answer"]

    def answer(self, approval_id, answer):
        if answer not in APPROVAL_ANSWERS:
            raise HTTPException(400, f"answer must be one of {APPROVAL_ANSWERS}")
        with self.lock:
            item = self.approvals.get(approval_id)
            if item is None or item["answer"] is not None:
                raise HTTPException(404, "approval not found or already answered")
            item["answer"] = answer
            item["done"].set()
            self._push_locked({"type": "approval_resolved", "id": approval_id, "answer": answer})


def create_app(host):
    app = FastAPI(title="LocAgent")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def get_state():
        return host.state()

    @app.get("/api/sessions")
    async def list_sessions():
        return host.sessions()

    @app.post("/api/sessions/new")
    async def new_session():
        host.new_session()
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/open")
    async def open_session(session_id: str):
        host.open_session(session_id)
        return {"ok": True}

    @app.post("/api/ask")
    async def ask(request: Request):
        text = str((await request.json()).get("text", "")).strip()
        if not text:
            raise HTTPException(400, "text must not be empty")
        host.ask(text)
        return {"ok": True}

    @app.post("/api/continue")
    async def continue_task():
        host.continue_task()
        return {"ok": True}

    @app.post("/api/compact")
    async def compact():
        host.compact()
        return {"ok": True}

    @app.post("/api/stop")
    async def stop():
        host.stop()
        return {"ok": True}

    @app.post("/api/approvals/{approval_id}")
    async def answer_approval(approval_id: str, request: Request):
        host.answer(approval_id, str((await request.json()).get("answer", "")))
        return {"ok": True}

    @app.get("/api/stream")
    async def stream(request: Request):
        queue = host.connect()

        async def messages():
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
            finally:
                host.disconnect(queue)

        return StreamingResponse(messages(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="locagent-web",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="LocAgent web UI.",
    )
    add_agent_arguments(parser)
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on. Keep 127.0.0.1 so only this computer can reach it.")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on.")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    load_project_env(args.cwd)
    try:
        host = WebHost(args)
    except RuntimeError as exc:
        # 例如缺少 QWEN_API_KEY
        print(str(exc))
        return 1
    print(f"LocAgent web: http://{args.host}:{args.port}  (workspace {host.agent.root})")
    uvicorn.run(create_app(host), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
