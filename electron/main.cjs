/* eslint-disable @typescript-eslint/no-require-imports */

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const { execFile, spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const apiProxy = require("./api-proxy.cjs");

const API_HOST = apiProxy.API_HOST;
const API_PORT = apiProxy.API_PORT;
const HEALTH_PATH = "/api/health";

let mainWindow = null;
let rendererServer = null;
let backendProcess = null;
let backendLogFd = null;
let backendStartError = null;
let ownsBackend = false;
let isQuitting = false;
// 本机后端的共享令牌（闸门要求非 GET 带上它）。空串 = 那台服务没启用令牌。
let backendToken = "";
let backendHealth = null;

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
}

function getBackendEntry() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "backend", "main.py");
  }
  return path.join(__dirname, "..", "backend", "main.py");
}

function getRendererRoot() {
  return app.isPackaged
    ? path.join(app.getAppPath(), "out")
    : path.join(__dirname, "..", "out");
}

function getIconPath() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "icon.png")
    : path.join(__dirname, "..", "desktop-assets", "icon.png");
}

function getLogPath() {
  return path.join(app.getPath("logs"), "backend.log");
}

// ── 自绘标题栏窗口控制 ──
ipcMain.handle("window:minimize", () => mainWindow?.minimize());
ipcMain.handle("window:toggle-maximize", () => {
  if (!mainWindow) return false;
  if (mainWindow.isMaximized()) {
    mainWindow.unmaximize();
    return false;
  }
  mainWindow.maximize();
  return true;
});
ipcMain.handle("window:close", () => mainWindow?.close());
ipcMain.handle("window:is-maximized", () => !!mainWindow?.isMaximized());

// ── 导出音轨：让用户自己选目录与文件名（浏览器模式只能用默认下载目录） ──
const JOB_ID_PATTERN = /^[0-9a-f]{12}$/;
const STEM_PATTERN = /^[a-z][a-z_]{0,19}$/;

function fetchStemToFile(jobId, stem, destination) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host: API_HOST,
        port: API_PORT,
        path: `/api/download/${jobId}/${encodeURIComponent(stem)}`,
        method: "GET",
        timeout: 120000,
      },
      (response) => {
        if (response.statusCode !== 200) {
          response.resume();
          reject(new Error(`音轨文件读不到（HTTP ${response.statusCode}），可能已被自动清理。`));
          return;
        }
        const stream = fs.createWriteStream(destination);
        response.pipe(stream);
        stream.on("finish", () => {
          stream.close(() => resolve(fs.statSync(destination).size));
        });
        stream.on("error", (error) => {
          fs.unlink(destination, () => {});
          reject(error);
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error("读取音轨超时")));
    request.on("error", (error) => reject(error));
    request.end();
  });
}

ipcMain.handle("vocal:save-stem", async (_event, payload) => {
  const jobId = String(payload?.jobId || "");
  const stem = String(payload?.stem || "");
  const suggested = String(payload?.suggestedName || "stem.wav").replace(/[\\/:*?"<>|]+/g, "-");
  if (!JOB_ID_PATTERN.test(jobId) || !STEM_PATTERN.test(stem)) {
    return { saved: false, reason: "参数不合法，未写入任何文件。" };
  }

  const target = await dialog.showSaveDialog(mainWindow, {
    title: "保存音轨",
    defaultPath: path.join(app.getPath("downloads"), suggested.endsWith(".wav") ? suggested : `${suggested}.wav`),
    filters: [{ name: "WAV 音频", extensions: ["wav"] }],
  });
  if (target.canceled || !target.filePath) return { saved: false, reason: "cancelled" };

  try {
    const bytes = await fetchStemToFile(jobId, stem, target.filePath);
    return { saved: true, path: target.filePath, bytes };
  } catch (error) {
    return { saved: false, reason: error instanceof Error ? error.message : String(error) };
  }
});

ipcMain.handle("vocal:reveal-path", (_event, targetPath) => {
  const resolved = path.resolve(String(targetPath || ""));
  if (!fs.existsSync(resolved)) return false;
  shell.showItemInFolder(resolved);
  return true;
});

function checkBackend(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const request = http.get(
      {
        host: API_HOST,
        port: API_PORT,
        path: HEALTH_PATH,
        timeout: timeoutMs,
      },
      (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          try {
            const payload = JSON.parse(body);
            if (response.statusCode === 200 && payload.status === "ok") {
              backendHealth = payload;
              resolve(true);
              return;
            }
            resolve(false);
          } catch {
            resolve(false);
          }
        });
      },
    );
    request.on("timeout", () => request.destroy());
    request.on("error", () => resolve(false));
  });
}

async function waitForBackend(maxWaitMs = 45000) {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    if (backendStartError) return false;
    if (await checkBackend()) return true;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

function resolveCommandPath(command) {
  const normalized = command.trim().replace(/^"(.*)"$/, "$1");
  if (!normalized) return null;
  if (path.isAbsolute(normalized)) {
    return fs.existsSync(normalized) ? normalized : null;
  }

  const lookupCommand = process.platform === "win32"
    ? path.join(process.env.SystemRoot || "C:\\Windows", "System32", "where.exe")
    : "which";
  const lookup = spawnSync(lookupCommand, [normalized], {
    encoding: "utf8",
    timeout: 10000,
    windowsHide: true,
  });
  if (lookup.status !== 0) return null;

  return lookup.stdout
    .split(/\r?\n/)
    .map((entry) => entry.trim())
    .find((entry) => entry && fs.existsSync(entry)) || null;
}

function findPythonRuntime() {
  const candidates = [];
  if (process.env.VOCAL_SEPARATOR_PYTHON) {
    candidates.push({
      command: process.env.VOCAL_SEPARATOR_PYTHON,
      prefixArgs: [],
    });
  }
  candidates.push(
    { command: "python.exe", prefixArgs: [] },
    { command: "python", prefixArgs: [] },
    { command: "py.exe", prefixArgs: ["-3"] },
    { command: "py", prefixArgs: ["-3"] },
  );

  for (const candidate of candidates) {
    const commandPath = resolveCommandPath(candidate.command);
    if (!commandPath) continue;
    const probe = spawnSync(
      commandPath,
      [
        ...candidate.prefixArgs,
        "-c",
        "import fastapi, uvicorn, demucs, torch; print('ok')",
      ],
      {
        encoding: "utf8",
        timeout: 20000,
        windowsHide: true,
      },
    );
    if (probe.status === 0 && probe.stdout.includes("ok")) {
      return { ...candidate, command: commandPath };
    }
  }
  return null;
}

async function ensureBackend() {
  const dataDir = path.join(app.getPath("userData"), "data");
  if (await checkBackend()) {
    // 附加到别人（start.bat / agent:serve）已经起来的后端：
    // 按它自己报的 guard.token_file 找回令牌，找不回来就如实失败，不留半能跑的界面。
    const resolved = apiProxy.tokenForRunningBackend(backendHealth, { dataDir });
    backendToken = resolved.token;
    if (resolved.problem) throw new Error(resolved.problem);
    return;
  }

  const backendEntry = getBackendEntry();
  if (!fs.existsSync(backendEntry)) {
    throw new Error(`未找到 AI 后端文件：${backendEntry}`);
  }

  const python = findPythonRuntime();
  if (!python) {
    throw new Error(
      "未找到可用的 Python AI 环境。请安装 Python、PyTorch、FastAPI 和 Demucs。",
    );
  }

  fs.mkdirSync(dataDir, { recursive: true });
  fs.mkdirSync(path.dirname(getLogPath()), { recursive: true });
  backendLogFd = fs.openSync(getLogPath(), "a");
  backendStartError = null;
  // 桌面壳自己起的后端：默认启用共享令牌（env 里已有就用已有的），并把同一份写进
  // <数据目录>/security.json —— 那是 README 与 /api/health 的 data.guard 里登记的发现方式。
  const guard = apiProxy.resolveToken({ dataDir, create: true });
  backendToken = guard.token;
  fs.writeSync(
    backendLogFd,
    `\n[${new Date().toISOString()}] Starting backend with ${python.command} (token source: ${guard.source})\n`,
  );

  backendProcess = spawn(
    python.command,
    [...python.prefixArgs, backendEntry],
    {
      cwd: path.dirname(backendEntry),
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONUNBUFFERED: "1",
        VOCAL_SEPARATOR_DATA_DIR: dataDir,
        VOCAL_SEPARATOR_TOKEN: guard.token,
      },
      windowsHide: true,
      stdio: ["ignore", backendLogFd, backendLogFd],
    },
  );
  ownsBackend = true;

  backendProcess.once("error", (error) => {
    backendStartError = error;
    if (backendLogFd !== null) {
      fs.writeSync(backendLogFd, `[spawn error] ${error.stack || error}\n`);
    }
  });

  const ready = await waitForBackend();
  if (!ready) {
    const reason = backendStartError
      ? `无法启动 Python：${backendStartError.message}`
      : "AI 后端在 45 秒内未能启动";
    throw new Error(`${reason}。日志：${getLogPath()}`);
  }
}

function proxyApiRequest(request, response) {
  return apiProxy.proxyApiRequest(request, response, {
    host: API_HOST,
    port: API_PORT,
    token: backendToken,
  });
}

const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function serveStaticFile(root, request, response) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    response.writeHead(405, { Allow: "GET, HEAD" });
    response.end();
    return;
  }

  let pathname;
  try {
    pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
  } catch {
    response.writeHead(400);
    response.end();
    return;
  }

  const relativePath = pathname === "/" ? "index.html" : pathname.slice(1);
  const resolvedRoot = path.resolve(root);
  let filePath = path.resolve(root, relativePath);
  if (filePath !== resolvedRoot && !filePath.startsWith(`${resolvedRoot}${path.sep}`)) {
    response.writeHead(403);
    response.end();
    return;
  }

  if (fs.existsSync(filePath) && fs.statSync(filePath).isDirectory()) {
    filePath = path.join(filePath, "index.html");
  }
  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    filePath = path.join(root, "404.html");
  }
  if (!fs.existsSync(filePath)) {
    response.writeHead(404);
    response.end();
    return;
  }

  const extension = path.extname(filePath).toLowerCase();
  const cacheControl = filePath.includes(`${path.sep}_next${path.sep}static${path.sep}`)
    ? "public, max-age=31536000, immutable"
    : "no-cache";
  response.writeHead(200, {
    "Cache-Control": cacheControl,
    "Content-Type": MIME_TYPES[extension] || "application/octet-stream",
    "X-Content-Type-Options": "nosniff",
  });
  if (request.method === "HEAD") {
    response.end();
    return;
  }
  fs.createReadStream(filePath).pipe(response);
}

function startRendererServer() {
  const root = getRendererRoot();
  if (!fs.existsSync(path.join(root, "index.html"))) {
    throw new Error(`未找到桌面页面资源：${root}`);
  }

  return new Promise((resolve, reject) => {
    rendererServer = http.createServer((request, response) => {
      if (request.url === "/api" || request.url.startsWith("/api/")) {
        proxyApiRequest(request, response);
        return;
      }
      serveStaticFile(root, request, response);
    });
    rendererServer.once("error", reject);
    rendererServer.listen(0, API_HOST, () => {
      const address = rendererServer.address();
      resolve(`http://${API_HOST}:${address.port}`);
    });
  });
}

function createWindow(rendererOrigin) {
  Menu.setApplicationMenu(null);
  mainWindow = new BrowserWindow({
    title: "声析 - 本地 AI 音轨分离",
    width: 1280,
    height: 860,
    minWidth: 920,
    minHeight: 680,
    backgroundColor: "#071a20",
    icon: getIconPath(),
    show: false,
    frame: false,
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      preload: path.join(__dirname, "preload.cjs"),
    },
  });

  for (const ev of ["maximize", "unmaximize"]) {
    mainWindow.on(ev, () => mainWindow?.webContents.send("vocal:maximized", ev === "maximize"));
  }

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://") || url.startsWith("http://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith(rendererOrigin)) event.preventDefault();
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.loadURL(rendererOrigin);
}

function stopBackend() {
  if (!ownsBackend || !backendProcess || backendProcess.killed) return;
  const pid = backendProcess.pid;
  if (process.platform === "win32") {
    execFile("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
      windowsHide: true,
    });
  } else {
    backendProcess.kill("SIGTERM");
  }
}

async function bootstrap() {
  try {
    await ensureBackend();
    const rendererOrigin = await startRendererServer();
    createWindow(rendererOrigin);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    dialog.showErrorBox(
      "声析启动失败",
      `${message}\n\n请确认 Python AI 环境已安装。`,
    );
    app.quit();
  }
}

app.on("second-instance", () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  }
});

app.whenReady().then(bootstrap);

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  if (isQuitting) return;
  isQuitting = true;
  if (rendererServer) rendererServer.close();
  stopBackend();
  if (backendLogFd !== null) {
    try {
      fs.closeSync(backendLogFd);
    } catch {
      // The process may still own the log descriptor during shutdown.
    }
    backendLogFd = null;
  }
});
