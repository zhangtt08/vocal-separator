# 🎙️ 声析 (ShengXi) · 本地 AI 人声伴奏分离神器

<p align="center">
  <img src="./desktop-assets/icon.png" width="96" height="96" alt="声析图标" />
</p>

<p align="center">
  <b>基于 Meta Demucs 深度学习模型的高清无损音频分轨桌面应用</b><br>
  一键离线提取【纯人声 / 去人声伴奏 / 鼓组 / 贝斯 / 其它乐器】多音轨 · 音乐翻唱、K歌伴奏制作与混音采样神器<br>
  100% 纯本地运行 · 绝无云端上传 · 不限音频时长与次数 · 永久免费开源 · 免配 Python 环境
</p>

<p align="center">
  <a href="./README.en.md">English Documentation</a> | <b>简体中文</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License" />
  <img src="https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6?logo=windows&logoColor=white" alt="Platform" />
  <img src="https://img.shields.io/badge/AI%20模型-Meta%20Demucs%20v4-purple" alt="Demucs" />
  <img src="https://img.shields.io/badge/GPU%20加速-CUDA%20PyTorch-76B900?logo=nvidia&logoColor=white" alt="CUDA" />
  <img src="https://img.shields.io/badge/100%25-纯本地离线-brightgreen" alt="Offline" />
  <img src="https://img.shields.io/badge/Agent-MCP%20Ready-orange" alt="MCP" />
</p>

<p align="center">
  <a href="https://github.com/zhangtt08/vocal-separator/releases/latest"><b>🚀 [点击立即下载 Windows 便携免安装版]</b></a>
</p>

<p align="center">
  <img src="docs/screenshot.png" alt="声析主界面" width="85%" />
</p>

---

## ⚡ 为什么选择「声析」？

许多音乐人、翻唱爱好者、视频剪辑师在提取人声和伴奏时，常常遇到以下痛点：
1. **在线工具收费昂贵、限制极多**：Lalal.ai、Moises 等平台按分钟扣费，免费版只能试听 30 秒，排队经常要等待 10~20 分钟；
2. **商业工程与未发布曲目面临泄密**：将私密录音、未发行的 Demo 上传到第三方网站服务器，存在极高的泄露与版权风险；
3. **音质受损发闷、伴奏残存人声**：劣质网页算法分离出来的人声单薄刺耳，伴奏低音丢失，高频毛刺严重；
4. **命令行 Demucs 部署极其繁杂**：普通创作者面对 Python、CUDA、PyTorch 环境配置望而却步，出现环境冲突毫无头绪。

**「声析」为你彻底打破这些壁垒！**  
无需配置复杂环境，纯本地 GPU 硬件加速推理，拖入任意音频或视频文件，即刻获得母带级别的无损分离音频！

---

## 🎯 核心使用场景

- 🎤 **K 歌与翻唱伴奏制作**：一键去除原唱人声，获得纯净、无损的高清消音伴奏。
- 🎸 **乐器扒带与练习**：单独剥离出鼓点（Drums）或贝斯（Bass）音轨，听音辨谱效率提升十倍。
- 🎬 **短视频混音剪辑**：提取人物纯对白，消除原片背景杂音或更换 BGM。
- 🎧 **DJ 采样与二次混音（Remix）**：轻松提取清脆的人声干音（Acapella），无缝导入 DAW 宿主软件中编曲。

---

## ✨ 核心特性

- 🎛️ **两轨 / 四轨一键自由选择**：
  - **两轨模式**：人声（Vocals）+ 纯伴奏（No Vocals）；
  - **四轨模式**：人声 + 鼓组（Drums）+ 贝斯（Bass）+ 其它乐器（Other）。
- ⚡ **GPU 硬件极速推理**：深度优化 PyTorch CUDA 显卡加速；无独立显卡时自动平滑回退至多核 CPU 运行。
- 🔒 **100% 纯本地离线，版权安全**：全程在本地电脑运行，断网亦可使用。音频绝不出机器，彻底告别泄密风险。
- 🎧 **内置多轨独立试听播放器**：界面内置专业波形试听组件，各分声音轨支持单独开启、静音与实时无缝切换试听。
- 📁 **无损音质与多格式导出**：支持导出 24-bit 无损 WAV（母带级质量）、FLAC 以及 320kbps 高音质 MP3。
- 📋 **多任务批量队列**：支持一次拖入多首歌曲批量排队处理，实时展示百分比进度与剩余时间预估。
- 🤖 **AI Agent 与 MCP 接口**：原生开放 MCP 协议与 FastAPI 接口，支持由 Cursor、Claude Code 等智能体自动调用完成音频剪辑编排。

---

## 📥 快速上手

### 方式一：直接运行桌面版（开箱即用，强烈推荐）
1. 前往 **[Releases 页面](https://github.com/zhangtt08/vocal-separator/releases/latest)** 下载 `ShengXi-Windows-x64.exe` 便携版；
2. 双击打开，软件将自动启动本地服务并打开现代化操作界面；
3. 将想要分离的音乐或视频拖入窗口；
4. 选择「人声 + 伴奏（两轨）」或「四条音轨」，点击「开始分离」；
5. 处理完成后，可直接在界面点击播放试听，或点击「保存」导出到本地。

### 方式二：开发者源码运行
需要 Node.js 20+ 与 Python 3.10+：
```bash
git clone https://github.com/zhangtt08/vocal-separator.git
cd vocal-separator
npm install
python -m pip install -r backend/requirements.txt

# 启动前后端
python backend/main.py   # 后端 API (端口 8000)
npm run dev              # 前端界面 (端口 3000)
```

---

## 🤖 AI Agent & MCP 支持

「声析」自带面向 AI 智能体的标准接口，监听 `127.0.0.1:8000`：
- `vocal.separate`：传入音频路径，指定两轨或四轨模式，返回分离后各轨道的本地绝对路径。
- `vocal.probe`：查询本机 CUDA、Demucs、Torch 运行时环境状态与可用显存。
- `vocal.queue_status`：查询当前后台批量分离任务队列进度。

---

## ⭐ 支持与致谢

如果「声析」为您的音乐创作、K歌翻唱或视频制作带来了便利，请在 GitHub 上为本项目点亮一颗 **Star ⭐** 吧！这是对开源开发者最好的支持与鼓励。

## 📄 开源许可证
本项目基于 [MIT License](LICENSE) 协议开源。深度学习模型基于 Meta 的 [Demucs](https://github.com/adefossez/demucs)。
