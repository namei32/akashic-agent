from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import Headers, MutableHeaders

from .runtime import Ask, Companion, Preferences, Scene


class SameOrigin:
    """Pure ASGI middleware preserves the actual request task's owner scope."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        host = headers.get("x-akashic-host", headers.get("host", ""))
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"} and (
            headers.get("x-akashic-csrf") != "1"
            or (origin is not None and urlsplit(origin).netloc.casefold() != host.casefold())
        ):
            return await JSONResponse({"detail": "请从 Akashic 页面内使用桌宠。"}, status_code=403)(scope, receive, send)
        async def uncached(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)
        await self.app(scope, receive, uncached)


def create_app(companion: Companion) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SameOrigin)

    @app.get("/api/companion/state")
    async def state(parent: str = Query(default="", max_length=512)):
        return await companion.state(parent)

    @app.post("/api/companion/preferences")
    async def preferences(value: Preferences):
        await companion.save("preferences", value.model_dump())
        return await companion.preferences()

    @app.post("/api/companion/scene")
    async def scene(value: Scene):
        try:
            await companion.scene(value)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"saved": True}

    @app.post("/api/companion/ask")
    async def ask(value: Ask):
        try:
            return await companion.start(value)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/companion/stop/{request_id}")
    async def stop(request_id: str):
        try:
            return await companion.stop(request_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error

    @app.get("/api/companion/events/{request_id}")
    async def events(request_id: str):
        try:
            await companion.result(request_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        async def stream():
            # StreamingResponse may run its iterator in a distinct ASGI task.
            async with companion.ctx.runtime_scope():
                previous = ""
                while True:
                    value = await companion.result(request_id)
                    encoded = json.dumps(value, ensure_ascii=False)
                    if encoded != previous:
                        yield encoded + "\n"
                        previous = encoded
                    if value["status"] != "running":
                        return
                    await asyncio.sleep(0.15)
        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"X-Accel-Buffering": "no"})

    return app
