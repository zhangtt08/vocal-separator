# 声析 (vocal-separator)

[English](README.md)

本地 AI 音轨分离桌面软件：上传一首歌，在本机用 [Demucs](https://github.com/adefossez/demucs) 拆分为 **人声 / 鼓组 / 贝斯 / 其他** 四条 WAV 音轨。音频全程不出本机，无上传云盘环节。

![界面](docs/screenshot.png)

## 功能特性

- **四轨分离**：Demucs（`htdemucs` 模型）拆出人声 / 鼓组 / 贝斯 / 其他，每轨输出一个 WAV 文件
- **完全本地**：无账号、无上传配额、无排队，音频不出本机
- **异步任务 API**：提交曲目后可轮询进度、中途取消、按轨下载
- **常见格式**：MP3 / WAV / FLAC / OGG / M4A / AAC，单文件最大 500 MB，FFmpeg 自动转 WAV
- **GPU 加速**：锁定的 PyTorch CUDA 12.4 轮子；CPU 也能跑，但更慢
- **自动清理**：上传和输出文件 1 小时后自动删除
- **两种形态**：Windows 便携桌面应用（`npm run desktop:pack`），或纯浏览器访问 localhost 使用

## 技术栈

- **前端**：Next.js 16 + React 19 + Tailwind CSS，Electron 桌面壳
- **后端**：Python FastAPI + Demucs 4（GPU: torch cu124）

## 运行

前置要求：Node.js ≥ 20.9（Next.js 16）、Python 3.10+（首次运行会一次性下载 `htdemucs` 模型权重）。

首次使用先安装依赖：

```powershell
npm install
python -m pip install -r backend/requirements.txt
```

随后双击 `start.bat`，或分别启动前后端：

```powershell
python backend/main.py
npm run dev
```

浏览器访问 `http://localhost:3000`。后端默认运行在 `http://localhost:8000`。

## 配置

- 前端可通过 `NEXT_PUBLIC_API_BASE_URL` 指定后端地址。
- 后端可通过 `VOCAL_SEPARATOR_ORIGINS` 设置允许访问的前端来源，多个地址用英文逗号分隔。
- 单个文件最大 500 MB，上传和输出文件会在 1 小时后自动清理。

## 检查

```powershell
npm run lint
npm run build
python -m py_compile backend/main.py
python -m unittest discover -s backend -v
```

## 桌面打包

```powershell
npm run desktop:pack   # 产出 release/ 下的 Windows 便携版 exe
```

## 项目结构

```
src/                  前端页面与组件（Next.js App Router）
electron/             Electron 主进程
backend/              FastAPI 服务（main.py）与单测（test_main.py）
desktop-assets/       应用图标
```

## 许可证

[MIT](LICENSE)
