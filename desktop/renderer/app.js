// Mai-Code desktop renderer. Multi-workspace chat UI: per-project daemons,
// streamed event rendering, permission approvals, theme + endpoint settings.

const $ = (sel) => document.querySelector(sel);

const stream = $("#stream");
const input = $("#input");
const sendBtn = $("#sendBtn");
const sessionList = $("#sessionList");
const sessionTitle = $("#sessionTitle");
const toastEl = $("#toast");

const FOLDER_SVG = '<svg viewBox="0 0 16 16" width="13" height="13"><path fill="currentColor" d="M1.5 3.5A1.5 1.5 0 0 1 3 2h3.2c.4 0 .8.16 1.06.44L8.5 3.7h4.9A1.6 1.6 0 0 1 15 5.3v7.2a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 1 12.5v-9Zm2 .1v2.9h9.9V5.3a.1.1 0 0 0-.1-.1H8.2a.9.9 0 0 1-.64-.26L6.3 3.6H3.5a.1.1 0 0 0-.1.1Z"/></svg>';

/* ---------- pixel brand (data lifted verbatim from the TUI) ---------- */

// _PIXEL_MASCOT_GRID / _PIXEL_MASCOT_COLORS in src/mai_code/tui/app.py
const LIBRA_GRID = [
  "..LLLLLLLLL..",
  "..BBBBBBBBB..",
  "..D...D...D..",
  "..D...D...D..",
  "BBBBB.L.BBBBB",
  ".SSS..D..SSS.",
  "...DDDDDDD...",
  "..SSSSSSSSS..",
];
const LIBRA_COLORS = { D: "#155e75", S: "#0c4a6e", B: "#38bdf8", L: "#e0f2fe" };

function libraSvg(width) {
  const h = LIBRA_GRID.length;
  const w = LIBRA_GRID[0].length;
  let rects = "";
  LIBRA_GRID.forEach((row, y) => {
    [...row].forEach((cell, x) => {
      if (cell !== ".") {
        rects += `<rect x="${x}" y="${y}" width="1" height="1" fill="${LIBRA_COLORS[cell]}"/>`;
      }
    });
  });
  return `<svg viewBox="0 0 ${w} ${h}" width="${width}" height="${(width * h) / w}" shape-rendering="crispEdges">${rects}</svg>`;
}

// _PIXEL_TITLE_GLYPHS + _pixel_title() in src/mai_code/tui/app.py
const TITLE_GLYPHS = {
  M: ["█...█", "██.██", "█.█.█", "█...█", "█...█"],
  A: ["..█..", ".█.█.", "█...█", "█████", "█...█"],
  I: ["███", ".█.", ".█.", ".█.", "███"],
  "-": ["....", "....", "████", "....", "...."],
  C: [".███.", "█...█", "█....", "█...█", ".███."],
  O: [".███.", "█...█", "█...█", "█...█", ".███."],
  D: ["████.", "█...█", "█...█", "█...█", "████."],
  E: ["█████", "█....", "████.", "█....", "█████"],
};
const TITLE_FACE = "#eef3fb";
const TITLE_DEPTH = "#5f7fae";

function pixelTitleSvg(text, pixel) {
  const height = 5;
  const placements = [];
  let cursor = 0;
  for (const ch of text) {
    const glyph = TITLE_GLYPHS[ch];
    placements.push([cursor, glyph]);
    cursor += glyph[0].length + 1;
  }
  const width = cursor - 1;
  const canvas = Array.from({ length: height + 1 }, () =>
    Array.from({ length: width + 1 }, () => null),
  );
  for (const [dx, dy] of [[1, 1], [1, 0]]) {
    for (const [gx, glyph] of placements) {
      glyph.forEach((row, ry) => {
        [...row].forEach((cell, rx) => {
          if (cell === "█" && !canvas[ry + dy][gx + rx + dx]) {
            canvas[ry + dy][gx + rx + dx] = TITLE_DEPTH;
          }
        });
      });
    }
  }
  for (const [gx, glyph] of placements) {
    glyph.forEach((row, ry) => {
      [...row].forEach((cell, rx) => {
        if (cell === "█") canvas[ry][gx + rx] = TITLE_FACE;
      });
    });
  }
  let rects = "";
  canvas.forEach((row, y) => {
    row.forEach((color, x) => {
      if (color) rects += `<rect x="${x}" y="${y}" width="1" height="1" fill="${color}"/>`;
    });
  });
  return `<svg viewBox="0 0 ${width + 1} ${height + 1}" width="${(width + 1) * pixel}" height="${(height + 1) * pixel}" shape-rendering="crispEdges">${rects}</svg>`;
}

$("#brand").innerHTML = pixelTitleSvg("MAI-CODE", 2.4);
$("#heroIcon").innerHTML = libraSvg(96);

/* ---------- persisted state ---------- */

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    localStorage.setItem(key, JSON.stringify(value));
  },
};

const S = {
  theme: store.get("mai.theme", "dark"),
  workspaces: store.get("mai.workspaces", [
    { id: "default", name: "默认工作区", path: "", port: 7437 },
  ]),
  activeWsId: store.get("mai.activeWsId", "default"),
  sessionWs: store.get("mai.sessionWs", {}), // session_id -> workspace id
  archived: store.get("mai.archived", []), // session ids hidden from the list
  sessions: [],
  models: [], // available models for the current endpoint
  currentModel: "—",
  currentSessionId: null,
  currentClosed: false,
  sending: false,
  activeRuns: new Set(),
  runBlocks: new Map(),
  toolCards: new Map(),
  permCards: new Map(),
};

function uid() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function activeWs() {
  return S.workspaces.find((w) => w.id === S.activeWsId) ?? S.workspaces[0];
}

/* ---------- tiny dom helpers ---------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function nearBottom() {
  return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 90;
}

function appendBlock(node) {
  const stick = nearBottom();
  const inner = $("#stream-inner") ?? stream;
  if (!$("#emptyState").classList.contains("hidden")) $("#emptyState").classList.add("hidden");
  inner.appendChild(node);
  if (stick) stream.scrollTop = stream.scrollHeight;
  return node;
}

let toastTimer = null;
function toast(msg, ms = 2600) {
  toastEl.textContent = msg;
  toastEl.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.add("hidden"), ms);
}

/* ---------- stream blocks ---------- */

function roleRow(name, withWhen = true) {
  const row = el("div", "role-row");
  const avatar = el("span", "avatar", name === "Mai" ? "M" : "你");
  row.append(avatar, el("span", "name", name));
  if (withWhen) row.append(el("span", "when", new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })));
  return row;
}

function addUserMessage(content) {
  const wrap = el("div", "msg-user");
  wrap.append(el("div", "bubble", content));
  appendBlock(wrap);
}

function runBlock(runId) {
  let b = S.runBlocks.get(runId);
  if (!b) {
    const root = el("div", "block");
    const text = el("div", "assistant-text");
    const caret = el("span", "caret");
    root.append(roleRow("Mai"), text, caret);
    b = { root, text, caret };
    S.runBlocks.set(runId, b);
    appendBlock(root);
  }
  return b;
}

function detachCaret(b) {
  if (b && b.caret.parentNode) b.caret.remove();
}

const TOOL_GLYPHS = {
  bash: ">_",
  read_file: "读",
  write_file: "写",
  list_dir: "列",
  note_save: "记",
  spawn_agent: "子",
};

function toolGlyph(name) {
  if (TOOL_GLYPHS[name]) return TOOL_GLYPHS[name];
  if (name.startsWith("task_")) return "任务";
  if (name.startsWith("mcp_")) return "MCP";
  return "工具";
}

function previewOf(params) {
  const s = typeof params === "string" ? params : JSON.stringify(params);
  return s.length > 120 ? s.slice(0, 117) + "…" : s;
}

function addToolCard(ev) {
  const b = runBlock(ev.run_id);
  const card = el("div", "tool-card");
  const head = el("div", "tool-head");
  const state = el("span", "tool-state");
  state.append(el("span", "spin"));
  head.append(
    el("span", "tool-glyph", toolGlyph(ev.tool_name)),
    el("span", "tool-name", ev.tool_name),
    el("span", "tool-preview", previewOf(ev.params)),
    state,
    el("span", "chev", "▶"),
  );
  const body = el("div", "tool-body");
  body.append(el("h4", "", "参数"), el("pre", "", JSON.stringify(ev.params, null, 2) || ""));
  const outWrap = el("div");
  body.append(outWrap);
  head.addEventListener("click", () => card.classList.toggle("open"));
  card.append(head, body);
  b.root.appendChild(card);
  S.toolCards.set(ev.tool_use_id, { card, stateEl: state, outWrap, outShown: false });
}

function toolFinish(ev, ok) {
  const card = S.toolCards.get(ev.tool_use_id);
  if (!card) return;
  const spin = card.stateEl.querySelector(".spin");
  if (spin) spin.remove();
  card.stateEl.className = `tool-state ${ok ? "ok" : "err"}`;
  if (ok) {
    card.stateEl.textContent = `✓ ${ev.elapsed_ms}ms`;
    if (!card.outShown) {
      card.outShown = true;
      card.outWrap.replaceChildren(el("h4", "", "输出"), el("pre", "", ev.output || "(空)"));
    }
  } else {
    const retry = ev.attempt > 1 ? ` · 重试#${ev.attempt}` : "";
    card.stateEl.textContent = `✕ ${ev.error_class}${retry}`;
    card.outWrap.replaceChildren(
      el("h4", "", "错误"),
      el("pre", "", `${ev.error_message}\n[${ev.error_class}]`),
    );
  }
}

function addPermissionCard(ev) {
  const holder = S.runBlocks.get(ev.run_id)?.root;
  const card = el("div", "perm-card");
  const head = el("div", "perm-head", "🔒 权限审批");
  head.append(el("span", "tool-name", ev.tool_name));
  const preview = el("div", "perm-preview", ev.param_preview || JSON.stringify(ev.params, null, 2));
  const actions = el("div", "perm-actions");

  const decide = async (decision, label) => {
    buttons.forEach((b) => (b.disabled = true));
    try {
      await request("permission.respond", { tool_use_id: ev.tool_use_id, decision });
      actions.replaceChildren(el("span", "perm-done", `已${label}`));
    } catch (err) {
      toast(`审批提交失败: ${err.message}`);
      buttons.forEach((b) => (b.disabled = false));
    }
  };
  const mk = (label, decision, cls) => {
    const btn = el("button", cls, label);
    btn.addEventListener("click", () => decide(decision, label));
    return btn;
  };
  const buttons = [
    mk("允许一次", "allow_once", "allow"),
    mk("总是允许", "always_allow", "allow"),
    mk("拒绝", "deny_once", "deny"),
  ];
  actions.append(...buttons);

  card.append(head, preview, actions);
  if (holder) holder.appendChild(card);
  else appendBlock(card);
  S.permCards.set(ev.tool_use_id, { card });
}

function addRunFinished(ev) {
  const b = S.runBlocks.get(ev.run_id);
  const chip = el("div", `run-status ${ev.status === "success" ? "ok" : "fail"}`);
  chip.textContent = ev.status === "success"
    ? `✓ 完成 · ${ev.steps} 步`
    : `✕ 失败 · ${ev.reason || "unknown"}`;
  if (b) {
    detachCaret(b);
    b.root.appendChild(chip);
  } else appendBlock(chip);
  S.activeRuns.delete(ev.run_id);
  if (S.activeRuns.size === 0) setRunning(false);
}

function addNotice(text) {
  appendBlock(el("div", "notice", text));
}

function setStreamError(msg) {
  appendBlock(el("div", "stream-error", msg));
}

/* ---------- running state ---------- */

function setRunning(on) {
  S.sending = on;
  sendBtn.disabled = on;
  sendBtn.title = on ? "运行中…" : "发送";
  input.disabled = S.currentClosed;
  input.placeholder = on
    ? "Mai 正在执行，完成后可以继续对话…"
    : "描述你的目标，Enter 发送，Shift+Enter 换行";
}

/* ---------- event router ---------- */

function handleEvent({ wsId, event }) {
  if (wsId !== S.activeWsId) return; // another workspace's daemon
  const ev = event;
  switch (ev.type) {
    case "session.message_received":
      addUserMessage(ev.content);
      break;
    case "run.started":
      S.activeRuns.add(ev.run_id);
      setRunning(true);
      runBlock(ev.run_id);
      break;
    case "llm.token": {
      const b = runBlock(ev.run_id);
      b.text.textContent += ev.token;
      if (nearBottom()) stream.scrollTop = stream.scrollHeight;
      break;
    }
    case "step.started": {
      const b = runBlock(ev.run_id);
      b.root.appendChild(el("div", "step-chip", `Step ${ev.step}`));
      break;
    }
    case "tool.call_started":
      addToolCard(ev);
      break;
    case "tool.call_finished":
      toolFinish(ev, true);
      break;
    case "tool.call_failed":
      toolFinish(ev, false);
      break;
    case "permission.requested":
      addPermissionCard(ev);
      break;
    case "permission.granted":
    case "permission.denied": {
      const p = S.permCards.get(ev.tool_use_id);
      if (p && p.card.isConnected) {
        const actions = p.card.querySelector(".perm-actions");
        if (actions) {
          actions.replaceChildren(
            el("span", "perm-done", ev.type === "permission.granted" ? "已允许" : "已拒绝"),
          );
        }
      }
      break;
    }
    case "llm.usage": {
      $("#ctxMeter").classList.remove("hidden");
      if (ev.model) {
        S.currentModel = ev.model;
        $("#modelName").textContent = ev.model;
      }
      let pct = ev.context_pct;
      if (pct > 1) pct = pct / 100;
      pct = Math.max(0, Math.min(1, pct || 0));
      const bar = $("#ctxBar");
      bar.style.width = `${Math.round(pct * 100)}%`;
      bar.className = pct > 0.9 ? "critical" : pct > 0.7 ? "hot" : "";
      $("#ctxPct").textContent = `${Math.round(pct * 100)}%`;
      break;
    }
    case "run.finished":
      addRunFinished(ev);
      break;
    case "session.waiting_for_input":
      setRunning(false);
      break;
    case "context.compacted":
      addNotice(`上下文已压缩：${ev.original_tokens} → ${ev.summary_tokens} tokens`);
      break;
    case "subagent.started":
      addNotice(`子代理启动：${ev.description}`);
      break;
    case "subagent.finished":
      addNotice(`子代理结束：${ev.status}`);
      break;
    case "skill.invoked":
      addNotice(`技能调用：${ev.skill_name}`);
      break;
    case "session.closed":
      if (ev.session_id === S.currentSessionId) {
        S.currentClosed = true;
        if (!S.activeRuns.size) setRunning(false);
        addNotice("会话已结束");
        refreshSessions();
      }
      break;
    case "session.created":
      refreshSessions();
      break;
    default:
      break;
  }
}

/* ---------- daemon / workspace plumbing ---------- */

function request(method, params) {
  return window.mai.request(S.activeWsId, method, params);
}

function setDaemonStatus(status) {
  const dot = $("#daemonDot");
  const text = $("#daemonText");
  if (status.connected) {
    dot.className = "dot ok";
    text.textContent = `已连接 :${status.port}`;
  } else if (status.spawning) {
    dot.className = "dot wait";
    text.textContent = "正在启动 mai-core…";
  } else {
    dot.className = "dot bad";
    text.textContent = status.error ? `连接失败` : "未连接";
  }
}

async function connectActiveWs() {
  setDaemonStatus({ spawning: true });
  const ws = activeWs();
  const res = await window.mai.connect(ws);
  setDaemonStatus({ ...res, connected: res.ok, spawning: !res.ok });
  if (!res.ok) {
    setStreamError(`无法连接工作区「${ws.name}」的 mai-core：${res.error}`);
    return false;
  }
  try {
    const llm = await request("config.get_llm", {});
    $("#cfgBaseUrl").placeholder = llm.base_url || "https://api.deepseek.com/anthropic";
    $("#cfgModel").placeholder = llm.default_model || "deepseek-chat";
  } catch (_err) {
    /* best-effort */
  }
  try {
    await fetchModels();
  } catch (_err) {
    /* model list is best-effort; the chip falls back to the config default */
  }
  return true;
}

/* ---------- model switcher (session.set_model: "" queries, value switches) ---------- */

async function fetchModels() {
  const r = await request("session.set_model", { model: "" });
  S.currentModel = r.current_model || "—";
  S.models = r.available_models ?? [];
  $("#modelName").textContent = S.currentModel;
  return S.models;
}

async function setModel(name) {
  try {
    const r = await request("session.set_model", { model: name });
    S.currentModel = r.current_model;
    if (r.available_models?.length) S.models = r.available_models;
    $("#modelName").textContent = S.currentModel;
    $("#modelPopover").classList.add("hidden");
    toast(`模型已切换为 ${S.currentModel}`);
  } catch (err) {
    toast(`切换失败: ${err.message}`);
  }
}

function renderModelPopover() {
  const pop = $("#modelPopover");
  pop.replaceChildren();
  if (!S.models.length) {
    pop.append(el("div", "model-empty", "暂未获取到模型列表，可在设置里「验证并获取模型」"));
    return;
  }
  for (const m of S.models) {
    const item = el("div", `model-item ${m === S.currentModel ? "current" : ""}`);
    item.append(el("span", "check", m === S.currentModel ? "✓" : ""));
    item.append(el("span", "", m));
    item.addEventListener("click", () => setModel(m));
    pop.append(item);
  }
}

$("#modelChip").addEventListener("click", async () => {
  const pop = $("#modelPopover");
  if (!pop.classList.contains("hidden")) {
    pop.classList.add("hidden");
    return;
  }
  if (!S.models.length) {
    try {
      await fetchModels();
    } catch (_err) {
      /* render the empty hint */
    }
  }
  renderModelPopover();
  pop.classList.remove("hidden");
});

/* ---------- projects (sidebar section) ---------- */

function renderProjects() {
  const list = $("#projectList");
  list.replaceChildren();
  for (const w of S.workspaces) {
    const item = el("div", `project-item ${w.id === S.activeWsId ? "active" : ""}`);
    const icon = el("span", "p-icon");
    icon.innerHTML = FOLDER_SVG;
    const info = el("div", "info");
    info.append(el("div", "n", w.name));
    item.append(icon, info);
    if (w.id !== "default") {
      const del = el("button", "del", "✕");
      del.title = "移除工作区";
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        removeWorkspace(w.id);
      });
      item.append(del);
    }
    item.title = w.path || "仓库内置工作区";
    item.addEventListener("click", () => switchWorkspace(w.id));
    list.append(item);
  }
  const add = el("button", "project-add", "＋ 添加项目文件夹…");
  add.addEventListener("click", addWorkspace);
  list.append(add);
}

async function addWorkspace() {
  const dir = await window.mai.pickFolder();
  if (!dir) return;
  const name = dir.split("/").filter(Boolean).pop() || dir;
  const port = Math.max(...S.workspaces.map((w) => w.port)) + 1;
  const w = { id: `ws-${uid()}`, name, path: dir, port };
  S.workspaces.push(w);
  store.set("mai.workspaces", S.workspaces);
  renderProjects();
  switchWorkspace(w.id);
}

function removeWorkspace(wsId) {
  if (wsId === S.activeWsId) {
    toast("先切换到其他工作区再移除");
    return;
  }
  S.workspaces = S.workspaces.filter((w) => w.id !== wsId);
  store.set("mai.workspaces", S.workspaces);
  renderProjects();
}

async function switchWorkspace(wsId) {
  if (wsId === S.activeWsId) return;
  if (S.activeRuns.size) {
    toast("当前工作区有任务正在运行，等它结束再切换");
    return;
  }
  S.activeWsId = wsId;
  store.set("mai.activeWsId", wsId);
  resetStreamToEmpty();
  renderProjects();
  sessionTitle.textContent = "新对话";
  await connectActiveWs();
}

/* ---------- sessions ---------- */

function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  if (diff < 60_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function renderSessions() {
  sessionList.replaceChildren();
  const visible = S.sessions.filter(
    (s) =>
      !S.archived.includes(s.id) &&
      (S.sessionWs[s.id] ?? "default") === S.activeWsId,
  );
  if (!visible.length) {
    sessionList.append(el("div", "side-label", "当前工作区还没有会话"));
    return;
  }
  for (const s of visible) {
    const item = el("div", "session-item");
    if (s.id === S.currentSessionId) item.classList.add("active");
    item.append(el("div", "t", s.title || "未命名会话"));
    const meta = el("div", "m");
    meta.append(
      el("span", `tag ${s.status === "active" ? "open" : "closed"}`,
        s.status === "active" ? "进行中" : "已结束"),
      el("span", "", fmtTime(s.updatedAt)),
    );
    const archiveBtn = el("button", "archive-btn", "归档");
    archiveBtn.title = "从列表归档（隐藏）此会话";
    archiveBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      archiveSession(s);
    });
    meta.append(archiveBtn);
    item.append(meta);
    item.addEventListener("click", () => openSession(s));
    sessionList.append(item);
  }
}

// Archive = hide locally (backend has no archive state). Sessions still open
// on the daemon are closed first so nothing keeps running behind the scenes.
async function archiveSession(s) {
  if (s.id === S.currentSessionId && S.activeRuns.size) {
    toast("这个会话还有任务在运行，结束后再归档");
    return;
  }
  if (s.status === "active") {
    try {
      await request("session.close", { session_id: s.id });
    } catch (_err) {
      /* best-effort: still archive locally */
    }
  }
  S.archived.push(s.id);
  store.set("mai.archived", S.archived);
  if (s.id === S.currentSessionId) resetStreamToEmpty();
  renderSessions();
  toast(`已归档「${s.title || "未命名会话"}」`);
}

async function refreshSessions() {
  try {
    S.sessions = await window.mai.listSessions();
    renderSessions();
  } catch (_err) {
    /* ignore */
  }
}

function flattenContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((c) => (typeof c === "string" ? c : c?.text ?? JSON.stringify(c))).join("");
  }
  return JSON.stringify(content);
}

async function openSession(s) {
  if (S.activeRuns.size) {
    toast("有任务正在运行，等它结束再切换会话");
    return;
  }
  try {
    const res = await request("session.get_history", { session_id: s.id });
    S.currentSessionId = s.id;
    S.currentClosed = s.status !== "active";
    S.runBlocks.clear();
    S.toolCards.clear();
    S.permCards.clear();
    $("#stream-inner").replaceChildren();
    $("#emptyState").classList.add("hidden");
    sessionTitle.textContent = s.title || "未命名会话";
    for (const m of res.messages ?? []) {
      const content = flattenContent(m.content);
      if (m.role === "user") addUserMessage(content);
      else {
        const b = runBlock(`hist-${m.role}`);
        b.text.textContent += content;
        detachCaret(b);
      }
    }
    setRunning(false);
    renderSessions();
  } catch (err) {
    toast(`加载会话失败: ${err.message}`);
  }
}

function resetStreamToEmpty() {
  S.currentSessionId = null;
  S.currentClosed = false;
  S.activeRuns.clear();
  S.runBlocks.clear();
  S.toolCards.clear();
  S.permCards.clear();
  $("#stream-inner").replaceChildren();
  $("#emptyState").classList.remove("hidden");
  sessionTitle.textContent = "新对话";
  setRunning(false);
  renderSessions();
}

/* ---------- composer ---------- */

function autoGrow() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
}

async function send() {
  const text = input.value.trim();
  if (!text || S.sending) return;
  sendBtn.disabled = true;
  try {
    if (!S.currentSessionId) {
      const res = await request("session.create", {
        mode: "chat",
        title: text.slice(0, 30),
      });
      S.currentSessionId = res.session_id;
      S.currentClosed = false;
      S.sessionWs[res.session_id] = S.activeWsId;
      store.set("mai.sessionWs", S.sessionWs);
      sessionTitle.textContent = text.slice(0, 30) || "新对话";
    }
    await request("session.send_message", {
      session_id: S.currentSessionId,
      content: text,
    });
    input.value = "";
    autoGrow();
  } catch (err) {
    setStreamError(`发送失败：${err.message}`);
    toast(`发送失败: ${err.message}`);
  } finally {
    // run.started hasn't arrived yet for a fresh run — only re-enable when
    // nothing is executing
    if (!S.activeRuns.size && !S.sending) sendBtn.disabled = false;
  }
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});
input.addEventListener("input", autoGrow);
$("#newChatBtn").addEventListener("click", resetStreamToEmpty);
document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    input.value = chip.dataset.prompt;
    autoGrow();
    input.focus();
  });
});

/* ---------- sidebar resizer ---------- */

const resizer = $("#resizer");
let draggingSidebar = false;

resizer.addEventListener("pointerdown", (e) => {
  draggingSidebar = true;
  resizer.setPointerCapture(e.pointerId);
  document.body.classList.add("resizing");
});
resizer.addEventListener("pointermove", (e) => {
  if (!draggingSidebar) return;
  const w = Math.max(200, Math.min(460, e.clientX));
  document.documentElement.style.setProperty("--side-w", `${w}px`);
});
resizer.addEventListener("pointerup", () => {
  draggingSidebar = false;
  document.body.classList.remove("resizing");
  store.set("mai.sidebarWidth", $("#sidebar").offsetWidth);
});

/* ---------- global click-away ---------- */

document.addEventListener("click", (e) => {
  const pop = $("#modelPopover");
  if (!pop.classList.contains("hidden") && !e.target.closest(".model-anchor")) {
    pop.classList.add("hidden");
  }
});

/* ---------- settings ---------- */

const settingsModal = $("#settingsModal");

function applyTheme(theme) {
  S.theme = theme;
  document.body.classList.toggle("light", theme === "light");
  store.set("mai.theme", theme);
  document.querySelectorAll("#themeSegment button").forEach((b) => {
    b.classList.toggle("active", b.dataset.theme === theme);
  });
}

document.querySelectorAll("#themeSegment button").forEach((btn) => {
  btn.addEventListener("click", () => applyTheme(btn.dataset.theme));
});

$("#settingsBtn").addEventListener("click", async () => {
  settingsModal.classList.remove("hidden");
  $("#cfgHint").textContent = "";
  try {
    const llm = await request("config.get_llm", {});
    $("#cfgBaseUrl").placeholder = llm.base_url || "https://api.deepseek.com/anthropic";
    $("#cfgModel").placeholder = llm.default_model || "deepseek-chat";
    $("#cfgModel").value = S.currentModel !== "—" ? S.currentModel : (llm.default_model || "");
  } catch (_err) {
    $("#cfgHint").textContent = "读取当前配置失败（daemon 未连接），保存将在连接后生效";
  }
});
$("#settingsClose").addEventListener("click", () => settingsModal.classList.add("hidden"));
settingsModal.addEventListener("click", (e) => {
  if (e.target === settingsModal) settingsModal.classList.add("hidden");
});

$("#probeBtn").addEventListener("click", async () => {
  const hint = $("#cfgHint");
  const btn = $("#probeBtn");
  btn.disabled = true;
  hint.className = "cfg-hint";
  hint.textContent = "探测中…";
  const payload = {
    base_url: $("#cfgBaseUrl").value.trim(),
    api_key: $("#cfgApiKey").value.trim(),
  };
  let result;
  if (payload.base_url && payload.api_key) {
    // both explicit — probe directly, no daemon needed
    result = await window.mai.probeLlm(payload);
  } else {
    // fall back to the daemon, which fills blanks from saved config
    try {
      const r = await request("config.probe_llm", payload);
      result = { ok: r.models.length > 0, models: r.models, error: "未探测到可用模型" };
    } catch (err) {
      result = { ok: false, models: [], error: `${err.message}（也可在上方同时填 Base URL 和 Key 直接验证）` };
    }
  }
  btn.disabled = false;
  if (result.ok && result.models.length) {
    hint.className = "cfg-hint ok";
    hint.textContent = `✓ 验证通过，发现 ${result.models.length} 个模型，已在下拉框中列出`;
    $("#modelOptions").replaceChildren(
      ...result.models.map((m) => {
        const opt = document.createElement("option");
        opt.value = m;
        return opt;
      }),
    );
  } else {
    hint.className = "cfg-hint err";
    hint.textContent = `✕ ${result.error}`;
  }
});

$("#saveCfgBtn").addEventListener("click", async () => {
  const hint = $("#cfgHint");
  try {
    const r = await request("config.set_llm", {
      base_url: $("#cfgBaseUrl").value.trim(),
      api_key: $("#cfgApiKey").value.trim(),
      default_model: $("#cfgModel").value.trim(),
    });
    hint.className = "cfg-hint ok";
    hint.textContent = `✓ 已保存：${r.base_url} · ${r.default_model}`;
    S.currentModel = r.default_model || S.currentModel;
    $("#modelName").textContent = S.currentModel;
    $("#cfgApiKey").value = "";
    toast("配置已保存");
  } catch (err) {
    hint.className = "cfg-hint err";
    hint.textContent = `✕ 保存失败：${err.message}`;
  }
});

/* ---------- boot ---------- */

async function boot() {
  applyTheme(new URLSearchParams(location.search).get("theme") || S.theme);
  const savedWidth = store.get("mai.sidebarWidth", 0);
  if (savedWidth >= 200 && savedWidth <= 460) {
    document.documentElement.style.setProperty("--side-w", `${savedWidth}px`);
  }
  window.mai.onEvent(handleEvent);
  window.mai.onStatus((st) => {
    if (st.wsId === S.activeWsId) setDaemonStatus(st);
  });
  renderProjects();

  const ok = await connectActiveWs();
  refreshSessions();
  if (!ok) return;

  // E2E helpers: MAI_E2E_GOAL auto-sends a goal; MAI_E2E_VIEW opens a panel.
  const params = new URLSearchParams(location.search);
  const goal = params.get("e2e");
  if (goal) {
    setTimeout(() => {
      input.value = goal;
      autoGrow();
      send();
    }, 1200);
  }
  if (params.get("view") === "settings") {
    setTimeout(() => $("#settingsBtn").click(), 800);
  }
}

boot();
