# 声析 · Agent API / MCP

按 `personal-agent-hub/docs/AGENT_API_STANDARD.md` 暴露的本机工具接口。只监听 `127.0.0.1`。

## 端口：8000（不新增端口）

本项目**本来就有常驻本地服务**：`backend/main.py` 的 FastAPI（`127.0.0.1:8000`），
Electron 壳（`electron/main.cjs`）、浏览器界面与 `start.bat` 连的都是它。
按标准的选择规则——已有常驻服务就把契约端点加进那个服务——所以：

| 项 | 值 |
| --- | --- |
| 端点位置 | `backend/main.py` 挂载 `backend/agent_api.py` 的 router |
| 地址 | `http://127.0.0.1:8000` |
| 工具声明 | `agent/tools.py`（7 个 `vocal.*`） |
| MCP 桥 | `agent/mcp-server.mjs`（标准模板，未改逻辑） |
| 自启动声明 | `agent/launch.json` → `python agent/server.py`，`ready_port: 8000` |
| 实际地址回执 | `agent/.endpoint`（后端启动时写，已 gitignore） |

个人 Agent 服务端口表里的 `vocal-separator = 8794` **没有被使用**：多开一个端口就要多开一份
任务表，Agent 提交的任务在界面里会看不见（同一次 `history.json`/`jobs` 只有一份才成立）。
注册 `config/catalog.json` 时请写 `"default_base": "http://127.0.0.1:8000"`、
`"start": {"command": "python agent/server.py"}`。

## 启动

界面在跑就已经在提供服务了。单独起（前台，Ctrl+C 停）：

```bash
npm run agent:serve          # = python agent/server.py
# 或直接：python backend/main.py
```

自检四个端点：

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/agent/tools
curl -s http://127.0.0.1:8000/api/agent/manifest
curl -s -X POST http://127.0.0.1:8000/api/agent/tool \
  -H 'content-type: application/json' \
  -d '{"tool":"vocal.env_probe","input":{}}'
```

`/api/health` 同时返回两种形状：顶层 `status:"ok"` + `gpu_available/demucs_available/
ffmpeg_available/model`（Electron 存活探针与 `scripts/wait-for-services.ps1` 读的就是这几个键），
以及标准信封 `ok:true` + `data:{project,version,agent_api,uptime_ms,...}`。

## 工具清单

`risk=exec` 需要显式 `confirm:true`。全部返回后端实测值（版本、路径、字节数都来自本机）。

| 工具 | risk | 作用 |
| --- | --- | --- |
| `vocal.env_probe` | read | python/torch/CUDA 设备名/demucs/ffmpeg 版本与路径、`htdemucs` 权重是否已缓存（含文件与 MB）、数据目录、在跑任务数 |
| `vocal.mode_list` | read | 音轨预设（四条音轨 / 人声+伴奏）、每轨中英文轨名、底层 Demucs 参数、接受格式、大小上限与保留时长 |
| `vocal.separate_submit` | exec | 按**本机文件路径**提交真实分离任务（复用界面的同一条 `run_separation` 链路），返回 job_id、输出目录、阶段；`wait:true` 可阻塞到结束 |
| `vocal.job_status` | read | 状态/进度/阶段（含中文阶段名）/已用与预计剩余秒数/排队位次/每条音轨的真实路径与字节数 |
| `vocal.job_wait` | read | 轮询到终态或超时（默认 300 秒，上限 1800），带 `timed_out` |
| `vocal.job_cancel` | write | 取消排队或进行中的任务：置标记并终止 Demucs 子进程、清理任务目录 |
| `vocal.output_list` | read | 历史输出索引（`history.json`）：源文件名+字节数、轨数、总字节、完成时间、每条轨的路径与是否仍在磁盘、`download_paths`；带 `name`+`bytes` 即"这首歌是否已分离过" |

调用形状：

```bash
# 提交（必须 confirm）
curl -s -X POST http://127.0.0.1:8000/api/agent/tool -H 'content-type: application/json' \
  -d '{"tool":"vocal.separate_submit","input":{"input_path":"C:/Music/song.mp3","preset":"vocal_backing","confirm":true}}'

# 查进度
curl -s -X POST http://127.0.0.1:8000/api/agent/tool -H 'content-type: application/json' \
  -d '{"tool":"vocal.job_status","input":{"job_id":"6c8eb405bc4e"}}'

# 历史（重复分离判定）
curl -s -X POST http://127.0.0.1:8000/api/agent/tool -H 'content-type: application/json' \
  -d '{"tool":"vocal.output_list","input":{"name":"short-mix.wav","bytes":1411244}}'
```

错误形状与标准一致：缺参数/未知参数 → HTTP 400 + `{"ok":false,"error":{"code":"bad_input"}}`；
未注册工具 → HTTP 400 + `error.code="unknown_tool"` + `error.available=[...]`；
不存在的 job → `{"ok":false,"error":{"code":"not_found"}}`。

## MCP

任何 MCP 客户端直接起 stdio 桥即可（`tools/list` 转发 `/api/agent/tools`，
`tools/call` 转发 `POST /api/agent/tool`）：

```bash
npm run agent:mcp            # = node agent/mcp-server.mjs
```

桥会依次试 `AGENT_BASE_URL` → `agent/.endpoint` → `agent/launch.json` 自启动。

## 本机已验证到什么程度（2026-10-02）

- 四种响应实测：`/api/health`（旧键 + 信封）/ `/api/agent/tools`（7 个）/ 成功调用 /
  `bad_input` 400 / `unknown_tool` 400（带 `available`）。
- MCP stdio 三条握手（`initialize` → protocol 2.2.0、`tools/list` → 7 个、
  `tools/call vocal.env_probe` → `isError:false` + 真值）都有响应。
- `vocal.env_probe` 返回的是本机真值：python 3.12.4、torch 2.6.0+cu124、
  `cuda_available=true`、NVIDIA GeForce RTX 3060 Laptop GPU、demucs 4.1.0、
  ffmpeg 6.1.1（仓库内 `backend/ffmpeg.exe`）、`model_cache.cached` 从 false 变成 true（含 80.1 MB）。
- 真实分离跑通两种预设（8 秒合成 WAV，`C:/Temp/vocal-samples/short-mix.wav`）：
  `four_stems` 四条轨 elapsed 18.9 秒（含首次下载权重）、`vocal_backing` 人声+伴奏 elapsed 8.9 秒；
  工具回执里每条轨的路径与字节数逐个和磁盘实际文件对过（`match=True`），并进入历史索引
  （`vocal.output_list` 按 `name`+`bytes` 能查回这三条）。
- `python -m unittest discover -s backend -v` 16 项全过；`npm run lint`、`npm run build` 全绿。
- **未验证**：桌面版「保存」对话框（需要真 Electron 窗口，本轮只做了静态构建与浏览器截图）。
