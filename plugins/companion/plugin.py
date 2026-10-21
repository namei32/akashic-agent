from __future__ import annotations

import asyncio
from contextlib import contextmanager
import socket

import uvicorn

from agent.plugin_composition import Context
from core.common.unix_socket import prepare_unix_socket, socket_alias_path
from plugins.content.contract import CONTENT
from plugins.ledger.contract import MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE, SESSION_ADMISSION
from plugins.models.contract import CHAT_MODELS

from .api import create_app
from .runtime import Companion

api_version = 3
name = "companion"
version = "0.1.0"
desc = "阅读时随手追问的桌宠：独立答疑、阅读引用与明确设置的解释偏好"
inject = (CONTENT, CHAT_MODELS, MESSAGE_CATALOG, MESSAGE_WRITERS, OWNER_STATE, SESSION_ADMISSION)


class Server(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = asyncio.Event()

    @contextmanager
    def capture_signals(self):
        yield

    async def startup(self, sockets=None):
        await super().startup(sockets)
        self.ready.set()


async def apply(ctx: Context) -> None:
    companion = Companion(ctx)
    app = create_app(companion)
    async def request(scope, receive, send):
        async with ctx.runtime_scope():
            await app(scope, receive, send)
    path = socket_alias_path(ctx.runtime.workspace / "runtime", "companion.sock")
    server = Server(uvicorn.Config(request, log_level="warning", lifespan="off", timeout_graceful_shutdown=5))

    def setup():
        prepare_unix_socket(path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.setblocking(False)
        except BaseException:
            listener.close()
            raise
        task = asyncio.create_task(server.serve(sockets=[listener]), name="companion-http")
        async def close():
            server.should_exit = True
            try:
                await task
            finally:
                listener.close()
                path.unlink(missing_ok=True)
        return close

    await ctx.effect(setup, label="companion.http")
    await asyncio.wait_for(server.ready.wait(), 10)
    await ctx.endpoint("api", protocol="http+unix", address=str(path), routes=("/api/companion",))
