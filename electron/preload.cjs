/* eslint-disable @typescript-eslint/no-require-imports */
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
  // 导出：主进程弹系统"另存为"，再把后端音轨写进用户选定的路径。
  saveStem: (payload) => ipcRenderer.invoke("vocal:save-stem", payload),
  revealPath: (targetPath) => ipcRenderer.invoke("vocal:reveal-path", targetPath),
});
