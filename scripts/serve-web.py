#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""声析 · 浏览器模式启动器（不依赖 npm / node）

为什么需要它：`out/` 是静态导出，页面的 API_BASE 在 production 下是空串 ——
所有 /api/* 请求走同源。而 `start.bat` 原来用 `npm run dev` 起前端，开发服务器
并不代理 /api，于是页面永远停在"服务启动中"、并且只能读到示例数据。

这个脚本做两件最小的事：
  1) 在 127.0.0.1:<port> 托管 `out/`（静态资源 + SPA 回退到 index.html）；
  2) 把同源的 /api/* 原样反向代理到后端（默认 127.0.0.1:8000）。

它不解析业务、不改响应体、不缓存 —— 只做转发，后端仍是唯一的事实来源。
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import os
import socket
import sys
import threading
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    backend_host = "127.0.0.1"
    backend_port = 8000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.join(ROOT, "out"), **kwargs)

    def log_message(self, fmt, *args):  # 安静一点：只报错误
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[static] " + (fmt % args) + "\n")

    # ---------------------------------------------------------------- proxy
    def _proxy(self):
        parsed = urllib.parse.urlsplit(self.path)
        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        conn = http.client.HTTPConnection(self.backend_host, self.backend_port, timeout=1800)
        try:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_HEADERS}
            conn.request(self.command, target, body=body, headers=headers)
            upstream = conn.getresponse()
            self.send_response(upstream.status, upstream.reason)
            for key, value in upstream.getheaders():
                if key.lower() in HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionRefusedError, OSError) as exc:
            # 后端没起来就如实说：页面靠这个 502 判断"服务没起"，不会误报成功。
            payload = ('{"ok":false,"error":{"code":"backend_unreachable","message":"%s"}}'
                       % str(exc).replace('"', "'")).encode("utf-8")
            self.send_response(502)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            conn.close()

    def do_GET(self):
        if self.path == "/api" or self.path.startswith("/api/"):
            self._proxy()
        else:
            super().do_GET()

    def do_HEAD(self):
        if self.path == "/api" or self.path.startswith("/api/"):
            self._proxy()
        else:
            super().do_HEAD()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()

    # SPA 回退：/audio 这类前端路由交给 index.html
    def send_head(self):
        path = self.translate_path(self.path)
        if not os.path.isdir(path) and not os.path.exists(path) and "." not in os.path.basename(path):
            self.path = "/index.html"
        return super().send_head()


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def main() -> int:
    parser = argparse.ArgumentParser(description="声析浏览器模式：静态页 + /api 反向代理")
    parser.add_argument("--port", type=int, default=int(os.getenv("VOCAL_SEPARATOR_UI_PORT", "3000")))
    parser.add_argument("--backend-port", type=int, default=int(os.getenv("VOCAL_SEPARATOR_PORT", "8000")))
    parser.add_argument("--backend-host", default="127.0.0.1")
    args = parser.parse_args()

    index = os.path.join(ROOT, "out", "index.html")
    if not os.path.exists(index):
        print("[fatal] 找不到 out/index.html：请先 npm run build", file=sys.stderr)
        return 2

    port = args.port
    for candidate in range(args.port, args.port + 20):
        if port_is_free(candidate):
            port = candidate
            break
    else:
        print("[fatal] %d-%d 都被占用" % (args.port, args.port + 19), file=sys.stderr)
        return 3

    Handler.backend_host = args.backend_host
    Handler.backend_port = args.backend_port
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("声析界面: %s" % url)
    print("代理 /api/* -> http://%s:%d" % (args.backend_host, args.backend_port))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())