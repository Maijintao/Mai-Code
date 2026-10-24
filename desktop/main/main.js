// Electron main process: window lifecycle + the bridge between the sandboxed
// renderer and the per-workspace mai-core daemons.

const { app, BrowserWindow, dialog, ipcMain } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { DaemonManager } = require("./daemon");

const repoRoot = path.resolve(__dirname, "..", "..");
const manager = new DaemonManager({ repoRoot });

let win = null;

function createWindow() {
  win = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 1000,
    minHeight: 660,
    title: "Mai-Code",
    backgroundColor: "#0e1116",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.loadFile(path.join(__dirname, "..", "renderer", "index.html"), {
    query: {
      ...(process.env.MAI_E2E_GOAL ? { e2e: process.env.MAI_E2E_GOAL } : {}),
      ...(process.env.MAI_E2E_VIEW ? { view: process.env.MAI_E2E_VIEW } : {}),
      ...(process.env.MAI_E2E_THEME ? { theme: process.env.MAI_E2E_THEME } : {}),
    },
  });
  win.on("closed", () => {
    win = null;
  });
}

function forward(channel) {
  return (...args) => {
    if (win && !win.isDestroyed()) win.webContents.send(channel, ...args);
  };
}

manager.on("event", forward("mai:event"));
manager.on("status", forward("mai:status"));
manager.on("log", (msg) => console.log(`[daemon] ${msg}`));

ipcMain.handle("mai:connect", async (_e, ws) => {
  try {
    return await manager.connect(ws);
  } catch (err) {
    return { ok: false, error: err.message, wsId: ws.id };
  }
});

ipcMain.handle("mai:request", (_e, wsId, method, params) =>
  manager.request(wsId, method, params),
);

ipcMain.handle("mai:listSessions", () => manager.listSessions());

ipcMain.handle("mai:pickFolder", async () => {
  if (!win) return null;
  const res = await dialog.showOpenDialog(win, {
    title: "选择项目文件夹",
    properties: ["openDirectory", "createDirectory"],
  });
  return res.canceled ? null : res.filePaths[0];
});

// Model probe happens in the daemon (config.probe_llm) when connected; this
// direct HTTP probe is the fallback used from the settings panel before any
// daemon accepts a new endpoint. Mirrors core/llm/probe.py's strategies.
ipcMain.handle("mai:probeLlm", async (_e, { base_url, api_key }) => {
  const base = (base_url || "").trim().replace(/\/+$/, "");
  const key = (api_key || "").trim();
  if (!/^https?:\/\//.test(base)) {
    return { ok: false, models: [], error: "base_url 需要以 http(s):// 开头" };
  }
  const origin = base.replace(/\/anthropic$/, "");
  const urls = [...new Set([`${base}/v1/models`, `${origin}/v1/models`, `${origin}/models`])];
  const headers = {};
  if (key) {
    headers["x-api-key"] = key;
    headers.Authorization = `Bearer ${key}`;
  }
  for (const url of urls) {
    try {
      const resp = await fetch(url, { headers, signal: AbortSignal.timeout(6_000) });
      if (!resp.ok) continue;
      const body = await resp.json();
      const ids = (Array.isArray(body?.data) ? body.data : [])
        .map((m) => m?.id)
        .filter((v) => typeof v === "string" && v);
      if (ids.length) return { ok: true, models: [...new Set(ids)].sort(), endpoint: url };
    } catch (_err) {
      // try next candidate
    }
  }
  return { ok: false, models: [], error: "无法从端点获取模型列表（检查 URL / Key / 网络）" };
});

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
  maybeScreenshot();
});

app.on("window-all-closed", () => {
  app.quit();
});

// Dev helper: MAI_SCREENSHOT=/abs/path.png launches, waits, captures the
// page and quits — lets CI/agents verify the UI without macOS screen
// recording permissions. Pair with MAI_E2E_GOAL / MAI_E2E_VIEW / MAI_E2E_THEME.
function maybeScreenshot() {
  const target = process.env.MAI_SCREENSHOT;
  if (!target || !win) return;
  const delay = Number(process.env.MAI_SCREENSHOT_DELAY_MS || 4000);
  setTimeout(async () => {
    try {
      const image = await win.webContents.capturePage();
      fs.writeFileSync(target, image.toPNG());
      console.log(`[screenshot] saved to ${target}`);
    } catch (err) {
      console.error(`[screenshot] failed: ${err.message}`);
    } finally {
      app.quit();
    }
  }, delay);
}
