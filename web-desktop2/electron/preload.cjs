/* 渲染进程只拿到两件小事：打开日志目录、问日志目录在哪。 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("glyphDesktop", {
  openLogs: () => ipcRenderer.invoke("glyph:open-logs"),
  logPath: () => ipcRenderer.invoke("glyph:log-path"),
});
