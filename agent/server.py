#!/usr/bin/env python
"""启动声析的 Agent API（不新增端口）。

个人 Agent 服务的标准模板给每个项目一个 `agent/server.py`。本项目**本来就有常驻本地
服务**——`backend/main.py` 的 FastAPI，只听 127.0.0.1:8000，Electron 壳、浏览器界面与
start.bat 连的都是它。所以契约端点（/api/health、/api/agent/tools、/api/agent/manifest、
POST /api/agent/tool）就加在那个服务上（backend/agent_api.py），这里只做一个 launcher：

1. 8000 上已经有健康的后端  -> 报告地址、写 agent/.endpoint、退出 0；
2. 没有                     -> 用当前解释器起 backend/main.py，前台等它（Ctrl+C 停）。

不监听第二个端口，不复制一份任务表：Agent 与界面看到的是同一批任务与同一批输出文件。
控制台可能是 GBK，所以这里的启动行只用 ASCII。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND_ENTRY = ROOT / "backend" / "main.py"
PORT = int(os.getenv("VOCAL_SEPARATOR_PORT", os.getenv("AGENT_PORT", "8000")))
HOST = "127.0.0.1"


def health(timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=timeout) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def agent_ready(payload: dict | None) -> bool:
    return bool(payload and payload.get("ok") and (payload.get("data") or {}).get("agent_api"))


def main() -> int:
    if not BACKEND_ENTRY.is_file():
        print(f"[agent] missing backend entry: {BACKEND_ENTRY}", flush=True)
        return 1

    payload = health()
    if payload is not None and not agent_ready(payload):
        print(
            f"[agent] port {PORT} is answering /api/health without the agent contract - "
            "it is not this project's backend, or it is an older build. Restart it.",
            flush=True,
        )
        return 1

    started: subprocess.Popen | None = None
    if not agent_ready(payload):
        print(f"[agent] starting backend/main.py on http://{HOST}:{PORT}", flush=True)
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
        started = subprocess.Popen(  # noqa: S603
            [sys.executable, str(BACKEND_ENTRY)],
            cwd=str(ROOT),
            env=environment,
        )
        for _ in range(120):
            time.sleep(0.5)
            if agent_ready(health()):
                break
            if started.poll() is not None:
                print(f"[agent] backend exited with code {started.returncode}", flush=True)
                return started.returncode or 1
        else:
            print("[agent] backend did not become healthy within 60s", flush=True)
            started.terminate()
            return 1

    (HERE / ".endpoint").write_text(f"http://{HOST}:{PORT}\n", encoding="utf-8")
    tools_url = f"http://{HOST}:{PORT}/api/agent/tools"
    try:
        with urllib.request.urlopen(tools_url, timeout=5) as response:
            tools = json.loads(response.read().decode("utf-8")).get("data", [])
    except (urllib.error.URLError, OSError, ValueError):
        tools = []
    print(f"[agent] vocal-separator -> http://{HOST}:{PORT} ({len(tools)} tools)", flush=True)
    for tool in tools:
        print(f"[agent]   {tool.get('name')} [{tool.get('risk')}]", flush=True)

    if started is None:
        print("[agent] backend was already running; nothing to supervise.", flush=True)
        return 0
    # 是自己起来的就留在前台，Ctrl+C 一并停掉，不留下没人管的后台进程。
    try:
        return started.wait()
    except KeyboardInterrupt:
        started.terminate()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
