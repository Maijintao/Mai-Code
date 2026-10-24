// Mai-Code desktop daemon layer.
//
// One daemon per workspace: the default workspace reuses the repo daemon on
// 7437; every added project folder gets its own mai-core process spawned with
// cwd = project dir and MAI_PORT = 7437 + n. That gives real per-project
// isolation (bash/read/write run inside the project) with zero backend
// changes — mirrors how VS Code runs one process per window.
//
// Wire protocol: JSON-RPC 2.0 over NDJSON on TCP (see WIRE_PROTOCOL.md).

const net = require("node:net");
const { EventEmitter } = require("node:events");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const CONNECT_TIMEOUT_MS = 3_000;
const REQUEST_TIMEOUT_MS = 30_000;
const SPAWN_WAIT_MS = 15_000;
const SPAWN_POLL_INTERVAL_MS = 400;
const RECONNECT_DELAY_MS = 2_000;

// One-shot ping used to probe whether a daemon is already listening.
function pingOnce({ host, port }, timeoutMs = CONNECT_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host, port });
    let buffer = "";
    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      socket.destroy();
      reject(new Error("ping timeout"));
    }, timeoutMs);

    socket.on("error", (err) => {
      clearTimeout(timer);
      socket.destroy();
      reject(err);
    });

    socket.on("connect", () => {
      socket.write(
        JSON.stringify({
          jsonrpc: "2.0",
          id,
          method: "core.ping",
          params: { type: "core.ping", client: "mai-desktop" },
        }) + "\n",
      );
    });

    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const nl = buffer.indexOf("\n");
      if (nl === -1) return;
      clearTimeout(timer);
      socket.destroy();
      try {
        const msg = JSON.parse(buffer.slice(0, nl));
        if (msg.id === id && msg.result) resolve(msg.result);
        else reject(new Error("unexpected ping response"));
      } catch (err) {
        reject(err);
      }
    });
  });
}

class DaemonClient extends EventEmitter {
  constructor({ repoRoot, ws }) {
    super();
    this.repoRoot = repoRoot;
    this.ws = ws;
    this.host = "127.0.0.1";
    this.port = ws.port;
    this.socket = null;
    this.buffer = "";
    this.pending = new Map(); // id -> { resolve, reject, timer }
    this.connected = false;
    this.spawnedPid = null;
    this.reconnectTimer = null;
    this.closedByUs = false;
  }

  status() {
    return {
      wsId: this.ws.id,
      connected: this.connected,
      host: this.host,
      port: this.port,
      spawnedPid: this.spawnedPid,
    };
  }

  // Ensure a daemon is listening; spawn one from the repo venv otherwise,
  // with the workspace dir as cwd so tools operate inside the project.
  async ensureRunning() {
    try {
      return await pingOnce(this);
    } catch (_err) {
      // not running — fall through and spawn
    }
    const pythonBin = path.join(this.repoRoot, ".venv", "bin", "python");
    if (!fs.existsSync(pythonBin)) {
      throw new Error(
        `未找到后端可执行文件 ${pythonBin}；请先在仓库根目录运行 bootstrap.sh 创建虚拟环境`,
      );
    }
    const child = spawn(pythonBin, ["-m", "mai_code.core"], {
      cwd: this.ws.path || this.repoRoot,
      detached: true,
      stdio: "ignore",
      env: { ...process.env, MAI_PORT: String(this.port) },
    });
    child.on("error", (err) => this.emit("log", `spawn failed: ${err.message}`));
    child.unref();
    this.spawnedPid = child.pid;

    const deadline = Date.now() + SPAWN_WAIT_MS;
    while (Date.now() < deadline) {
      try {
        return await pingOnce(this, 1_000);
      } catch (_err) {
        await new Promise((r) => setTimeout(r, SPAWN_POLL_INTERVAL_MS));
      }
    }
    throw new Error("daemon 启动超时（15s）");
  }

  // Persistent connection; the promise settles once the socket is actually
  // up, so callers can issue requests right after awaiting it.
  connect() {
    this.closedByUs = false;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.socket) {
      this.socket.removeAllListeners();
      this.socket.destroy();
    }
    return new Promise((resolve, reject) => {
      const socket = net.createConnection({ host: this.host, port: this.port });
      this.socket = socket;
      let settled = false;
      const settle = (fn, arg) => {
        if (!settled) {
          settled = true;
          fn(arg);
        }
      };

      socket.setTimeout(CONNECT_TIMEOUT_MS, () => {
        socket.destroy(new Error("connect timeout"));
      });
      socket.on("error", (err) => {
        settle(reject, err);
        this._onDown(err.message);
      });
      socket.on("close", () => this._onDown("connection closed"));
      socket.on("connect", () => {
        socket.setTimeout(0);
        this.buffer = "";
        this.connected = true;
        this.emit("status", { ...this.status(), note: "connected" });
        this._subscribeAll();
        settle(resolve);
      });
      socket.on("data", (chunk) => this._onData(chunk));
    });
  }

  // Subscribe once to every event topic; the daemon pushes them as
  // {"kind":"event","event":{...}} lines on this socket.
  _subscribeAll() {
    this.request("event.subscribe", { topics: ["*"], scope: "global" }).catch(
      (err) => this.emit("log", `event.subscribe failed: ${err.message}`),
    );
  }

  _onData(chunk) {
    this.buffer += chunk.toString("utf8");
    let nl;
    while ((nl = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (!line) continue;
      let msg;
      try {
        msg = JSON.parse(line);
      } catch (_err) {
        this.emit("log", "dropping unparseable line");
        continue;
      }
      if (msg.kind === "event" && msg.event) {
        this.emit("event", msg.event);
      } else if (msg.id !== undefined && this.pending.has(msg.id)) {
        const { resolve, reject, timer } = this.pending.get(msg.id);
        clearTimeout(timer);
        this.pending.delete(msg.id);
        if (msg.error) reject(new Error(`${msg.error.message} (code ${msg.error.code})`));
        else resolve(msg.result);
      }
    }
  }

  _onDown(reason) {
    const wasConnected = this.connected;
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(new Error(`daemon 连接断开: ${reason}`));
    }
    this.pending.clear();
    this.connected = false;
    if (wasConnected) this.emit("status", { ...this.status(), note: "disconnected" });

    if (!this.closedByUs && !this.reconnectTimer) {
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        this.connect().catch(() => {}); // next _onDown schedules another retry
      }, RECONNECT_DELAY_MS);
    }
  }

  request(method, params = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
    if (!this.connected || !this.socket) {
      return Promise.reject(new Error("daemon 未连接"));
    }
    const id = crypto.randomUUID();
    const frame = {
      jsonrpc: "2.0",
      id,
      method,
      params: { type: method, ...params },
    };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`request timeout: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.write(JSON.stringify(frame) + "\n");
    });
  }

  destroy() {
    this.closedByUs = true;
    clearTimeout(this.reconnectTimer);
    if (this.socket) this.socket.destroy();
    this.connected = false;
  }
}

class DaemonManager extends EventEmitter {
  constructor({ repoRoot }) {
    super();
    this.repoRoot = repoRoot;
    this.clients = new Map(); // wsId -> DaemonClient
  }

  clientFor(ws) {
    let client = this.clients.get(ws.id);
    if (!client) {
      client = new DaemonClient({ repoRoot: this.repoRoot, ws });
      client.on("event", (event) => this.emit("event", { wsId: ws.id, event }));
      client.on("status", (status) => this.emit("status", status));
      client.on("log", (msg) => this.emit("log", `[${ws.name}] ${msg}`));
      this.clients.set(ws.id, client);
    }
    return client;
  }

  async connect(ws) {
    const client = this.clientFor(ws);
    const pong = await client.ensureRunning();
    if (!client.connected) await client.connect();
    return { ok: true, pong, ...client.status() };
  }

  request(wsId, method, params) {
    const client = this.clients.get(wsId);
    if (!client) return Promise.reject(new Error("工作区未连接"));
    return client.request(method, params);
  }

  // Sessions live in the shared ~/.mai/sessions store; the desktop tags them
  // with workspaces locally (see renderer).
  listSessions(limit = 100) {
    const root = path.join(os.homedir(), ".mai", "sessions");
    let entries;
    try {
      entries = fs.readdirSync(root, { withFileTypes: true });
    } catch (_err) {
      return [];
    }
    const sessions = [];
    for (const ent of entries) {
      if (!ent.isDirectory() || !ent.name.startsWith("sess-")) continue;
      try {
        const meta = JSON.parse(
          fs.readFileSync(path.join(root, ent.name, "meta.json"), "utf8"),
        );
        sessions.push({
          id: meta.id ?? ent.name,
          title: meta.title || "",
          mode: meta.mode ?? "chat",
          status: meta.status ?? "closed",
          createdAt: meta.created_at ?? "",
          updatedAt: meta.updated_at ?? "",
        });
      } catch (_err) {
        // skip unreadable session dirs
      }
    }
    sessions.sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1));
    return sessions.slice(0, limit);
  }
}

module.exports = { DaemonManager };
