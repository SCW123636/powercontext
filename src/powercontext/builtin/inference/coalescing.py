# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Coalesce concurrent Embedding requests and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


@dataclass(slots=True)
class _Slot:
    """One distinct text waiting for its next provider batch."""

    text: str
    future: asyncio.Future[EmbeddingVector]
    batch: _Batch | None = None


@dataclass(slots=True)
class _Batch:
    """One provider request shared by every waiter that supplied its texts."""

    texts: tuple[str, ...]
    usage: InferenceUsage = field(default_factory=lambda: InferenceUsage(requests=0))


class CoalescingEmbeddingModel:
    """Share in-flight text embeddings across calls and cache successful vectors."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if isinstance(max_batch_size, bool) or not isinstance(max_batch_size, int) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, int | float) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if isinstance(cache_size, bool) or not isinstance(cache_size, int) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._slots: dict[str, _Slot] = {}
        self._pending: list[_Slot] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self.profile: EmbeddingProfile = model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed one call, joining or starting shared batches while preserving order."""
        unique = tuple(dict.fromkeys(texts))
        if not unique:
            return EmbeddingResult(vectors=())

        loop = asyncio.get_running_loop()
        resolved: dict[str, EmbeddingVector] = {}
        slots: dict[str, _Slot] = {}
        for text in unique:
            vector = self._cache_get(text)
            if vector is not None:
                resolved[text] = vector
                continue
            slot = self._slots.get(text)
            if slot is None:
                slot = _Slot(text=text, future=loop.create_future())
                slot.future.add_done_callback(_consume_exception)
                self._slots[text] = slot
                self._pending.append(slot)
            slots[text] = slot
        self._schedule_flush(loop)

        if slots:
            results = await asyncio.gather(*(asyncio.shield(slot.future) for slot in slots.values()))
            for text, vector in zip(slots, results, strict=True):
                resolved[text] = vector

        return EmbeddingResult(
            vectors=tuple(resolved[text] for text in texts),
            usage=_combined_usage(tuple(_distinct_batches(slots))),
        )

    def _schedule_flush(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._flush_handle is not None or not self._pending:
            return
        self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)

    def _flush(self) -> None:
        self._flush_handle = None
        if not self._pending:
            return
        loop = asyncio.get_running_loop()
        while self._pending:
            batch_slots = tuple(self._pending[: self._max_batch_size])
            del self._pending[: self._max_batch_size]
            batch = _Batch(texts=tuple(slot.text for slot in batch_slots))
            for slot in batch_slots:
                slot.batch = batch
            task = loop.create_task(self._execute(batch, batch_slots))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute(self, batch: _Batch, slots: tuple[_Slot, ...]) -> None:
        try:
            result = await self._model.embed(batch.texts)
            if len(result.vectors) != len(batch.texts):
                raise ValueError("embedding provider returned the wrong number of vectors")  # noqa: TRY003
            for slot, vector in zip(slots, result.vectors, strict=True):
                self._cache_put(slot.text, vector)
            batch.usage = result.usage
        except asyncio.CancelledError:
            for slot in slots:
                if not slot.future.done():
                    slot.future.cancel()
            raise
        except Exception as error:
            for slot in slots:
                if not slot.future.done():
                    slot.future.set_exception(error)
        else:
            for slot, vector in zip(slots, result.vectors, strict=True):
                if not slot.future.done():
                    slot.future.set_result(vector)
        finally:
            for slot in slots:
                if self._slots.get(slot.text) is slot:
                    del self._slots[slot.text]

    def _cache_get(self, text: str) -> EmbeddingVector | None:
        if self._cache_size == 0:
            return None
        vector = self._cache.get(text)
        if vector is not None:
            self._cache.move_to_end(text)
        return vector

    def _cache_put(self, text: str, vector: EmbeddingVector) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


def _distinct_batches(slots: dict[str, _Slot]) -> list[_Batch]:
    seen: set[int] = set()
    batches: list[_Batch] = []
    for slot in slots.values():
        batch = slot.batch
        if batch is None or id(batch) in seen:
            continue
        seen.add(id(batch))
        batches.append(batch)
    return batches


def _combined_usage(batches: tuple[_Batch, ...]) -> InferenceUsage:
    if not batches:
        return InferenceUsage(requests=0)
    return InferenceUsage(
        requests=sum(batch.usage.requests for batch in batches),
        input_tokens=_sum_optional(batch.usage.input_tokens for batch in batches),
        output_tokens=_sum_optional(batch.usage.output_tokens for batch in batches),
    )


def _sum_optional(values: Iterable[int | None]) -> int | None:
    tokens = tuple(values)
    if any(value is None for value in tokens):
        return None
    return sum(token for token in tokens if token is not None)


def _consume_exception(future: asyncio.Future[EmbeddingVector]) -> None:
    if not future.cancelled():
        future.exception()


__all__ = [
    "CoalescingEmbeddingModel",
    "DEFAULT_CACHE_SIZE",
    "DEFAULT_COALESCE_MS",
    "DEFAULT_MAX_BATCH_SIZE",
]
