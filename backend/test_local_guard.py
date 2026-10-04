"""本机服务闸门（`backend/local_guard.py`）的测试。

两层都测，两条方向都测：

* `GuardDecisionTests` —— `decide()` 是纯函数，判据表逐条给正反例；
* `GuardHttpTests` —— 真实 HTTP 层：跨站 `multipart/form-data` 表单 POST、伪造 Host、
  带/不带令牌的第一方 POST、`/api/session-token` 与 `/api/health` 的令牌发现方式。

这里的正例（第一方 + 令牌被接受）就是"界面与桌面壳没被闸门挡在门外"的证明；
它挂了要先想到闸门，不要想到把断言改松。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import local_guard
import main
from test_main import LOOPBACK_BASE, ServiceSandbox

EVIL_ORIGIN = "http://evil.test"
TOKEN = "0123456789abcdef0123456789abcdef"


def state(token: str | None = None) -> local_guard.GuardState:
    return local_guard.GuardState(
        token=token,
        token_source="test" if token else None,
        allowed_origins={"http://localhost:3000", "http://127.0.0.1:3000"},
        token_file="/tmp/security.json",
    )


def decide(
    *,
    method: str = "POST",
    host: str = "127.0.0.1:8000",
    origin: str | None = None,
    referer: str | None = None,
    token: str = "",
    configured: str | None = None,
) -> local_guard.Decision:
    return local_guard.decide(
        method=method,
        host=host,
        origin=origin,
        referer=referer,
        token=token,
        state=state(configured),
    )


class GuardDecisionTests(unittest.TestCase):
    def test_only_loopback_hosts_pass_the_host_pin(self):
        for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "127.0.0.1", "localhost"):
            self.assertTrue(decide(host=host).allowed, host)
        for host in (
            "evil.test:8000",
            "0.0.0.0:8000",
            "10.0.0.7:8000",
            "169.254.169.254",
            "127.0.0.1.evil.com",
            "[::ffff:10.0.0.1]",
            "",
        ):
            result = decide(host=host)
            self.assertFalse(result.allowed, host)
            self.assertEqual(result.code, "host_forbidden", host)

    def test_cross_origin_is_refused_without_comparing_to_own_host(self):
        # DNS rebinding 的形状：Host 与 Origin 都是攻击者域名，两者"恰好一致"。
        # 拿 Origin 去比本次请求的 Host 会放它过去，所以判据只能是"Origin 是不是第一方"。
        result = decide(host="evil.test:8000", origin="http://evil.test:8000")
        self.assertFalse(result.allowed)
        self.assertEqual(result.code, "host_forbidden")
        # 同一个 Origin 打到回环 Host 上（rebinding 成功、Host 被浏览器按 IP 改写）也照样拒。
        self.assertEqual(decide(origin=EVIL_ORIGIN).code, "origin_forbidden")

    def test_first_party_origins_and_loopback_pages_are_allowed(self):
        for origin in ("http://localhost:3000", "http://127.0.0.1:3000", "http://127.0.0.1:54321", "http://[::1]:8000"):
            self.assertTrue(decide(origin=origin).allowed, origin)
        # 没有 Origin = 非浏览器调用（curl / 桌面壳探针 / Agent 桥），不是跨站表单。
        self.assertTrue(decide().allowed)
        # Referer 只在没有 Origin 时参与判定。
        self.assertEqual(decide(referer="http://evil.com/page").code, "origin_forbidden")
        self.assertTrue(decide(referer="http://localhost:3000/").allowed)
        # Origin: null（沙箱 iframe / data: URL）不是第一方。
        self.assertEqual(decide(origin="null").code, "origin_forbidden")

    def test_non_get_needs_the_shared_token_when_configured(self):
        self.assertEqual(decide(configured=TOKEN).code, "token_required")
        self.assertEqual(decide(configured=TOKEN).status, 401)
        self.assertTrue(decide(configured=TOKEN, token=TOKEN).allowed)
        wrong = decide(configured=TOKEN, token="deadbeef")
        self.assertEqual(wrong.code, "token_mismatch")
        self.assertEqual(wrong.status, 403)
        # 没配置令牌时，非 GET 只受 Host / Origin 两层约束（标准里写的"when configured"）。
        self.assertTrue(decide().allowed)
        # 读操作永远不需要令牌。
        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertTrue(decide(method=method, configured=TOKEN).allowed, method)

    def test_token_comparison_is_timing_safe_and_header_flexible(self):
        source = Path(local_guard.__file__).read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest", source)
        self.assertTrue(local_guard.matches_token(TOKEN, TOKEN))
        self.assertFalse(local_guard.matches_token(TOKEN, ""))
        self.assertFalse(local_guard.matches_token(None, TOKEN))
        self.assertEqual(local_guard.presented_token({"authorization": f"Bearer {TOKEN}"}), TOKEN)
        self.assertEqual(local_guard.presented_token({local_guard.TOKEN_HEADER: TOKEN}), TOKEN)

    def test_state_resolution_prefers_env_then_file_then_none(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            with patch.dict(os.environ, {"VOCAL_SEPARATOR_TOKEN": TOKEN}, clear=False):
                resolved = local_guard.guard_state_for(directory)
                self.assertEqual(resolved.token, TOKEN)
                self.assertEqual(resolved.token_source, "env:VOCAL_SEPARATOR_TOKEN")
                # 环境变量优先于文件：两个都在时以运维显式设的那个为准。
                (directory / local_guard.GUARD_FILE_NAME).write_text(
                    json.dumps({"token": "from-file"}), encoding="utf-8"
                )
                self.assertEqual(local_guard.guard_state_for(directory).token, TOKEN)

            os.environ.pop("VOCAL_SEPARATOR_TOKEN", None)
            from_file = local_guard.guard_state_for(directory)
            self.assertEqual(from_file.token, "from-file")
            self.assertEqual(from_file.token_source, f"file:{local_guard.GUARD_FILE_NAME}")

            (directory / local_guard.GUARD_FILE_NAME).write_text("{ 损坏的 json", encoding="utf-8")
            self.assertFalse(local_guard.guard_state_for(directory).token_required)
            os.environ.pop("VOCAL_SEPARATOR_TOKEN", None)
            self.assertFalse(local_guard.guard_state_for(directory / "没有这个目录").token_required)

    def test_describe_never_carries_the_token_itself(self):
        payload = state(TOKEN).describe()
        self.assertIs(payload["token_required"], True)
        self.assertNotIn(TOKEN, json.dumps(payload, ensure_ascii=False))


class GuardHttpTests(ServiceSandbox, unittest.TestCase):
    """HTTP 层的四条验收：跨站 403、伪造 Host 403、第一方 + 令牌 202、发现方式说实话。"""

    def client(self, base_url: str = LOOPBACK_BASE) -> TestClient:
        return TestClient(main.app, base_url=base_url)

    def test_cross_origin_multipart_post_is_refused_with_json_403(self):
        with self.client() as client:
            with patch.dict(os.environ, {"VOCAL_SEPARATOR_TOKEN": TOKEN}, clear=False):
                response = client.post(
                    "/api/separate",
                    files={"file": ("song.wav", b"RIFF" + b"\0" * 64, "audio/wav")},
                    data={"preset": "four_stems"},
                    headers={"origin": EVIL_ORIGIN},
                )
            self.assertEqual(response.status_code, 403)
            body = response.json()
            self.assertIs(body["ok"], False)
            self.assertEqual(body["error"]["code"], "origin_forbidden")
            self.assertIn("第一方", body["detail"])
            # 闸门在业务之前：什么都没排队、什么都没落盘。
            self.assertEqual(main.jobs, {})
            self.assertEqual(list(self.uploads.iterdir()), [])
            cancel = client.post(
                "/api/jobs/abc123def456/cancel",
                headers={"origin": "https://evil.test"},
            )
            self.assertEqual(cancel.status_code, 403)
            self.assertEqual(cancel.json()["error"]["code"], "origin_forbidden")

    def test_spoofed_host_is_refused_even_with_a_matching_origin(self):
        # 浏览器把 rebinding 后的域名原样写进 Host，所以 Host 与 Origin 互相"印证"也不能放行。
        with self.client("http://attacker.example:8000") as client:
            for method, path in (("get", "/api/health"), ("post", "/api/jobs/abc123def456/cancel")):
                response = getattr(client, method)(path, headers={"origin": "http://attacker.example:8000"})
                self.assertEqual(response.status_code, 403, path)
                self.assertEqual(response.json()["error"]["code"], "host_forbidden", path)

    def test_legit_loopback_post_with_token_is_accepted(self):
        with self.client() as client:
            with patch.dict(os.environ, {"VOCAL_SEPARATOR_TOKEN": TOKEN}, clear=False):
                missing = client.post("/api/jobs/abc123def456/cancel")
                self.assertEqual(missing.status_code, 401)
                self.assertEqual(missing.json()["error"]["code"], "token_required")

                wrong = client.post(
                    "/api/jobs/abc123def456/cancel",
                    headers={local_guard.TOKEN_HEADER: "not-the-token"},
                )
                self.assertEqual(wrong.status_code, 403)
                self.assertEqual(wrong.json()["error"]["code"], "token_mismatch")

                # 界面（第一方 Origin + 令牌）与桌面壳（代理补 Host + 令牌，无 Origin）都走得通。
                from_frontend = client.post(
                    "/api/separate",
                    files={"file": ("song.wav", b"RIFF" + b"\0" * 64, "audio/wav")},
                    headers={"origin": "http://127.0.0.1:3000", local_guard.TOKEN_HEADER: TOKEN},
                )
                self.assertEqual(from_frontend.status_code, 202, from_frontend.text)
                self.assertEqual(from_frontend.json()["status"], "queued")

                from_shell = client.post(
                    "/api/separate",
                    files={"file": ("shell.wav", b"RIFF" + b"\0" * 64, "audio/wav")},
                    headers={local_guard.TOKEN_HEADER: TOKEN},
                )
                self.assertEqual(from_shell.status_code, 202, from_shell.text)
                # Agent 契约端点同样是 POST，也在这道闸门后面。
                agent = client.post(
                    "/api/agent/tool",
                    json={"tool": "vocal.mode_list", "input": {}},
                    headers={local_guard.TOKEN_HEADER: TOKEN},
                )
                self.assertEqual(agent.status_code, 200)
                self.assertIs(agent.json()["ok"], True)
                agent_without_token = client.post("/api/agent/tool", json={"tool": "vocal.mode_list", "input": {}})
                self.assertEqual(agent_without_token.status_code, 401)

    def test_state_changing_routes_never_cors_wildcard(self):
        with self.client() as client:
            refused = client.post("/api/separate", files={"file": ("s.wav", b"RIFF", "audio/wav")}, headers={"origin": EVIL_ORIGIN})
            self.assertNotEqual(refused.headers.get("access-control-allow-origin"), "*")
            allowed = client.get("/api/health", headers={"origin": "http://localhost:3000"})
            self.assertEqual(allowed.headers.get("access-control-allow-origin"), "http://localhost:3000")
            self.assertNotEqual(allowed.headers.get("access-control-allow-origin"), "*")

    def test_session_token_endpoint_answers_first_party_only_and_health_describes_guard(self):
        with self.client() as client:
            with patch.dict(os.environ, {"VOCAL_SEPARATOR_TOKEN": TOKEN}, clear=False):
                anonymous = client.get("/api/session-token")
                self.assertEqual(anonymous.status_code, 200)
                self.assertIs(anonymous.json()["required"], True)
                self.assertEqual(anonymous.json()["token"], TOKEN)
                self.assertEqual(anonymous.json()["header"], local_guard.TOKEN_HEADER)
                # 来源说真话：这份令牌是从环境变量来的，不是文件来的。
                self.assertEqual(anonymous.json()["source"], "env:VOCAL_SEPARATOR_TOKEN")

                evil = client.get("/api/session-token", headers={"origin": EVIL_ORIGIN})
                self.assertEqual(evil.status_code, 403)

                health = client.get("/api/health").json()["data"]["guard"]
                self.assertIs(health["token_required"], True)
                self.assertEqual(health["token_header"], local_guard.TOKEN_HEADER)
                self.assertTrue(str(health["token_file"]).endswith("security.json"))
                self.assertNotIn(TOKEN, json.dumps(health, ensure_ascii=False))

            unconfigured = client.get("/api/session-token").json()
            self.assertIs(unconfigured["required"], False)
            self.assertIsNone(unconfigured["token"])

    def test_token_file_is_the_documented_discovery_path_for_the_desktop_shell(self):
        # 桌面壳（electron/api-proxy.cjs）读的就是这个文件：写它 -> 服务开始要求令牌。
        target = self.root / local_guard.GUARD_FILE_NAME
        target.write_text(json.dumps({"token": TOKEN}), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("VOCAL_SEPARATOR_TOKEN", None)
            with self.client() as client:
                self.assertEqual(client.post("/api/jobs/abc123def456/cancel").status_code, 401)
                accepted = client.post(
                    "/api/separate",
                    files={"file": ("from-file.wav", b"RIFF" + b"\0" * 64, "audio/wav")},
                    headers={local_guard.TOKEN_HEADER: TOKEN},
                )
                self.assertEqual(accepted.status_code, 202, accepted.text)
                health = client.get("/api/health").json()["data"]["guard"]
                self.assertEqual(health["token_source"], f"file:{local_guard.GUARD_FILE_NAME}")
                self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["token"], TOKEN)

    def test_reads_still_work_for_first_party_browsers(self):
        with self.client() as client:
            for path in ("/api/health", "/api/history", "/api/jobs/abc123def456"):
                for origin in (None, "http://localhost:3000", "http://127.0.0.1:41234"):
                    headers = {"origin": origin} if origin else {}
                    response = client.get(path, headers=headers)
                    self.assertIn(response.status_code, (200, 404), f"{path} {origin}")


if __name__ == "__main__":
    unittest.main()
