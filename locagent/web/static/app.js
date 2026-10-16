// LocAgent 网页前端：用 SSE 接收服务端推送的事件并渲染，用户操作通过 POST 发给服务端。
// 页面打开（或断线重连）时先收到一份快照（当前会话的全部事件），按同一套逻辑渲染，所以回放和实时显示完全一致。

const $ = (id) => document.getElementById(id);

const TOOL_LABELS = {
  read_file: "Read",
  write_file: "Write",
  patch_file: "Edit",
  run_shell: "Bash",
  search: "Search",
  list_files: "List",
  update_todos: "Todos",
};
const EDIT_TOOLS = ["write_file", "patch_file"];
const STOP_REASONS = {
  final_answer_returned: "完成",
  step_limit_reached: "步数用完，任务未完成",
  retry_limit_reached: "模型多次输出无效，已停止",
  interrupted: "已停止",
  model_error: "模型请求失败",
};
const TODO_MARKS = { pending: "○", in_progress: "▸", completed: "✓" };

const ui = {
  state: null,
  turn: null,         // 当前一轮对话的容器
  cards: {},          // 工具卡片，按 tool_use_id
  results: {},        // 工具结果全文，按 tool_use_id
  approvals: {},      // 审批卡片，按审批 id
  totals: { requests: 0, input: 0, output: 0, cacheRead: 0 },
  lastInput: 0,
};

// ---------- 工具函数 ----------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fmt(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "k";
  return String(n);
}

function inputTokens(usage) {
  return (usage.input_tokens || 0) + (usage.cache_read_input_tokens || 0) + (usage.cache_creation_input_tokens || 0);
}

// 简单的 Markdown 渲染：代码块、标题、列表、表格、粗体、行内代码、链接。先整体转义，再加标签，模型输出里的 HTML 不会被执行
function inline(text) {
  return text
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function markdown(source) {
  const lines = escapeHtml(source).split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const code = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i += 1;
      out.push(`<pre><code>${code.join("\n")}</code></pre>`);
    } else if (/^#{1,3} /.test(line)) {
      const level = line.match(/^#+/)[0].length;
      out.push(`<h${level}>${inline(line.slice(level + 1))}</h${level}>`);
      i += 1;
    } else if (/^\s*([-*]|\d+\.) /.test(line)) {
      const ordered = /^\s*\d+\. /.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*([-*]|\d+\.) /, ""))}</li>`);
        i += 1;
      }
      out.push(ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`);
    } else if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line).map((c) => `<th>${c}</th>`).join("");
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(`<tr>${cells(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`);
        i += 1;
      }
      out.push(`<table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`);
    } else if (!line.trim()) {
      i += 1;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,3} |\s*([-*]|\d+\.) |\s*\|)/.test(lines[i])) para.push(inline(lines[i++]));
      if (!para.length) para.push(inline(lines[i++]));
      out.push(`<p>${para.join("<br>")}</p>`);
    }
  }
  return out.join("");
}

// ---------- 滚动 ----------

function nearBottom() {
  const s = $("scroller");
  return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}

function scrollToBottom() {
  const s = $("scroller");
  s.scrollTop = s.scrollHeight;
}

// ---------- 对话渲染 ----------

function ensureTurn() {
  if (!ui.turn) {
    $("messages").querySelector(".empty")?.remove();
    ui.turn = el("div", "turn");
    $("messages").append(ui.turn);
  }
  return ui.turn;
}

function renderEmpty() {
  const box = el("div", "empty");
  box.append(el("h2", "", "今天要做什么？"), el("div", "muted small", ui.state ? ui.state.workspace : ""));
  $("messages").append(box);
}

function briefArgs(name, args) {
  if (name === "update_todos") {
    const todos = args.todos || [];
    return `${todos.filter((t) => t.status === "completed").length}/${todos.length} 完成`;
  }
  if (args.command) return args.command;
  if (args.pattern) return `"${args.pattern}" in ${args.path || "."}`;
  if (args.path) return args.path;
  return JSON.stringify(args);
}

function toolCard(event) {
  const card = el("div", "tool running");
  const head = el("button", "tool-head");
  head.type = "button";
  head.append(el("span", "tool-dot"), el("span", "tool-name", TOOL_LABELS[event.name] || event.name), el("span", "tool-arg", briefArgs(event.name, event.args)), el("span", "tool-time"));
  head.addEventListener("click", () => card.classList.toggle("open"));
  const body = el("div", "tool-body");
  body.append(el("div", "tool-label", "参数"), el("pre", "", JSON.stringify(event.args, null, 2)));
  card.append(head, body);
  ui.cards[event.tool_use_id] = card;
  return card;
}

function fillToolResult(id) {
  const card = ui.cards[id];
  const content = ui.results[id];
  if (!card || content === undefined || card.querySelector(".tool-result")) return;
  const body = card.querySelector(".tool-body");
  body.append(el("div", "tool-label", "结果"), el("pre", "tool-result", content));
}

function finishToolCard(event) {
  const card = ui.cards[event.tool_use_id];
  if (!card) return;
  card.classList.remove("running");
  card.classList.add(event.tool_status === "ok" ? "ok" : "error");
  let time = `${(event.duration_ms / 1000).toFixed(1)}s`;
  if (event.approval_ms) time += ` · 审批 ${(event.approval_ms / 1000).toFixed(1)}s`;
  card.querySelector(".tool-time").textContent = time;
  if (ui.results[event.tool_use_id] === undefined) ui.results[event.tool_use_id] = event.result;
  fillToolResult(event.tool_use_id);
}

function systemLine(text, className) {
  const line = el("div", "system" + (className ? " " + className : ""), text);
  ensureTurn().append(line);
  return line;
}

function runFooter(event) {
  // 新的一轮开始后，旧的“接着做”按钮失效，只保留最后一轮的
  document.querySelectorAll(".run-footer button").forEach((b) => b.remove());
  const footer = el("div", "run-footer");
  const usage = event.usage;
  const total = inputTokens(usage);
  const reason = STOP_REASONS[event.stop_reason] || event.stop_reason;
  footer.append(el("span", event.status === "completed" ? "" : "stopped", reason));
  footer.append(el("span", "", `${event.tool_steps} 步 · ${(event.duration_ms / 1000).toFixed(1)}s`));
  footer.append(el("span", "", `输入 ${fmt(total)} · 输出 ${fmt(usage.output_tokens)}` + (total ? ` · 缓存 ${Math.round((usage.cache_read_input_tokens / total) * 100)}%` : "")));
  if (event.status !== "completed") {
    const button = el("button", "", "接着做");
    button.type = "button";
    button.addEventListener("click", () => post("/api/continue"));
    footer.append(button);
  }
  ensureTurn().append(footer);
}

function handleEvent(event) {
  switch (event.event) {
    case "run_started": {
      ui.turn = null;
      const turn = ensureTurn();
      const request = event.user_request;
      if (request.startsWith("<checkpoint>")) {
        const bubble = el("div", "user-msg resume", "接着做未完成的任务");
        bubble.title = request;
        turn.append(bubble);
      } else {
        turn.append(el("div", "user-msg", request));
      }
      break;
    }
    case "model_responded": {
      const usage = event.usage || {};
      ui.totals.requests += 1;
      ui.totals.input += inputTokens(usage);
      ui.totals.output += usage.output_tokens || 0;
      ui.totals.cacheRead += usage.cache_read_input_tokens || 0;
      ui.lastInput = inputTokens(usage);
      if (event.stop_reason === "max_tokens") {
        systemLine("输出达到单次上限被截断，这次回复已丢弃，模型会分段重写。");
      } else if (event.text && event.text.trim()) {
        const box = el("div", "assistant-text" + (event.tool_calls.length ? " note" : ""));
        box.innerHTML = markdown(event.text.trim());
        ensureTurn().append(box);
      }
      break;
    }
    case "tool_started":
      ensureTurn().append(toolCard(event));
      break;
    case "history_recorded":
      // 只有工具结果会推送过来：全文用于展开卡片
      ui.results[event.record.tool_use_id] = event.record.content;
      fillToolResult(event.record.tool_use_id);
      break;
    case "tool_finished":
      finishToolCard(event);
      break;
    case "notice":
      systemLine("运行时提示：" + event.content);
      break;
    case "context_reduced": {
      if (event.action === "compact") {
        const line = systemLine("");
        const details = el("details");
        details.append(el("summary", "", `上下文已压缩：${event.replaced_records} 条记录合并为摘要`), el("pre", "", event.summary || ""));
        line.append(details);
        ui.lastInput = 0;
      } else {
        systemLine(`上下文已清理：${event.cleared} 条旧工具结果（${fmt(event.freed_chars)} 字符）`);
      }
      break;
    }
    case "checkpoint":
      renderTodos(event.checkpoint.todos);
      break;
    case "model_failed":
      systemLine("模型请求失败：" + event.error, "error");
      break;
    case "run_finished":
      runFooter(event);
      break;
    default:
      break;
  }
}

// ---------- 审批 ----------

function approvalTitle(name, args) {
  if (name === "run_shell") return "需要确认：运行命令";
  if (name === "write_file") return `需要确认：写入文件 ${args.path}`;
  if (name === "patch_file") return `需要确认：修改文件 ${args.path}`;
  return `需要确认：${name}`;
}

function showApproval(message) {
  const card = el("div", "approval");
  card.append(el("div", "approval-title", approvalTitle(message.name, message.args)));
  const pre = el("pre");
  for (const line of message.preview.split("\n")) {
    const span = el("span", line.startsWith("+ ") ? "add" : line.startsWith("- ") ? "del" : "", line);
    pre.append(span, document.createTextNode(line.startsWith("+ ") || line.startsWith("- ") ? "" : "\n"));
  }
  card.append(pre);
  const actions = el("div", "approval-actions");
  const always = EDIT_TOOLS.includes(message.name) ? "本会话允许所有文件修改" : message.name === "run_shell" ? "本会话总是允许这条命令" : "本会话总是允许";
  for (const [label, answer, cls] of [["允许", "yes", "primary"], ["拒绝", "no", ""], [always, "always", ""]]) {
    const button = el("button", cls, label);
    button.type = "button";
    button.addEventListener("click", () => post(`/api/approvals/${message.id}`, { answer }));
    actions.append(button);
  }
  card.append(actions);
  ensureTurn().append(card);
  ui.approvals[message.id] = card;
}

function resolveApproval(message) {
  const card = ui.approvals[message.id];
  if (!card) return;
  card.querySelector(".approval-actions")?.remove();
  const text = { yes: "已允许", no: "已拒绝", always: "已允许（本会话不再询问）" }[message.answer] || message.answer;
  card.append(el("div", "approval-result", text));
  delete ui.approvals[message.id];
}

// ---------- 状态和侧栏 ----------

function renderTodos(todos) {
  const list = $("todos");
  list.replaceChildren();
  if (!todos || !todos.length) {
    list.append(el("li", "muted", "暂无"));
    return;
  }
  for (const todo of todos) {
    const item = el("li", todo.status);
    item.append(el("span", "mark", TODO_MARKS[todo.status] || "○"), el("span", "", todo.content));
    list.append(item);
  }
}

function renderStats() {
  const t = ui.totals;
  $("stat-requests").textContent = t.requests;
  $("stat-input").textContent = fmt(t.input);
  $("stat-output").textContent = fmt(t.output);
  $("stat-cache").textContent = t.input ? `${Math.round((t.cacheRead / t.input) * 100)}%` : "-";
  const s = ui.state;
  if (!s) return;
  const ratio = Math.min(ui.lastInput / s.budget, 1);
  const fill = $("context-fill");
  fill.style.width = `${ratio * 100}%`;
  fill.className = "meter-fill" + (ratio > s.compact_ratio ? " high" : ratio > s.clear_ratio ? " warn" : "");
  $("clear-mark").style.left = `${s.clear_ratio * 100}%`;
  $("compact-mark").style.left = `${s.compact_ratio * 100}%`;
  $("context-text").textContent = `${fmt(ui.lastInput)} / ${fmt(s.budget)} token（${Math.round(ratio * 100)}%）`;
}

function renderState() {
  const s = ui.state;
  if (!s) return;
  $("send").hidden = s.busy;
  $("stop").hidden = !s.busy;
  $("compact").disabled = s.busy;
  $("continue").disabled = s.busy || !s.unfinished;
  $("new-session").disabled = s.busy;
  $("task-status").textContent = s.busy ? "正在运行" : s.unfinished ? "有未完成的任务，可以接着做" : "没有未完成的任务";
  $("model").textContent = `${s.model} · 审批：${s.approval_policy} · 每次最多 ${s.max_steps} 步`;
  $("workspace").textContent = s.workspace;
  $("workspace").title = s.workspace;
  if (!ui.lastInput) ui.lastInput = s.last_input_tokens;
  renderStats();
  refreshThinking();
}

// 运行中且没有等待审批时，在对话末尾显示“正在处理”
function refreshThinking() {
  document.querySelectorAll(".thinking").forEach((node) => node.remove());
  if (ui.state && ui.state.busy && !Object.keys(ui.approvals).length) {
    ensureTurn().append(el("div", "thinking", "正在处理……"));
  }
}

async function loadSessions() {
  const response = await fetch("/api/sessions");
  const sessions = await response.json();
  const nav = $("sessions");
  nav.replaceChildren();
  for (const session of sessions) {
    const button = el("button", "session" + (session.current ? " current" : ""));
    button.type = "button";
    const title = session.title === "(empty session)" ? "新会话" : session.title;
    const name = el("span", "session-name", title);
    const meta = el("span", "session-meta", session.updated);
    if (session.unfinished) meta.append(el("span", "badge", "未完成"));
    button.append(name, meta);
    button.title = session.id;
    button.addEventListener("click", () => {
      if (!session.current) post(`/api/sessions/${session.id}/open`);
    });
    nav.append(button);
    if (session.current) $("session-title").textContent = title;
  }
}

// ---------- 推送消息 ----------

function applySnapshot(message) {
  $("messages").replaceChildren();
  Object.assign(ui, { turn: null, cards: {}, results: {}, approvals: {}, totals: { requests: 0, input: 0, output: 0, cacheRead: 0 }, lastInput: 0 });
  ui.state = message.state;
  renderTodos(message.state.todos);
  for (const event of message.events) handleEvent(event);
  if (!$("messages").children.length) renderEmpty();
  for (const approval of message.approvals) showApproval(approval);
  renderState();
  loadSessions();
  scrollToBottom();
}

function onMessage(message) {
  const stick = nearBottom();
  switch (message.type) {
    case "snapshot":
      applySnapshot(message);
      return;
    case "event":
      document.querySelectorAll(".thinking").forEach((node) => node.remove());
      handleEvent(message.event);
      if (message.event.event === "run_finished") loadSessions();
      renderStats();
      refreshThinking();
      break;
    case "state":
      ui.state = message.state;
      renderState();
      if (!message.state.busy) loadSessions();
      break;
    case "approval_request":
      document.querySelectorAll(".thinking").forEach((node) => node.remove());
      showApproval(message);
      break;
    case "approval_resolved":
      resolveApproval(message);
      refreshThinking();
      break;
    case "error":
      systemLine(message.message, "error");
      break;
    default:
      break;
  }
  if (stick) scrollToBottom();
}

function connect() {
  const source = new EventSource("/api/stream");
  source.onopen = () => {
    $("connection").textContent = "已连接";
    $("connection").className = "connection ok";
  };
  // 断线后浏览器会自动重连，重连成功时服务端会重新发送快照
  source.onerror = () => {
    $("connection").textContent = "连接断开，正在重连";
    $("connection").className = "connection lost";
  };
  source.onmessage = (e) => onMessage(JSON.parse(e.data));
}

// ---------- 用户操作 ----------

async function post(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    systemLine("操作失败：" + (data.detail || response.statusText), "error");
    if (nearBottom()) scrollToBottom();
  }
  return response.ok;
}

function autosize() {
  const input = $("input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 240) + "px";
}

async function submit() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || (ui.state && ui.state.busy)) return;
  // 和命令行一样支持几个斜杠命令
  const commands = { "/continue": "/api/continue", "/compact": "/api/compact", "/new": "/api/sessions/new", "/reset": "/api/sessions/new" };
  const ok = commands[text] ? await post(commands[text]) : await post("/api/ask", { text });
  if (ok) {
    input.value = "";
    autosize();
  }
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  submit();
});
$("input").addEventListener("keydown", (e) => {
  // 输入法组字时的回车不发送
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submit();
  }
});
$("input").addEventListener("input", autosize);
$("stop").addEventListener("click", () => post("/api/stop"));
$("compact").addEventListener("click", () => post("/api/compact"));
$("continue").addEventListener("click", () => post("/api/continue"));
$("new-session").addEventListener("click", () => post("/api/sessions/new"));

connect();
