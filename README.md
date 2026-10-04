# vocal-separator (声析)

Local-first AI stem separation: split a song into **vocals / drums / bass / other** — or into **vocal + instrumental** — on your own machine. No cloud upload, no queue, no account.
本地 AI 音轨分离：用 [Demucs](https://github.com/adefossez/demucs) 在本机拆出人声 / 鼓组 / 贝斯 / 其他四条音轨，或直接得到人声 + 伴奏两轨，音频全程不出电脑。

Separating vocals usually means uploading your audio to an online service and waiting in line. **vocal-separator** runs the Demucs `htdemucs` model through a local FastAPI service instead: upload, separation, preview and export all stay on your computer. It ships as a Windows desktop app (Electron) and also works as a plain web UI at `localhost:3000`.

[中文说明](README.zh-CN.md)

![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows-blueviolet)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Demucs](https://img.shields.io/badge/Demucs-htdemucs-FF6F00)
![Next.js](https://img.shields.io/badge/Next.js-16-black?logo=next.js&logoColor=white)
![Electron](https://img.shields.io/badge/Electron-43-47848F?logo=electron&logoColor=white)

![vocal-separator UI](docs/screenshot.png)

## ✨ Features

- **Two track presets** — `四条音轨` (vocals / drums / bass / other) or `人声 + 伴奏` (vocal + instrumental, via Demucs `--two-stems`); every stem is a WAV file
- **Batch queue** — drop several files at once, they run one at a time (a Demucs job owns the GPU), each row shows upload percent, phase, elapsed time and an ETA
- **Cancellable, even when Demucs goes silent** — cancel a queued *or* running job; output is read on a watchdog thread and the loop wakes every 0.5 s (`VOCAL_SEPARATOR_CHILD_POLL`) to check the cancel flag and a wall-clock deadline (`VOCAL_SEPARATOR_JOB_TIMEOUT`, default 30 min), then escalates terminate → wait → kill and returns the GPU slot. A child that never exits is reaped by the same sequence, and both paths report honestly (`服务重启，未跑完` / 墙钟到点) rather than pretending the job finished
- **Machine health you can read** — the UI strip shows the real python / torch / CUDA device / Demucs / ffmpeg versions and whether the `htdemucs` weights are already cached
- **Actionable failures** — missing Demucs, missing ffmpeg, CUDA out of memory and expired results each come with the next step to take, not just an error string
- **Recent results** — a persistent index (`backend/history.json`) lists past separations with real file sizes, so re-dropping the same file asks "already separated, run again?" instead of silently burning 10 minutes
- **Export under your control** — preview in place; in the desktop app "保存" opens a real Save dialog (folder and filename yours), in the browser it downloads; the result directory is shown and can be opened
- **Common formats in** — MP3 / WAV / FLAC / OGG / M4A / AAC, plus MP4 / MOV / MKV / WebM / AVI (FFmpeg extracts the audio first), up to 500 MB
- **Restart-safe queue** — the job table is written to `backend/queue.json` (atomic) and recovered on boot: a job that was still queued and whose upload survived is put back in line, a job that was mid-flight is marked 服务重启，未跑完 — never silently "succeeded" — and `GET /api/health` → `data.recovery` reports what was recovered and which partial output directories/uploads were reclaimed
- **Self-cleaning** — partial output left by a crash is reclaimed at startup (with a report), and anything older than 1 hour is wiped; files you saved elsewhere are untouched
- **Agent API + MCP** — the same service exposes `vocal.*` tools for local agents over `127.0.0.1:8000` (see below)

## 🚀 Quick Start

Prerequisites: **Node.js ≥ 20.9** (Next.js 16) and **Python 3.10+**. An NVIDIA GPU helps — CPU works but is much slower. The first separation downloads the `htdemucs` weights once (~80 MB) into the torch/Hugging Face cache.

```powershell
git clone https://github.com/zhangtt08/vocal-separator.git
cd vocal-separator
npm install
python -m pip install -r backend/requirements.txt
```

Then double-click `start.bat` (one-click launcher for Windows), or start both services manually:

```powershell
python backend/main.py   # FastAPI backend  -> http://127.0.0.1:8000
npm run dev              # Next.js frontend -> http://localhost:3000
```

Open <http://localhost:3000>, drop in one or more songs, pick a preset, wait for the stems.
Desktop mode: `npm run desktop` (builds `out/`, then Electron serves it and spawns the backend itself).

### Configuration

| Variable | Effect |
| --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | point the frontend at a different backend address |
| `VOCAL_SEPARATOR_ORIGINS` | comma-separated frontend origins allowed by the backend (CORS + guard) |
| `VOCAL_SEPARATOR_TOKEN` | shared token; when set, every non-GET request must carry `x-vocal-token` |
| `VOCAL_SEPARATOR_DATA_DIR` | where uploads / outputs / history live (desktop app uses its userData dir) |
| `VOCAL_SEPARATOR_PORT` | backend port, default `8000` |
| `VOCAL_SEPARATOR_PYTHON` | which interpreter the desktop shell should launch the backend with |
| `VOCAL_SEPARATOR_FFMPEG` | explicit ffmpeg path (ffmpeg is looked up in exactly three places: that variable, `backend/ffmpeg(.exe)`, `PATH` — there is no pip ffmpeg) |
| `VOCAL_SEPARATOR_JOB_TIMEOUT` | wall-clock seconds one job may hold the GPU (default 1800) |
| `VOCAL_SEPARATOR_QUEUE_TIMEOUT` | how long a job waits in line before it gives up (default 3600) |
| `VOCAL_SEPARATOR_CHILD_POLL` | seconds between cancel/deadline checks while the child is silent (default 0.5) |

### Checks

```powershell
npm run verify      # typecheck + backend unittest + window-control UI test + desktop guard test
npm run lint
npm run build
python -m unittest discover -s backend -v   # or: npm run test:backend
npm run test:ui                             # window controls hydrate identically
npm run test:guard                          # desktop shell -> backend first-party path, end to end
```

## 🔒 It really is local-only

The service binds `127.0.0.1`, and on top of that every request passes one guard
(`backend/local_guard.py`, wired in `backend/main.py` as middleware, so routes cannot
forget it):

1. **Host pinning** — `Host` must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`;
   anything else is a JSON `403 host_forbidden`. This is what kills DNS rebinding: the
   attacker's domain still shows up in `Host`. The check never compares `Origin` against the
   request's own `Host` — under rebinding those two agree, which is the bug this rule avoids.
2. **Origin / Referer allowlist** — if a request carries one, it must be first party: a loopback
   origin on any port (the desktop shell's renderer server gets a random one) or an origin listed
   in `VOCAL_SEPARATOR_ORIGINS`. A cross-site `multipart/form-data` form POST — which needs no
   preflight and no CORS permission to *reach* the handler — is refused with `403 origin_forbidden`
   before a job is queued and before a byte is written. Absent `Origin`/`Referer` means a
   non-browser client (curl, the MCP bridge, the health probe), which is allowed.
3. **Shared token for non-GET** — when a token is configured, all state-changing requests must
   carry `x-vocal-token` (or `Authorization: Bearer`), compared with `hmac.compare_digest`.
   Missing → `401 token_required`, wrong → `403 token_mismatch`. Reads (`GET`/`HEAD`/`OPTIONS`)
   never need it. `Access-Control-Allow-Origin: *` is never sent on state-changing routes.

**Token discovery (what the desktop shell actually does)** — resolution order is
`VOCAL_SEPARATOR_TOKEN` → `<VOCAL_SEPARATOR_DATA_DIR>/security.json` (`{"token": "…"}`) → not
configured. `GET /api/health` answers `data.guard` with `token_required`, `token_header`,
`token_source` and the absolute `token_file` path — never the token itself — so any local
first-party process knows *where* to look. `GET /api/session-token` hands the token to a
first-party page (same-origin/loopback only, `Cache-Control: no-store`) and is what the browser
UI uses; `agent/mcp-server.mjs` reads `data.guard.token_file`; and `electron/main.cjs` (via
`electron/api-proxy.cjs`) generates a token when it starts the backend itself, writes it to
`security.json`, passes it to the child in `VOCAL_SEPARATOR_TOKEN` and injects the header into
its `/api/*` proxy. If it attaches to a backend that requires a token it cannot read, it fails
at startup with the reason instead of shipping a UI whose buttons 401.

If no token is configured, layers 1 and 2 still apply — that is the
`personal-agent-hub/docs/AGENT_API_STANDARD.md` "require a shared token on non-GET **when
configured**" rule, honored literally so that plain `python backend/main.py` (what the agent
hub and `start.bat` launch) keeps working unchanged.

## 🤖 Agent API / MCP

The FastAPI service is already resident (the desktop shell and the web UI both talk to it), so the
`personal-agent-hub` agent API contract (`docs/AGENT_API_STANDARD.md`) is mounted
**on the same port — no extra listener, no second job table**:

```
GET  http://127.0.0.1:8000/api/health          # legacy keys + standard {ok,data} envelope
GET  http://127.0.0.1:8000/api/agent/tools
GET  http://127.0.0.1:8000/api/agent/manifest
POST http://127.0.0.1:8000/api/agent/tool      # {"tool":"vocal.…","input":{…}}
```

Seven tools, all backed by the same functions the UI calls — versions, paths and byte counts are read
from this machine, never invented:

| Tool | Risk | What it does |
| --- | --- | --- |
| `vocal.env_probe` | read | python / torch / CUDA device / demucs / ffmpeg versions and paths, whether `htdemucs` weights are cached (with file sizes), data directories, running job count |
| `vocal.mode_list` | read | presets, track labels, underlying `demucs` arguments, accepted formats, size limit, retention window |
| `vocal.separate_submit` | exec | start a real separation for a file **already on disk** (requires `confirm:true`), returns job id, output directory, phase; `wait:true` blocks until it finishes |
| `vocal.job_status` | read | status / progress / phase / elapsed / ETA / queue position, plus every stem's real path, size and existence |
| `vocal.job_wait` | read | poll until a terminal state or timeout |
| `vocal.job_cancel` | write | cancel a queued or running job (terminates the subprocess, cleans the job dir) |
| `vocal.output_list` | read | history index with real per-stem byte counts; `name` + `bytes` answers "was this file already separated?" |

```powershell
npm run agent:serve    # python agent/server.py — reports the address, starts the backend if needed
npm run agent:mcp      # node agent/mcp-server.mjs — MCP stdio bridge for any MCP client
```

Full details, curl examples and what was verified on this machine: [`agent/README.md`](agent/README.md).

## 🏗️ Architecture

```
src/                  Next.js frontend (App Router) — queue, progress, preview, export
electron/             Electron main + preload (frameless shell, Save dialog, backend supervision)
backend/              FastAPI service — presets, job table, Demucs runner, history, agent contract
backend/agent_api.py  the four contract endpoints mounted on this same service
agent/                tools.py (project-owned) + errors.py / mcp-server.mjs / server.py (standard)
scripts/              wait-for-services.ps1 used by start.bat
desktop-assets/       App icons
```

Pipeline: browser/Electron → `POST /api/separate` (multipart + preset) → optional FFmpeg decode →
`python -m demucs [--two-stems vocals]` as a subprocess → WAV stems under `outputs/<job_id>/stems`,
served by `GET /api/download/{job_id}/{stem}`. Progress, phase and ETA come from `GET /api/jobs/{job_id}`;
completed jobs are appended to `backend/history.json` and listed by `GET /api/history`.
One separation runs at a time (`SEPARATION_SLOTS`), later jobs stay `queued` with a queue position.

## 📄 License

[MIT](LICENSE)
