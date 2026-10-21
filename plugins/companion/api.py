from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .runtime import Ask, Companion, Preferences, Scene


def create_app(companion: Companion) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        host = request.headers.get("x-akashic-host", request.headers.get("host", ""))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("x-akashic-csrf") != "1"
            or (origin is not None and urlsplit(origin).netloc.casefold() != host.casefold())
        ):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "请从 Akashic 页面内使用桌宠。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

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
