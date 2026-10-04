"""本机服务的来源闸门 —— 挡 CSRF、跨站表单提交与 DNS rebinding。

判据来自 `personal-agent-hub/docs/AGENT_API_STANDARD.md`「本机服务」一节，四条：

1. **Host 钉死回环**：`Host` 只允许 `127.0.0.1[:端口]`、`localhost[:端口]`、`[::1][:端口]`。
   攻击者把自家域名 DNS 指到 127.0.0.1（rebinding）时，Host 仍是那个域名 —— 当场拒。
2. **Origin / Referer 白名单**：只要带了就必须是第一方（回环来源，或
   `VOCAL_SEPARATOR_ORIGINS` 里显式登记的来源）。**绝不拿 Origin 去和本次请求自己的
   Host 比** —— 那正是 rebinding 的漏法：域名解析到本机后两者会"恰好一致"。
3. **非 GET 要共享令牌**：配置了令牌（`VOCAL_SEPARATOR_TOKEN`，或
   `<DATA_DIR>/security.json` 里的 `token`）时，所有非 GET 请求必须带
   `x-vocal-token`（或 `Authorization: Bearer`），按定长时间比较。
   没配置时这一层不启用（Host + Origin 两层仍然生效），令牌发现方式见 `/api/health` 的
   `data.guard.token_file` —— 桌面壳用它决定要不要注入请求头。
4. **不给状态变更路由挂 `Access-Control-Allow-Origin: *`**：见 main.py 的 CORS 配置，
   以及本模块的 `first_party_origins()`。

`decide()` 是纯函数（进什么、判什么，全在参数里），两条方向都有单测。
"""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "ip6-localhost", "ip6-loopback"}
TOKEN_HEADER = "x-vocal-token"
GUARD_FILE_NAME = "security.json"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Origin 里出现的都是不可见字符/空白以外的东西；解析不了就当"带了一个不认识的来源"。
_ORIGIN_PORT = re.compile(r"^\d{1,5}$")


def strip_brackets(host: str) -> str:
    value = host.strip()
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    return value


def split_host(header_value: str | None) -> tuple[str, str | None]:
    """把 `Host` / URL 主机段拆成 (主机名, 端口或 None)。支持 `[::1]:8000`。"""
    value = (header_value or "").strip()
    if not value:
        return "", None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value, None
        host = value[1:end]
        rest = value[end + 1 :]
        port = rest[1:] if rest.startswith(":") else None
        return host, port or None
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        return host, (port or None)
    # `::1`（无括号）或纯 IPv6 / 纯主机名
    return value, None


def is_loopback_host(host: str) -> bool:
    cleaned = strip_brackets(host or "").strip().rstrip(".").lower()
    if not cleaned:
        return False
    if cleaned in LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 整段都是回环；::ffff:127.0.0.1 这类映射也按回环算。
    if cleaned.startswith("127."):
        return all(part.isdigit() and 0 <= int(part) <= 255 for part in cleaned.split(".")[1:]) and cleaned.count(".") == 3
    if cleaned.startswith("::ffff:127."):
        return True
    return cleaned.startswith("::1")


def origin_parts(origin: str) -> tuple[str, str | None]:
    """从 `http://127.0.0.1:3000` / `http://[::1]:8000` 里取 (主机名, 端口)。"""
    value = (origin or "").strip()
    if not value:
        return "", None
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0]
    host, port = split_host(value)
    if port is not None and not _ORIGIN_PORT.match(port):
        return host, None
    return host, port


def is_first_party_origin(origin: str | None, allowlist: set[str]) -> bool:
    """第一方 = 回环来源（任意端口，桌面壳的渲染服务端口每次都不一样）
    或 VOCAL_SEPARATOR_ORIGINS 显式登记的来源。"""
    value = (origin or "").strip().rstrip("/")
    if not value or value.lower() == "null":
        return False
    if value.lower() in {"null", "undefined"}:
        return False
    if allowlist and value.lower() in {item.lower() for item in allowlist}:
        return True
    scheme, sep, _rest = value.partition("://")
    if not sep:
        return False
    if scheme.lower() not in {"http", "https"}:
        return False
    host, port = origin_parts(value)
    if not is_loopback_host(host):
        return False
    if port is not None and not _ORIGIN_PORT.match(port):
        return False
    return True


@dataclass
class Decision:
    allowed: bool
    status: int = 200
    code: str = ""
    message: str = ""
    host: str = ""
    origin: str = ""

    @property
    def body(self) -> dict[str, Any]:
        """两种外壳都给：`detail` 是界面读的（中文），`ok/error` 是 Agent 契约读的。"""
        return {
            "ok": False,
            "detail": self.message,
            "error": {"code": self.code, "message": self.message},
            "guard": {"host": self.host, "origin": self.origin},
        }


@dataclass
class GuardState:
    """闸门此刻实际生效的配置。`describe()` 里没有任何密钥本体。"""

    token: str | None = None
    token_source: str | None = None
    allowed_origins: set[str] = field(default_factory=set)
    token_file: str = ""

    @property
    def token_required(self) -> bool:
        return bool(self.token)

    def describe(self) -> dict[str, Any]:
        return {
            "host_pin": "127.0.0.1 / localhost / [::1]",
            "origin_allowlist": sorted(self.allowed_origins) or "回环来源（任意端口）",
            "token_required": self.token_required,
            "token_source": self.token_source,
            "token_header": TOKEN_HEADER,
            "token_file": self.token_file,
            "note": (
                "令牌已启用：非 GET 必须带 " + TOKEN_HEADER
                if self.token_required
                else "未配置共享令牌：仍强制 Host 钉死与 Origin/Referer 白名单。"
                f"桌面壳要启用就在 {self.token_file} 写入 {{\"token\": \"…\"}} 或设 VOCAL_SEPARATOR_TOKEN。"
            ),
        }


def first_party_origins() -> set[str]:
    """`VOCAL_SEPARATOR_ORIGINS`（与 CORS 同一份配置，避免两处各写一遍）。"""
    raw = os.getenv("VOCAL_SEPARATOR_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


def read_token_file(data_dir: Path) -> tuple[str | None, str | None]:
    """读 `<DATA_DIR>/security.json` 的 token 字段；损坏/没有就当没配置（不猜）。"""
    target = Path(data_dir) / GUARD_FILE_NAME
    if not target.is_file():
        return None, None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    token = payload.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip(), f"file:{target.name}"
    return None, None


def guard_state_for(data_dir: Path) -> GuardState:
    """当前生效的闸门配置：env 优先，其次令牌文件，都没有就是未配置。"""
    env_token = os.getenv("VOCAL_SEPARATOR_TOKEN", "").strip()
    target = Path(data_dir) / GUARD_FILE_NAME
    if env_token:
        return GuardState(token=env_token, token_source="env:VOCAL_SEPARATOR_TOKEN", allowed_origins=first_party_origins(), token_file=str(target))
    file_token, file_source = read_token_file(data_dir)
    return GuardState(token=file_token, token_source=file_source, allowed_origins=first_party_origins(), token_file=str(target))


def presented_token(headers: Any) -> str:
    """支持 `x-vocal-token` 与 `Authorization: Bearer`（Agent 侧常见写法）。"""
    value = str(headers.get(TOKEN_HEADER) or "").strip()
    if value:
        return value
    authorization = str(headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def matches_token(expected: str | None, presented: str) -> bool:
    """定长时间比较；少一个空判断就等于给自己留一个计时侧信道。"""
    if not expected:
        return False
    return hmac.compare_digest(str(expected), str(presented or ""))


def decide(
    *,
    method: str,
    host: str | None,
    origin: str | None,
    referer: str | None,
    token: str,
    state: GuardState,
) -> Decision:
    """闸门的唯一判据。纯函数：不读 env、不碰磁盘，两条方向都能直接测。"""
    host_value = (host or "").strip()
    host_name, _port = split_host(host_value)
    if not is_loopback_host(host_name):
        return Decision(
            False,
            403,
            "host_forbidden",
            f"拒绝：Host「{host_value or '(空)'}」不是本机回环地址。本服务只服务 127.0.0.1 / localhost。",
            host=host_value,
            origin=(origin or ""),
        )

    candidate = (origin or "").strip()
    source_label = "Origin"
    if not candidate:
        referer_value = (referer or "").strip()
        if referer_value:
            candidate = referer_value
            source_label = "Referer"
    if candidate and not is_first_party_origin(candidate, state.allowed_origins):
        return Decision(
            False,
            403,
            "origin_forbidden",
            f"拒绝：{source_label}「{candidate}」不是第一方来源。只接受本机界面"
            + (f"（已登记：{'、'.join(sorted(state.allowed_origins))}）" if state.allowed_origins else "")
            + "。如果你是从别的程序调用本机接口，请不要带 Origin/Referer 头。",
            host=host_value,
            origin=candidate if source_label == "Origin" else "",
        )

    if method.upper() in SAFE_METHODS:
        return Decision(True, host=host_value, origin=candidate)

    if state.token_required and not matches_token(state.token, token):
        if token:
            return Decision(
                False,
                403,
                "token_mismatch",
                f"拒绝：本机服务启用了共享令牌（来源 {state.token_source}），请求带的 {TOKEN_HEADER} 不对。",
                host=host_value,
                origin=candidate,
            )
        return Decision(
            False,
            401,
            "token_required",
            f"拒绝：本机服务启用了共享令牌（来源 {state.token_source}），非 GET 请求必须带 {TOKEN_HEADER} 头。"
            f"令牌见 {state.token_file} 或 VOCAL_SEPARATOR_TOKEN。",
            host=host_value,
            origin=candidate,
        )

    return Decision(True, host=host_value, origin=candidate)


def make_middleware_class() -> Any:
    """把判据套成 Starlette 中间件。延迟导入，测试里只依赖 decide()。"""
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class LocalGuardMiddleware(BaseHTTPMiddleware):
        def __init__(self, app: Any, resolve_state: Callable[[], GuardState]) -> None:
            super().__init__(app)
            self._resolve_state = resolve_state

        async def dispatch(self, request: Any, call_next: Any) -> Any:  # pragma: no cover - 由 HTTP 层测试覆盖
            state = self._resolve_state()
            decision = decide(
                method=request.method,
                host=request.headers.get("host"),
                origin=request.headers.get("origin"),
                referer=request.headers.get("referer"),
                token=presented_token(request.headers),
                state=state,
            )
            if not decision.allowed:
                return JSONResponse(decision.body, status_code=decision.status)
            return await call_next(request)

    return LocalGuardMiddleware


__all__ = [
    "Decision",
    "GuardState",
    "LOOPBACK_HOSTS",
    "TOKEN_HEADER",
    "GUARD_FILE_NAME",
    "decide",
    "first_party_origins",
    "guard_state_for",
    "is_first_party_origin",
    "is_loopback_host",
    "make_middleware_class",
    "matches_token",
    "origin_parts",
    "presented_token",
    "read_token_file",
    "split_host",
]
