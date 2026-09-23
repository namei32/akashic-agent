"""Use existing isolated runtime model providers, without starting its Agent."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from agent.plugin_composition.models import CHAT_MODELS, EMBEDDINGS, ModelRequest
from agent.plugins.snapshot import lease_runtime_snapshot
from eval.longmemeval.runtime import close_runtime, create_runtime

from .schema import Settings


class RuntimeClient:
    def __init__(self, context, settings: Settings, *, model_id: str):
        self.context, self.settings, self.model_id = context, settings, model_id

    def embedding_descriptor(self):
        descriptor = self.context.require(EMBEDDINGS).describe()
        if (descriptor.model, descriptor.dimensions) != (self.settings.embedding_model, self.settings.dimensions):
            raise ValueError("Configured embedding provider differs from frozen settings")
        return {"model": descriptor.model, "dimensions": descriptor.dimensions, "space": descriptor.identity}

    async def embed(self, texts):
        expected = self.embedding_descriptor()
        async with self.context.require(EMBEDDINGS).bind() as model:
            descriptor = model.descriptor
            if (descriptor.model, descriptor.dimensions, descriptor.identity) != (
                expected["model"], expected["dimensions"], expected["space"]
            ):
                raise ValueError("Embedding provider changed while binding")
            result = await model.embed(texts)
            return {**expected, "vectors": result.vectors,
                    "usage": None if result.usage is None else asdict(result.usage)}

    async def complete(self, system: str, user: str, output_tokens: int):
        models = self.context.require(CHAT_MODELS)
        async with models.independent_execution(
            model_id=self.model_id, reasoning_effort=self.settings.effort
        ) as execution:
            model = execution.chat("agent")
            descriptor = model.descriptor
            if (descriptor.model, descriptor.reasoning_effort) != (self.settings.model, self.settings.effort):
                raise ValueError("Configured answer/judge model differs from frozen settings")
            response = await model.complete(ModelRequest(
                system_prompt=system, messages=({"role": "user", "content": user},),
                tools=(), tool_choice="none", max_output_tokens=output_tokens,
            ))
            return {"model": descriptor.model, "effort": descriptor.reasoning_effort,
                    "text": response.content, "tool_calls": [asdict(call) for call in response.tool_calls],
                    "usage": None if response.usage is None else asdict(response.usage),
                    "finish_reason": response.finish_reason,
                    "call_record_id": response.call_record_id}


@asynccontextmanager
async def runtime_client(config: Path, workspace: Path, settings: Settings):
    # create_runtime validates isolated template/plugin-home and refuses unknown workspaces.
    runtime = await create_runtime(config, workspace)
    try:
        # Do not call runtime.start(): no reply, memory or proactive workers are needed.
        async with lease_runtime_snapshot(runtime.core.plugin_manager.snapshot_store) as snapshot:
            if snapshot.composition_root is None:
                raise RuntimeError("Model provider composition unavailable")
            yield RuntimeClient(snapshot.composition_root.context, settings, model_id=runtime.agent_model.model_id)
    finally:
        await close_runtime(runtime)
