from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray

from agent.plugin_composition.messages import MessageCatalog, MessageEmbeddings
from agent.plugin_composition.models import Embeddings
from agent.plugin_contracts import Input
from agent.plugin_contracts.turns import TurnProjection
from core.common.file_io import run_file_io

from .config import Config
from .sources import Suppress, approved_turns, text


class Interest:
    """Keep Wake's 0.9/0.1, max-cosine**4 contract independent of remote recall."""

    def __init__(
        self,
        config: Config,
        catalog: MessageCatalog,
        projection: TurnProjection,
        embeddings: MessageEmbeddings,
        model: Embeddings,
        suppress: Suppress,
    ):
        self.config, self.catalog, self.projection = config, catalog, projection
        self.embeddings, self.model, self.suppress = embeddings, model, suppress
        self._lock = asyncio.Lock()
        self._closed = False

    def decision(self) -> bool:
        return self.config.enabled

    def status(self) -> str | None:
        if self._closed:
            return "Hindsight 兴趣服务已关闭"
        try:
            _ = self.model.describe()
        except (RuntimeError, ValueError) as error:
            return str(error)
        return None

    def close(self) -> None:
        self._closed = True

    async def score(self, texts: Sequence[str], *, cutoff: str) -> tuple[float, ...]:
        through = datetime.fromisoformat(cutoff)
        if through.tzinfo is None or through.utcoffset() is None:
            raise ValueError("兴趣截止时间必须包含时区")
        if any(not isinstance(value, str) for value in texts):
            raise TypeError("兴趣候选必须是字符串")
        async with self._lock:
            reason = self.status()
            if reason is not None:
                raise RuntimeError(reason)
            descriptor = self.model.describe()
            samples = await run_file_io(
                lambda: approved_turns(
                    self.catalog,
                    self.projection,
                    self.config,
                    self.suppress,
                    cutoff=through,
                )[-256:]
            )
            if not samples or not any(value.strip() for value in texts):
                return tuple(0.0 for _ in texts)
            records = self.embeddings.bind(text)
            messages = {
                m.message_id: m
                for _, members in samples
                for m in members
                if text(m).strip() and (isinstance(m.body, Input) or m is members[-1])
            }

            def missing():
                return [
                    m
                    for m in messages.values()
                    if records.read(
                        m, model=descriptor.identity, dimension=descriptor.dimensions
                    )
                    is None
                ]

            needed = await run_file_io(missing)
            async with self.model.bind(model_id=descriptor.model_id) as bound:
                if bound.descriptor.identity != descriptor.identity:
                    raise ValueError("兴趣向量空间在本次调用中发生变化")
                for start in range(0, len(needed), 64):
                    batch = needed[start : start + 64]
                    generated = await bound.embed([text(m) for m in batch])
                    self._validate(generated.vectors, len(batch), descriptor.dimensions)

                    def save(batch=batch, vectors=generated.vectors):
                        for message, vector in zip(batch, vectors, strict=True):
                            records.save(
                                message,
                                model=descriptor.identity,
                                embedding=list(vector),
                            )

                    await run_file_io(save)
                active = [(i, value) for i, value in enumerate(texts) if value.strip()]
                generated = await bound.embed([value for _, value in active])
            self._validate(generated.vectors, len(active), descriptor.dimensions)

            def prototypes() -> list[NDArray[np.floating[Any]]]:
                values: list[NDArray[np.floating[Any]]] = []
                for _, members in samples:
                    users = [
                        m
                        for m in members
                        if isinstance(m.body, Input) and text(m).strip()
                    ]
                    vectors = [
                        np.asarray(
                            records.read(
                                m,
                                model=descriptor.identity,
                                dimension=descriptor.dimensions,
                            ),
                            dtype=np.float32,
                        )
                        for m in users
                    ]
                    user = (
                        vectors[0]
                        if len(vectors) == 1
                        else np.mean(np.stack(vectors), axis=0)
                    )
                    if len(vectors) > 1:
                        norm = float(np.linalg.norm(user))
                        if norm == 0:
                            continue
                        user = user / norm
                    answer = np.asarray(
                        records.read(
                            members[-1],
                            model=descriptor.identity,
                            dimension=descriptor.dimensions,
                        ),
                        dtype=np.float32,
                    )
                    combined = 0.9 * user + 0.1 * answer
                    norm = float(np.linalg.norm(combined))
                    if norm > 0:
                        values.append(combined / norm)
                return values

            history = await run_file_io(prototypes)
            scores = [0.0] * len(texts)
            for (index, _), vector in zip(active, generated.vectors, strict=True):
                candidate = np.asarray(vector, dtype=np.float32)
                norm = float(np.linalg.norm(candidate))
                if norm > 0 and history:
                    cosine = max(
                        float(np.dot(candidate / norm, value)) for value in history
                    )
                    scores[index] = min(0.999, max(0.0, cosine) ** 4)
            return tuple(scores)

    @staticmethod
    def _validate(
        vectors: Sequence[Sequence[float]], count: int, dimension: int
    ) -> None:
        if len(vectors) != count:
            raise ValueError("兴趣 embedding 数量不匹配")
        for value in vectors:
            array = np.asarray(value, dtype=np.float32)
            if (
                array.shape != (dimension,)
                or not np.all(np.isfinite(array))
                or float(np.linalg.norm(array)) == 0
            ):
                raise ValueError("兴趣 embedding 维度、有限性或零向量无效")
