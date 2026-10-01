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
- **Cancellable** — cancel a queued or running job; the Demucs subprocess is terminated and its job directory removed
- **Machine health you can read** — the UI strip shows the real python / torch / CUDA device / Demucs / ffmpeg versions and whether the `htdemucs` weights are already cached
- **Actionable failures** — missing Demucs, missing ffmpeg, CUDA out of memory and expired results each come with the next step to take, not just an error string
- **Recent results** — a persistent index (`backend/history.json`) lists past separations with real file sizes, so re-dropping the same file asks "already separated, run again?" instead of silently burning 10 minutes
- **Export under your control** — preview in place; in the desktop app "保存" opens a real Save dialog (folder and filename yours), in the browser it downloads; the result directory is shown and can be opened
- **Common formats in** — MP3 / WAV / FLAC / OGG / M4A / AAC, plus MP4 / MOV / MKV / WebM / AVI (FFmpeg extracts the audio first), up to 500 MB
- **Self-cleaning** — uploads and outputs are wiped automatically after 1 hour; files you saved elsewhere are untouched
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
| `VOCAL_SEPARATOR_ORIGINS` | comma-separated frontend origins allowed by the backend (CORS) |
| `VOCAL_SEPARATOR_DATA_DIR` | where uploads / outputs / history live (desktop app uses its userData dir) |
| `VOCAL_SEPARATOR_PORT` | backend port, default `8000` |
| `VOCAL_SEPARATOR_PYTHON` | which interpreter the desktop shell should launch the backend with |
| `VOCAL_SEPARATOR_FFMPEG` | explicit ffmpeg path |

### Checks

```powershell
npm run lint
npm run build
python -m unittest discover -s backend -v   # or: npm run test:backend
```

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
