/* Glyph 桌面端主进程。
 *
 * 这版的“出事能查、能救”照 DSH 的做法：
 *   · 日志落到 app.getPath("logs")（Electron 统一位置），后端输出逐行追加；
 *   · 崩溃写 JSON 报告（版本、阶段、时间、错误），不只 printf；
 *   · 后端起不来 → 弹窗给「重试 / 查看日志 / 退出」，重试换端口再拉；
 *   · 窗口大小与位置记在 userData/window-state.json，屏幕变了就回默认。
 */
const { app, BrowserWindow, shell, dialog, ipcMain, Tray, Menu, nativeImage, protocol, net: electronNet } = require("electron");
const { spawn } = require("node:child_process");
const net = require("node:net");
const path = require("node:path");
const fs = require("node:fs");
const http = require("node:http");

// 启动自检日志（打包后看不到控制台时用来定位"点开就退"）
function bootLog(msg) {
  try {
    fs.appendFileSync(path.join(process.env.TEMP || ".", "glyph-boot.log"), new Date().toISOString() + " " + msg + "\n", "utf8");
  } catch { /* 忽略 */ }
}

const isDev = !app.isPackaged;
bootLog("boot isPackaged=" + app.isPackaged + " resources=" + process.resourcesPath);
const projectRoot = path.resolve(__dirname, "..", ".."); // markr/
let backend = null;
let win = null;
let tray = null;
let backendReady = false;
let backendPort = 0;
let shuttingDown = false;
let quitting = false;

// 单实例：第二次双击只把已有窗口叫到前面
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  bootLog("single-instance: lock not acquired, quitting");
  app.quit();
} else {
  app.on("second-instance", () => {
    bootLog("second-instance received win=" + Boolean(win));
    if (!win || win.isDestroyed()) { void createWindow(); return; }
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
    bootLog("second-instance show -> isVisible=" + win.isVisible());
  });
}

app.setAppLogsPath();

// 固定来源：桌面端每次后端端口不同，如果直接用 http://127.0.0.1:<端口>，
// 浏览器会把它当成不同网站，localStorage（主题/字号/上次会话等）每次全部重置。
// DSH 用的是 dsh-app:// 自定义协议，这里照做：glyph://app/ 始终是同一个来源。
protocol.registerSchemesAsPrivileged([
  { scheme: "glyph", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } },
]);

/* ---------- 日志 ---------- */
function logDir() {
  return app.getPath("logs");
}

function logLine(stream, text) {
  try {
    const dir = logDir();
    fs.mkdirSync(dir, { recursive: true });
    const day = new Date().toISOString().slice(0, 10);
    const file = path.join(dir, "backend-" + day + ".log");
    const line = "[" + new Date().toISOString() + "][" + stream + "] " + String(text);
    fs.appendFileSync(file, line.endsWith("\n") ? line : line + "\n", "utf8");
    // 简单上限：超过 5MB 先把旧文件改名留一份
    const st = fs.statSync(file);
    if (st.size > 5 * 1024 * 1024) {
      try { fs.renameSync(file, file + ".1"); } catch { /* 忽略 */ }
    }
  } catch {
    /* 日志写不进去不能影响主流程 */
  }
}

function writeCrashReport(source, error) {
  try {
    const dir = logDir();
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, "crash-" + Date.now() + ".json");
    const payload = {
      source,
      phase: backendReady ? "running" : "startup",
      message: error && error.message ? error.message : String(error),
      stack: error && error.stack ? error.stack : null,
      app: {
        name: app.getName(),
        version: app.getVersion(),
        platform: process.platform,
        arch: process.arch,
        electron: process.versions.electron,
        node: process.versions.node,
        locale: app.getLocale(),
      },
      time: new Date().toISOString(),
    };
    fs.writeFileSync(file, JSON.stringify(payload, null, 2), "utf8");
    return file;
  } catch {
    return null;
  }
}

process.on("uncaughtException", (err) => {
  logLine("main", "uncaughtException: " + (err && err.stack ? err.stack : String(err)));
  writeCrashReport("main-process", err);
});
process.on("unhandledRejection", (reason) => {
  logLine("main", "unhandledRejection: " + String(reason));
});

/* ---------- 托盘（照 DSH：关窗只是隐藏，右键菜单退出） ---------- */
function trayIconPath() {
  return isDev
    ? path.join(projectRoot, "web-desktop2", "build", "icon.ico")
    : path.join(process.resourcesPath, "icon.ico");
}

function showMainWindow() {
  bootLog("showMainWindow win=" + Boolean(win));
  if (!win || win.isDestroyed()) { void createWindow(); return; }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function buildTray() {
  if (tray) return;
  try {
    const img = nativeImage.createFromPath(trayIconPath());
    tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img);
    tray.setToolTip("Glyph");
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: "打开 Glyph", click: () => showMainWindow() },
      { type: "separator" },
      { label: "重启后端", click: () => { void restartBackend(); } },
      { label: "打开日志文件夹", click: () => { void shell.openPath(logDir()); } },
      { type: "separator" },
      { label: "退出", click: () => { quitting = true; app.quit(); } },
    ]));
    tray.on("click", () => showMainWindow());
    tray.on("double-click", () => showMainWindow());
  } catch (err) {
    logLine("main", "tray 创建失败: " + String(err));
  }
}

/* ---------- 后端 ---------- */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitHealthy(port, timeoutMs = 40000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = () => {
      const req = http.get({ host: "127.0.0.1", port, path: "/api/config", timeout: 1500 }, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on("error", retry);
      req.on("timeout", () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (shuttingDown) return reject(new Error("正在退出"));
      if (Date.now() - started > timeoutMs) return reject(new Error("后端启动超时"));
      setTimeout(tick, 400);
    };
    tick();
  });
}

function startBackend(port) {
  if (process.env.GLYPH_TEST_FAIL_BACKEND === "1") {
    throw new Error("测试用：故意让后端起不来");
  }
  const packaged = path.join(process.resourcesPath || "", "backend", "glyph-backend.exe");
  const dist = isDev
    ? path.join(projectRoot, "web-desktop2", "dist")
    : path.join(process.resourcesPath, "web");
  const env = { ...process.env, PYTHONPATH: path.join(projectRoot, "src"), GLYPH_WEB_DIST: dist };
  const devExe = path.join(projectRoot, "dist", "glyph-backend.exe");
  const cmd = !isDev
    ? { file: packaged, args: ["--port", String(port)], cwd: path.dirname(packaged) }
    : fs.existsSync(devExe)
      ? { file: devExe, args: ["--port", String(port)], cwd: path.dirname(devExe) }
      : { file: path.join(projectRoot, ".venv", "Scripts", "python.exe"), args: ["-m", "uvicorn", "web.server:app", "--host", "127.0.0.1", "--port", String(port)], cwd: projectRoot };
  if (!isDev && !fs.existsSync(packaged)) {
    throw new Error("找不到打包好的后端（resources/backend/glyph-backend.exe）");
  }
  const child = spawn(cmd.file, cmd.args, { cwd: cmd.cwd, env, windowsHide: true });
  child.stdout.on("data", (d) => { const s = String(d); process.stdout.write("[backend] " + s); logLine("out", s.trimEnd()); });
  child.stderr.on("data", (d) => { const s = String(d); process.stderr.write("[backend] " + s); logLine("err", s.trimEnd()); });
  child.on("exit", (code) => {
    logLine("main", "backend exited code=" + code);
    if (!shuttingDown) {
      backendReady = false;
      writeCrashReport("backend-exit", new Error("backend exited with code " + code));
      void offerRecovery("后端进程退出了（代码 " + code + "）");
    }
  });
  return child;
}

/* ---------- 恢复弹窗（照 DSH：重试 / 看日志 / 退出） ---------- */
let recovering = false;
async function offerRecovery(detail) {
  if (recovering || shuttingDown) return;
  recovering = true;
  try {
    const choice = dialog.showMessageBoxSync(win ?? undefined, {
      type: "error",
      title: "Glyph 启动失败",
      message: "Glyph 的后端没有启动起来",
      detail: (detail ? detail + "\n\n" : "") + "可能原因：端口被占用、被杀毒软件拦了，或文件损坏。\n日志位置：" + logDir(),
      buttons: ["重试", "查看日志", "退出"],
      defaultId: 0,
      cancelId: 2,
      noLink: true,
    });
    if (choice === 1) {
      void shell.openPath(logDir());
      recovering = false;
      await offerRecovery(detail);
      return;
    }
    if (choice === 2) {
      shuttingDown = true;
      app.quit();
      return;
    }
    await restartBackend();
  } finally {
    recovering = false;
  }
}

async function restartBackend() {
  try {
    if (backend && !backend.killed) backend.kill();
    backend = null;
    backendReady = false;
    const port = await freePort();
    backendPort = port;
    backend = startBackend(port);
    await waitHealthy(port);
    backendReady = true;
    if (win && !win.isDestroyed()) await win.loadURL("glyph://app/");
  } catch (err) {
    writeCrashReport("startup", err);
    await offerRecovery(err && err.message ? err.message : String(err));
  }
}

/* ---------- 窗口 ---------- */
async function createWindow() {
  // 重建窗口时先收掉旧后端，避免端口和进程越积越多
  if (backend && !backend.killed) { backend.kill(); backend = null; }
  const port = await freePort();
  backendPort = port;
  backend = startBackend(port);
  await waitHealthy(port);
  backendReady = true;

  // 每次打开都用固定默认尺寸并居中，不沿用上次的窗口状态
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    center: true,
    minWidth: 980,
    minHeight: 640,
    backgroundColor: "#0c0e11",
    title: "Glyph",
    icon: path.join(projectRoot, "web-desktop2", "build", "icon.png"),
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });
  // 关窗只是隐藏到托盘（不弹提醒）；真正退出走托盘「退出」
  win.on("close", (event) => {
    if (quitting || shuttingDown) return;
    event.preventDefault();
    win.hide();
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    logLine("main", "render-process-gone: " + JSON.stringify(details));
    writeCrashReport("renderer-gone", new Error(JSON.stringify(details)));
  });
  await win.loadURL("glyph://app/");
}

// 只有拿到单实例锁的那个进程才真正启动界面/后端；
// 第二个实例只负责把事件转给第一个，然后退出（否则会多起一份后端）。
if (gotLock) {
app.whenReady().then(() => {
  protocol.handle("glyph", async (request) => {
    const u = new URL(request.url);
    const target = "http://127.0.0.1:" + backendPort + u.pathname + u.search;
    try {
      // 只转发必要头：把 Origin/Referer/Sec-Fetch-* 一起转给本地后端会被拒绝，
      // 表现为 ERR_FAILED / 连接被重置（脚本和样式表全 502）。
      const headers = {};
      for (const name of ["accept", "accept-language", "content-type", "authorization", "range"]) {
        const value = request.headers.get(name);
        if (value) headers[name] = value;
      }
      const init = { method: request.method, headers, redirect: "manual" };
      if (request.method !== "GET" && request.method !== "HEAD") {
        // 请求体先读成字节再转发：把 ReadableStream 直接交给 net.fetch 需要 duplex，
        // 在 Electron 里会 ERR_FAILED（所有 POST/PUT 都会变成 502）。
        init.body = Buffer.from(await request.arrayBuffer());
      }
      return await electronNet.fetch(target, init);
    } catch (err) {
      logLine("main", "proxy 失败: " + String(err && err.message ? err.message : err));
      return new Response("后端暂不可用", { status: 502 });
    }
  });
  ipcMain.handle("glyph:open-logs", async () => {
    await shell.openPath(logDir());
    return logDir();
  });
  ipcMain.handle("glyph:log-path", () => logDir());
  bootLog("app ready");
  buildTray();
  createWindow().catch(async (err) => {
    bootLog("createWindow failed: " + (err && err.stack ? err.stack : String(err)));
    logLine("main", "createWindow failed: " + (err && err.stack ? err.stack : String(err)));
    writeCrashReport("startup", err);
    await offerRecovery(err && err.message ? err.message : String(err));
  });
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});
}

app.on("before-quit", () => {
  quitting = true;
  shuttingDown = true;
  if (tray) { tray.destroy(); tray = null; }
  if (backend && !backend.killed) backend.kill();
});

// 托盘常驻：关闭所有窗口不退出应用（Windows/Linux），macOS 本来就常驻
app.on("window-all-closed", () => { /* 留在托盘 */ });
