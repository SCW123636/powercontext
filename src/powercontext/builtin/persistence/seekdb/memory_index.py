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

"""seekdb native hybrid candidate retrieval over full-text and vector indexes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyobvector import VECTOR
from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemoryCapabilities,
    MemoryProjection,
    MemorySearchChannels,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.persistence.memory_index import (
    MemoryIndex,
    memory_channel_hits,
)
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

# Each branch uses its native index and applies scope, artifact, tag and candidate-limit
# filters inside seekdb before the top-k. The two ordered rankings are returned by one
# statement; shared reciprocal-rank fusion still happens in the application.
_HYBRID_SQL = """
SELECT * FROM (
  (SELECT
      h.memory_artifact_id,
      h.head_revision,
      h.entry_id,
      h.entry_version_id,
      v.text,
      MATCH(h.searchable_text) AGAINST(:query_text) AS score,
      NULL AS distance,
      'fts' AS channel
   FROM pc_memory_entry_heads AS h
   JOIN pc_memory_entry_versions AS v
     ON v.scope_id = h.scope_id
    AND v.memory_artifact_id = h.memory_artifact_id
    AND v.entry_version_id = h.entry_version_id
   WHERE h.scope_id = :scope_id
     AND h.memory_artifact_id IN :memory_artifact_ids
     AND MATCH(h.searchable_text) AGAINST(:query_text)
     {fts_tag}
   ORDER BY MATCH(h.searchable_text) AGAINST(:query_text) DESC,
            h.memory_artifact_id, h.entry_id, h.entry_version_id
   LIMIT :candidate_limit)
  UNION ALL
  (SELECT
      m.memory_artifact_id,
      m.head_revision,
      m.entry_id,
      m.entry_version_id,
      v.text,
      NULL AS score,
      l2_distance(m.embedding, :query_vector) AS distance,
      'vector' AS channel
   FROM pc_memory_vector_entries AS m
   JOIN pc_memory_entry_versions AS v
     ON v.scope_id = m.scope_id
    AND v.memory_artifact_id = m.memory_artifact_id
    AND v.entry_version_id = m.entry_version_id
   WHERE m.scope_id = :scope_id
     AND m.memory_artifact_id IN :memory_artifact_ids
     {vector_tag}
   ORDER BY l2_distance(m.embedding, :query_vector){approximate}
   LIMIT :candidate_limit)
) AS hybrid_candidates
"""


class SeekDBMemoryHybridIndex:
    """One native seekdb statement for the full-text and vector candidate channels."""

    def __init__(self, fts: MemoryIndex, vector: MemoryIndex) -> None:
        profile = vector.capabilities.embedding_profile
        if profile is None or not vector.capabilities.vector:
            raise CapabilityNotSupportedError("vector")
        self._fts = fts
        self._vector = vector
        self._profile = profile
        self.capabilities = MemoryCapabilities(
            fts=fts.capabilities.fts,
            vector=True,
            hybrid=fts.capabilities.fts,
            embedding_profile=profile,
            tag_filter=fts.capabilities.tag_filter and vector.capabilities.tag_filter,
        )
        self.tables = fts.tables + vector.tables
        self._statement = self._compile(tagged=False)
        self._tagged_statement = self._compile(tagged=True)

    def _compile(self, *, tagged: bool) -> TextClause:
        sql = _HYBRID_SQL.format(
            fts_tag=memory_tag_sql("h") if tagged else "",
            vector_tag=memory_tag_sql("m") if tagged else "",
            # An approximate vector scan cannot combine with the correlated tag
            # predicate, so tagged hybrid queries keep the exact distance ranking.
            approximate="" if tagged else " APPROXIMATE",
        )
        statement = text(sql).bindparams(
            bindparam("memory_artifact_ids", expanding=True),
            bindparam("query_vector", type_=VECTOR(self._profile.dimension)),
        )
        if tagged:
            statement = statement.bindparams(
                bindparam("tag_keys", expanding=True),
                bindparam("tag_hashes", expanding=True),
            )
        return statement

    async def initialize(self, connection: AsyncConnection, /) -> None:
        await self._fts.initialize(connection)
        await self._vector.initialize(connection)

    async def replace(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self._fts.replace(connection, scope_id, memory_ref, projections)
        await self._vector.replace(connection, scope_id, memory_ref, projections)

    async def delete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        entry_ids: tuple[str, ...],
        /,
    ) -> None:
        await self._fts.delete(connection, scope_id, memory_ref, entry_ids)
        await self._vector.delete(connection, scope_id, memory_ref, entry_ids)

    async def upsert(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memory_ref: ArtifactRef,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> None:
        await self._fts.upsert(connection, scope_id, memory_ref, projections)
        await self._vector.upsert(connection, scope_id, memory_ref, projections)

    async def search(
        self,
        connection: AsyncConnection,
        scope_id: str,
        request: MemorySearchRequest,
        /,
    ) -> MemorySearchChannels:
        if request.mode == "fts":
            return await self._fts.search(connection, scope_id, request)
        if request.mode == "vector":
            return await self._vector.search(connection, scope_id, request)
        if request.mode != "hybrid":
            return MemorySearchChannels()
        if request.embedding_profile != self._profile or request.query_vector is None:
            raise CapabilityNotSupportedError("embedding-profile")
        if not await self.vector_complete(connection, scope_id, request.memories, self._profile):
            raise CapabilityNotSupportedError("vector")
        statement = self._statement if request.tag_filter is None else self._tagged_statement
        rows = (
            await connection.execute(
                statement,
                {
                    "query_text": request.analyzed_query,
                    "scope_id": scope_id,
                    "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
                    "candidate_limit": request.candidate_limit,
                    "query_vector": validate_embedding(
                        request.query_vector,
                        dimension=self._profile.dimension,
                    ),
                    **memory_tag_parameters(request.tag_filter),
                },
            )
        ).mappings().all()
        fts_rows = sorted(
            (row for row in rows if str(row["channel"]) == "fts"),
            key=_fts_rank,
        )
        vector_rows = sorted(
            (row for row in rows if str(row["channel"]) == "vector"),
            key=_vector_rank,
        )
        return MemorySearchChannels(
            fts=memory_channel_hits(fts_rows, request.memories),
            vector=memory_channel_hits(vector_rows, request.memories),
        )
    async def vector_complete(
        self,
        connection: AsyncConnection,
        scope_id: str,
        memories: tuple[ArtifactRef, ...],
        profile: EmbeddingProfile,
        /,
    ) -> bool:
        return await self._vector.vector_complete(connection, scope_id, memories, profile)

    async def hydrate(
        self,
        connection: AsyncConnection,
        scope_id: str,
        projections: tuple[MemoryProjection, ...],
        /,
    ) -> tuple[MemoryProjection, ...]:
        projections = await self._fts.hydrate(connection, scope_id, projections)
        return await self._vector.hydrate(connection, scope_id, projections)


def _fts_rank(row: Mapping[Any, Any]) -> tuple[float, bytes, bytes, bytes]:
    return (
        -float(row["score"]),
        str(row["memory_artifact_id"]).encode("utf-8"),
        str(row["entry_id"]).encode("utf-8"),
        str(row["entry_version_id"]).encode("utf-8"),
    )


def _vector_rank(row: Mapping[Any, Any]) -> tuple[float, bytes, bytes, bytes]:
    return (
        float(row["distance"]),
        str(row["memory_artifact_id"]).encode("utf-8"),
        str(row["entry_id"]).encode("utf-8"),
        str(row["entry_version_id"]).encode("utf-8"),
    )
