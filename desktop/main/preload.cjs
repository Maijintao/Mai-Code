// Context bridge: the renderer only ever sees these promises/callbacks,
// never Node, never a raw socket.

const { contextBridge, ipcRenderer } = require("electron");

function subscribe(channel, callback) {
  const handler = (_event, ...args) => callback(...args);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

contextBridge.exposeInMainWorld("mai", {
  connect: (ws) => ipcRenderer.invoke("mai:connect", ws),
  request: (wsId, method, params) =>
    ipcRenderer.invoke("mai:request", wsId, method, params),
  listSessions: () => ipcRenderer.invoke("mai:listSessions"),
  pickFolder: () => ipcRenderer.invoke("mai:pickFolder"),
  probeLlm: (cfg) => ipcRenderer.invoke("mai:probeLlm", cfg),
  onEvent: (cb) => subscribe("mai:event", cb), // ({wsId, event})
  onStatus: (cb) => subscribe("mai:status", cb), // ({wsId, connected, ...})
});
