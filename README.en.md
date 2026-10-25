# 🎙️ ShengXi (vocal-separator) · Local AI Audio & Stem Separator

<p align="center">
  <img src="./desktop-assets/icon.png" width="96" height="96" alt="ShengXi Icon" />
</p>

<p align="center">
  <b>High-Fidelity AI Stem Separation Powered by Meta Demucs</b><br>
  Extract clean vocals, instrumentals, drums, and bass in one click · For musicians, DJs, karaoke, and audio creators<br>
  100% Local Processing · No Cloud Uploads · Unlimited Songs · Free & Open-Source
</p>

<p align="center">
  <b>English</b> | <a href="./README.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License" />
  <img src="https://img.shields.io/badge/Platform-Windows%2010%2B-0078D6?logo=windows&logoColor=white" alt="Platform" />
  <img src="https://img.shields.io/badge/AI%20Model-Meta%20Demucs%20v4-purple" alt="Demucs" />
  <img src="https://img.shields.io/badge/GPU%20Accel-CUDA%20PyTorch-76B900?logo=nvidia&logoColor=white" alt="CUDA" />
  <img src="https://img.shields.io/badge/100%25-Local%20Offline-brightgreen" alt="Offline" />
  <img src="https://img.shields.io/badge/Agent-MCP%20Ready-orange" alt="MCP" />
</p>

---

## 🎯 Highlights & Features

- **2-Stem & 4-Stem Modes**:
  - *2-Stem*: Clean Vocals + Instrumental (Karaoke accompaniment / Acapella).
  - *4-Stem*: Vocals + Drums + Bass + Other Instruments (Remixing, sampling, and music production).
- **GPU Acceleration**: Utilizes NVIDIA CUDA PyTorch acceleration with graceful CPU fallback.
- **100% Privacy & Offline**: All computations run locally on your machine. Unreleased songs, voiceovers, and stems never touch external servers.
- **Built-in Waveform Preview**: Seamlessly audition and toggle individual stems directly within the interactive desktop player.
- **Lossless Export**: Export stems to 24-bit lossless WAV, FLAC, or high-quality 320kbps MP3.
- **Batch Processing Queue**: Drag in multiple songs, reorder jobs, and monitor real-time progress.
- **AI Agent & MCP Ready**: Built-in FastAPI backend and Model Context Protocol (MCP) server for automated audio workflows.

---

## 🖥️ Preview

<p align="center">
  <img src="docs/screenshot.png" alt="ShengXi UI" width="85%" />
</p>

---

## 🚀 Quick Start

### Option 1: Run Desktop Version (Recommended)
1. Double click **`start.bat`** (or the desktop executable shortcut).
2. Drag and drop your audio or video file into the window.
3. Choose **2-Stem** or **4-Stem** mode.
4. Click **Start Separation**!

### Option 2: Run from Source
```bash
git clone https://github.com/zhangtt08/vocal-separator.git
cd vocal-separator
npm install
python -m pip install -r backend/requirements.txt

# Start backend & frontend
python backend/main.py
npm run dev
```

---

## ⭐ Star & License

If you find ShengXi helpful, please consider giving it a **Star ⭐** on GitHub!
Licensed under the [MIT License](./LICENSE). Powered by Meta's [Demucs](https://github.com/adefossez/demucs).
