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

"""Persistence for caller-declared Source lifecycle graphs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE


@dataclass(frozen=True, slots=True)
class LifecycleNodeRow:
    """One registered node with its persisted propagated availability."""

    scope_id: str
    node_id: str
    kind: str
    policy: str | None
    parents: tuple[str, ...]
    reference: str
    available: bool
    source_revision: int | None
    source_sequence: int
    source_reason: str | None
    registration_order: int
    artifact_id: str | None


def encode_reference(value: dict[str, Any]) -> str:
    """Serialize one reference payload deterministically for replay comparison."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encode_parents(parents: tuple[str, ...]) -> str:
    return json.dumps(list(parents), ensure_ascii=False, separators=(",", ":"))


def _decode_row(row: Any) -> LifecycleNodeRow:
    return LifecycleNodeRow(
        scope_id=str(row["scope_id"]),
        node_id=str(row["node_id"]),
        kind=str(row["kind"]),
        policy=None if row["policy"] is None else str(row["policy"]),
        parents=tuple(str(parent) for parent in json.loads(str(row["parents"]))),
        reference=str(row["reference"]),
        available=bool(row["available"]),
        source_revision=None if row["source_revision"] is None else int(row["source_revision"]),
        source_sequence=int(row["source_sequence"]),
        source_reason=None if row["source_reason"] is None else str(row["source_reason"]),
        registration_order=int(row["registration_order"]),
        artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
    )


class SourceLifecycleRepository:
    """Read and write the scope-local lifecycle graph and its state columns."""

    async def list(self, connection: AsyncConnection, scope_id: str, /) -> tuple[LifecycleNodeRow, ...]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE)
                .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
                .order_by(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.registration_order,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id,
                )
            )
        ).mappings().all()
        return tuple(_decode_row(row) for row in rows)

    async def get(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
    ) -> LifecycleNodeRow | None:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
                )
            )
        ).mappings().first()
        return None if row is None else _decode_row(row)

    async def next_order(self, connection: AsyncConnection, scope_id: str, /) -> int:
        current = await connection.scalar(
            select(func.max(SOURCE_LIFECYCLE_NODES_TABLE.c.registration_order)).where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id
            )
        )
        return int(current or 0) + 1

    async def insert(self, connection: AsyncConnection, node: LifecycleNodeRow, /) -> None:
        await connection.execute(
            insert(SOURCE_LIFECYCLE_NODES_TABLE).values(
                scope_id=node.scope_id,
                node_id=node.node_id,
                kind=node.kind,
                policy=node.policy,
                parents=encode_parents(node.parents),
                reference=node.reference,
                available=node.available,
                source_revision=node.source_revision,
                source_sequence=node.source_sequence,
                source_reason=node.source_reason,
                registration_order=node.registration_order,
                artifact_id=node.artifact_id,
            )
        )

    async def set_source_state(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        /,
        *,
        available: bool,
        sequence: int,
        reason: str | None,
    ) -> None:
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(available=available, source_sequence=sequence, source_reason=reason)
        )

    async def set_available(
        self,
        connection: AsyncConnection,
        scope_id: str,
        node_id: str,
        available: bool,
        /,
    ) -> None:
        await connection.execute(
            update(SOURCE_LIFECYCLE_NODES_TABLE)
            .where(
                SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node_id,
            )
            .values(available=available)
        )

    async def unavailable_topic_artifact_ids(
        self,
        connection: AsyncConnection,
        scope_id: str,
        /,
    ) -> frozenset[str]:
        """Return Topic artifact identities whose lifecycle state is unavailable."""

        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_NODES_TABLE.c.artifact_id).where(
                    SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
                    SOURCE_LIFECYCLE_NODES_TABLE.c.kind == "topic",
                    SOURCE_LIFECYCLE_NODES_TABLE.c.available.is_(False),
                    SOURCE_LIFECYCLE_NODES_TABLE.c.artifact_id.is_not(None),
                )
            )
        ).scalars().all()
        return frozenset(str(value) for value in rows)
