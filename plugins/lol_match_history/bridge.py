"""在国服客户端所在电脑运行的认证只读 HTTP 桥接。"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pydantic import ValidationError

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = Path(__file__).resolve().parent.name

from .query import HOSTS, Client, DetailQuery, HistoryQuery, QueryError


def serve(lockfile: Path, platform: str, token: str, listen: str, port: int) -> None:
    capacity = threading.BoundedSemaphore(4)

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format: str, *args: object) -> None:
            pass  # 请求、token、客户端资料均不写 HTTP 日志。

        def respond(self, status: int, payload: dict[str, object]) -> None:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self, method: str) -> None:
            self.close_connection = True
            supplied = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
                self.respond(401, QueryError("unauthorized", "桥接认证失败").payload())
                return
            if (method, self.path) not in {
                ("GET", "/v1/status"),
                ("POST", "/v1/history"),
                ("POST", "/v1/detail"),
            }:
                self.respond(404, QueryError("not_found", "不存在的查询入口").payload())
                return
            if not capacity.acquire(blocking=False):
                self.respond(
                    429,
                    QueryError("busy", "桥接正在处理其他查询，请稍后重试").payload(),
                )
                return
            client = None
            try:
                body = {}
                if method == "POST":
                    size = int(self.headers.get("Content-Length", "0"))
                    if self.headers.get("Transfer-Encoding") or not 0 < size <= 8192:
                        raise QueryError("invalid_request", "请求体大小无效")
                    body = json.loads(self.rfile.read(size))
                query = (
                    HistoryQuery.model_validate(body)
                    if self.path == "/v1/history"
                    else (
                        DetailQuery.model_validate(body)
                        if self.path == "/v1/detail"
                        else None
                    )
                )
                client = Client(lockfile, platform)
                if isinstance(query, HistoryQuery):
                    result = client.history(query)
                elif isinstance(query, DetailQuery):
                    result = client.detail(query)
                else:
                    me = client.current()
                    result = {
                        "connected": True,
                        "platform": platform,
                        "game_name": me.get("gameName", me.get("displayName")),
                        "tag_line": me.get("tagLine"),
                        "protocol_version": 1,
                    }
                self.respond(200 if "error" not in result else 502, result)
            except (ValueError, ValidationError):
                self.respond(
                    400, QueryError("invalid_request", "查询参数无效").payload()
                )
            except QueryError as error:
                self.respond(
                    400 if error.code == "invalid_request" else 502, error.payload()
                )
            finally:
                if client is not None:
                    client.close()
                capacity.release()

        def do_GET(self) -> None:
            self.dispatch("GET")

        def do_POST(self) -> None:
            self.dispatch("POST")

    server = ThreadingHTTPServer((listen, port), Handler)
    server.daemon_threads = True
    print(f"LOL 只读桥接：{listen}:{server.server_port}，大区 {platform}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lockfile", required=True, type=Path, help="LOL 安装目录中的 lockfile"
    )
    parser.add_argument(
        "--platform", required=True, choices=tuple(HOSTS), help="当前登录的合区代号"
    )
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=22361)
    parser.add_argument("--token-env", default="AKASHIC_LOL_BRIDGE_TOKEN")
    args = parser.parse_args()
    token = os.environ.get(args.token_env, "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        parser.error("token 环境变量必须包含至少 32 个无空白 ASCII 字符")
    serve(args.lockfile, args.platform, token, args.listen, args.port)


if __name__ == "__main__":
    main()
