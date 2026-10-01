"""Agent API 契约层：把个人 Agent 服务的四个契约端点挂到本项目的常驻服务上。

契约见 `personal-agent-hub/docs/AGENT_API_STANDARD.md`：

    GET  /api/health          由 main.py 提供（同时保留 Electron 壳读的顶层旧键）
    GET  /api/agent/tools     {ok:true,data:[{name,description,input_schema,risk}]}
    GET  /api/agent/manifest  {ok:true,data:{project,version,base_url,tools}}
    POST /api/agent/tool      {tool,input} -> {ok:true,tool,ms,data} / {ok:false,error:{code,message}}

本项目**已经有常驻本地服务**（FastAPI，127.0.0.1:8000，Electron 与 start.bat 都连它），
所以契约端点加在这个服务上，**不新增端口**：`/api/health` 的旧形状是 Electron 的存活探针
（`electron/main.cjs` 读 `status=="ok"`，`scripts/wait-for-services.ps1` 同样读它），
因此健康检查返回"旧键 + 标准信封"的超集，两边都不破。

工具声明与 handler 在 `agent/tools.py`（本项目唯一需要写的文件）。校验逻辑与
标准模板 `templates/agent-api/python/server.py` 保持一致（required + additionalProperties，
bad_input → 400，未知工具 → 400 且带 available）。

handler 一律放在线程池里跑：`vocal.job_wait` 会阻塞轮询，不能占住事件循环。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

BASE_DIR = Path(__file__).resolve().parent
ROOT = BASE_DIR.parent
AGENT_DIR = ROOT / "agent"

for path_entry in (str(ROOT), str(AGENT_DIR)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from errors import AgentError  # noqa: E402  与 agent/tools.py 共用同一个类对象


def load_tools() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """与标准模板 server.py 的 load_tools() 同法加载 agent/tools.py。"""
    path = AGENT_DIR / "tools.py"
    if not path.exists():
        raise RuntimeError(f"missing {path}: 项目必须实现 agent/tools.py")
    spec = importlib.util.spec_from_file_location("agent_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return getattr(module, "PROJECT", {"name": "vocal-separator", "version": "0.0.0"}), list(getattr(module, "TOOLS"))


PROJECT, TOOLS = load_tools()
_BY_NAME: dict[str, dict[str, Any]] = {tool["name"]: tool for tool in TOOLS}
_START = time.time()


def descriptor(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": tool.get("input_schema", {}),
        "risk": tool.get("risk", "read"),
    }


def validate(schema: dict[str, Any] | None, data: Any) -> None:
    """只校验 required 与未知键；类型收窄交给 handler 自己。"""
    if not schema or schema.get("type") != "object":
        return
    obj = data if isinstance(data, dict) else {}
    missing = [key for key in schema.get("required", []) if obj.get(key) in (None, "")]
    if missing:
        raise AgentError("bad_input", "缺少必填参数：" + ", ".join(missing))
    if schema.get("additionalProperties") is False:
        properties = schema.get("properties", {})
        unknown = [key for key in obj if key not in properties]
        if unknown:
            raise AgentError("bad_input", f"未知参数：{', '.join(unknown)}；可用：{', '.join(properties) or '无'}")


def write_endpoint_file(port: int, host: str = "127.0.0.1") -> Path:
    """把实际地址写进 agent/.endpoint，MCP 桥优先读它（端口占用时不至于找不到服务）。"""
    endpoint = AGENT_DIR / ".endpoint"
    try:
        endpoint.write_text(f"http://{host}:{port}\n", encoding="utf-8")
    except OSError:
        pass
    return endpoint


router = APIRouter(tags=["agent-api"])


@router.get("/api/agent/tools")
async def agent_tools() -> dict[str, Any]:
    return {"ok": True, "data": [descriptor(tool) for tool in TOOLS]}


@router.get("/api/agent/manifest")
async def agent_manifest(request: Request) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "project": PROJECT["name"],
            "version": PROJECT.get("version", "0.0.0"),
            "description": PROJECT.get("summary", ""),
            "base_url": f"http://{request.url.hostname}:{request.url.port or 8000}",
            "shared_port_with_app_backend": True,
            "tools": [descriptor(tool) for tool in TOOLS],
        },
    }


def _envelope_error(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message, **extra}}
    return JSONResponse(status_code=status_code, content=body)


@router.post("/api/agent/tool")
async def agent_tool(request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > 8 * 1024 * 1024:
        return _envelope_error(400, "too_large", "请求体超过 8MB")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _envelope_error(400, "bad_json", "请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _envelope_error(400, "bad_input", "请求体必须是 JSON 对象")

    name = body.get("tool")
    tool = _BY_NAME.get(name)
    if tool is None:
        return _envelope_error(
            400,
            "unknown_tool",
            f"未注册的工具：{name}",
            available=list(_BY_NAME),
        )

    handler: Callable[..., Any] = tool["handler"]
    started = time.time()
    try:
        validate(tool.get("input_schema"), body.get("input"))
        data = await run_in_threadpool(
            handler,
            body.get("input") or {},
            {"project": PROJECT, "host": request.url.hostname, "port": request.url.port or 8000},
        )
        payload = {"ok": True, "tool": name, "ms": int((time.time() - started) * 1000), "data": data}
        return JSONResponse(status_code=200, content=json.loads(json.dumps(payload, ensure_ascii=False, default=str)))
    except AgentError as exc:
        status_code = 400 if exc.code == "bad_input" else 500
        response = _envelope_error(status_code, exc.code, str(exc))
        response.headers["x-agent-tool"] = str(name)
        return response
    except Exception as exc:  # noqa: BLE001
        return _envelope_error(500, "handler_failed", f"{type(exc).__name__}: {exc}")


__all__ = ["PROJECT", "TOOLS", "router", "validate", "write_endpoint_file", "agent_api_uptime_ms"]


def agent_api_uptime_ms() -> int:
    return int((time.time() - _START) * 1000)
