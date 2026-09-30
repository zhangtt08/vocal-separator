# vocal-separator (声析)

Local-first AI stem separation: split any song into **vocals / drums / bass / other** on your own machine — no cloud upload, no queue.
本地 AI 音轨分离：上传一首歌，用 [Demucs](https://github.com/adefossez/demucs) 在本机拆出人声 / 鼓组 / 贝斯 / 其他四条音轨，音频全程不出电脑。

Separating vocals from accompaniment usually means uploading your audio to an online service and waiting in line. **vocal-separator** runs the Demucs `htdemucs` model through a local FastAPI service instead: the whole job — upload, separation, download — stays on your computer. It ships as a Windows desktop app (Electron) and also works as a plain web UI at `localhost:3000`.

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

- **Four-stem separation** — vocals, drums, bass and other, each delivered as a WAV file, powered by Demucs (`htdemucs`)
- **Fully local** — audio never leaves your machine; no account, no upload quota, no waiting in a queue
- **Job-based async API** — submit a track, poll progress, cancel mid-run, download stems individually
- **Common formats in** — MP3 / WAV / FLAC / OGG / M4A / AAC up to 500 MB; FFmpeg converts to WAV automatically
- **GPU acceleration** — pinned PyTorch CUDA 12.4 wheels; also runs on CPU (slower)
- **Self-cleaning** — uploads and outputs are wiped automatically after 1 hour
- **Two ways to run** — Windows portable desktop app (`npm run desktop:pack`) or browser-only mode

## 🚀 Quick Start

Prerequisites: **Node.js ≥ 20.9** (Next.js 16) and **Python 3.10+**. An NVIDIA GPU is strongly recommended — CPU works but separation is slow. The first run downloads the `htdemucs` model weights once.

```powershell
git clone https://github.com/zhangtt08/vocal-separator.git
cd vocal-separator
npm install
python -m pip install -r backend/requirements.txt
```

Then double-click `start.bat` (one-click launcher for Windows), or start both services manually:

```powershell
python backend/main.py   # FastAPI backend  -> http://localhost:8000
npm run dev              # Next.js frontend -> http://localhost:3000
```

Open <http://localhost:3000>, drop in a song, wait for the four stems.

### Configuration

- `NEXT_PUBLIC_API_BASE_URL` — point the frontend at a different backend address
- `VOCAL_SEPARATOR_ORIGINS` — comma-separated list of frontend origins allowed by the backend

## 🏗️ Architecture

```
src/                  Next.js frontend (App Router) — upload, progress, stem download
electron/             Electron main process (Windows portable shell)
backend/              FastAPI service — job queue, Demucs runner, stem downloads
desktop-assets/       App icons
```

Pipeline: browser/Electron → `POST /api/separate` (multipart upload) → FastAPI converts the input to WAV with FFmpeg → runs `python -m demucs` as a subprocess → four WAV stems, exposed via `GET /api/download/{job_id}/{stem}`. Health check at `GET /api/health`.

## 📄 License

[MIT](LICENSE)
