const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("vocal", {
  windowControls: {
    minimize: () => ipcRenderer.invoke("window:minimize"),
    toggleMaximize: () => ipcRenderer.invoke("window:toggle-maximize"),
    close: () => ipcRenderer.invoke("window:close"),
    isMaximized: () => ipcRenderer.invoke("window:is-maximized"),
    onMaximizedChange: (cb) => {
      const h = (_e, v) => cb(v);
      ipcRenderer.on("vocal:maximized", h);
      return () => ipcRenderer.removeListener("vocal:maximized", h);
    },
  },
});
