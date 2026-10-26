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

"""HTTP cross-encoder reranking for coarse Memory candidates."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

_SCORE_FIELDS = {"rank", "score"}
_RESPONSE_FIELDS = {"scores"}


class HTTPMemoryReranker:
    """Score coarse Memory candidates with one HTTP cross-encoder service.

    The service caps how many candidates one request may carry, so the pool is
    split into batches while every candidate keeps its original global rank.
    Batch scores are merged before the final selection, which keeps the chosen
    order independent of batching and response order.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once and select the globally highest ranks."""

        _validate_bounds(len(candidates), limit)
        scored: list[tuple[float, int]] = []
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            first_rank = start + 1
            expected_ranks = range(first_rank, first_rank + len(batch))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text}
                        for rank, candidate in zip(expected_ranks, batch, strict=True)
                    ],
                },
            )
            requests += 1
            _ensure_success(response, endpoint=self._endpoint)
            scored.extend(_parse_scores(response, expected_ranks=expected_ranks))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return MemoryRerankDecision(
            selected_ranks=tuple(rank for _, rank in scored[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _ensure_success(response: httpx.Response, *, endpoint: str) -> None:
    """Raise an httpx error for any non-2xx service response."""

    if 200 <= response.status_code < 300:
        return
    try:
        request = response.request
    except RuntimeError:
        request = httpx.Request("POST", endpoint)
    raise httpx.HTTPStatusError(
        f"rerank service returned HTTP {response.status_code}",
        request=request,
        response=response,
    )


def _parse_scores(
    response: httpx.Response,
    *,
    expected_ranks: Iterable[int],
) -> list[tuple[float, int]]:
    """Validate one batch response and return its (score, rank) pairs."""

    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise ValueError("rerank response is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _RESPONSE_FIELDS:
        raise ValueError("rerank response must contain exactly the scores field")  # noqa: TRY003
    raw_scores = payload["scores"]
    if not isinstance(raw_scores, list):
        raise ValueError("rerank scores must be a list")  # noqa: TRY003
    expected = set(expected_ranks)
    seen: set[int] = set()
    parsed: list[tuple[float, int]] = []
    for item in raw_scores:
        if not isinstance(item, dict) or set(item) != _SCORE_FIELDS:
            raise ValueError("rerank score must contain exactly rank and score")  # noqa: TRY003
        rank = item["rank"]
        score = item["score"]
        if isinstance(rank, bool) or not isinstance(rank, int) or rank not in expected or rank in seen:
            raise ValueError("rerank score ranks must cover this batch exactly once")  # noqa: TRY003
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise ValueError("rerank score must be a number")  # noqa: TRY003
        if isinstance(score, float) and not math.isfinite(score):
            raise ValueError("rerank score must be finite")  # noqa: TRY003
        seen.add(rank)
        parsed.append((float(score), rank))
    if seen != expected:
        raise ValueError("rerank score ranks must cover this batch exactly once")  # noqa: TRY003
    return parsed


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


__all__ = ["HTTPMemoryReranker"]
