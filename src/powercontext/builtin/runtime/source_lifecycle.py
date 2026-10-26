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

"""Propagate caller-declared Source availability through dependent evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryVersion, MemoryService
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.source_lifecycle import (
    LifecycleNodeRow,
    SourceLifecycleRepository,
    encode_reference,
)
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.topic_memory import TopicMemoryRepository
from powercontext.errors import ArtifactNotFoundError
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

Purpose = Literal["generation", "audit"]
Kind = Literal["source", "memory", "topic"]

_PURPOSES = frozenset({"generation", "audit"})
_POLICIES = frozenset({"all", "any"})
_TRANSITION_REASON = "source-lifecycle"


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle registration, event, or query breaks the contract."""


class SourceLifecycleEvent(BaseModel):
    """One strictly validated Source availability transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    revision: int = Field(gt=0, strict=True)
    sequence: int = Field(gt=0, strict=True)
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")  # noqa: TRY003
        return value


def _encoded_reference(kind: Kind, reference: SourceRef | MemoryCitation | ArtifactRef) -> str:
    return encode_reference({kind: reference.model_dump(mode="json")})


def _decode_reference(kind: str, payload: str) -> SourceRef | MemoryCitation | ArtifactRef:
    try:
        value = json.loads(payload)[kind]
    except (KeyError, TypeError, ValueError) as error:
        raise SourceLifecycleError("stored-reference") from error
    if kind == "source":
        return SourceRef.model_validate(value)
    if kind == "memory":
        return MemoryCitation.model_validate(value)
    if kind == "topic":
        return ArtifactRef.model_validate(value)
    raise SourceLifecycleError("stored-kind")


def _validate_node_id(node_id: object) -> str:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node-id")
    if len(node_id) > MAX_SOURCE_ID_LENGTH:
        raise SourceLifecycleError("node-id")
    return node_id


def _validate_policy(policy: object) -> str:
    if not isinstance(policy, str) or policy not in _POLICIES:
        raise SourceLifecycleError("policy")
    return policy


def _normalize_parents(parents: object) -> tuple[str, ...]:
    if isinstance(parents, (str, bytes)) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents")
    normalized = tuple(_validate_node_id(parent) for parent in parents)
    if not normalized:
        raise SourceLifecycleError("parents")
    if len(set(normalized)) != len(normalized):
        raise SourceLifecycleError("parents")
    return normalized


def _derive(parents: tuple[str, ...], states: dict[str, bool], policy: str) -> bool:
    try:
        support = tuple(states[parent] for parent in parents)
    except KeyError as error:
        raise SourceLifecycleError("missing-parent") from error
    return all(support) if policy == "all" else any(support)


class SourceLifecycleService:
    """Apply one scope's registered dependency graph to native evidence state."""

    def __init__(
        self,
        *,
        database: AsyncDatabase,
        scope_id: str,
        nodes: SourceLifecycleRepository,
        sources: SourceRepository,
        topics: TopicMemoryRepository,
        memory: Callable[[], MemoryService],
        memory_artifact_id: str,
    ) -> None:
        self._database = database
        self._scope_id = scope_id
        self._nodes = nodes
        self._sources = sources
        self._topics = topics
        self._memory = memory
        self._memory_artifact_id = memory_artifact_id

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        /,
        *,
        revision: int = 3,
    ) -> None:
        """Register one Source at a fixed lifecycle revision."""

        node = _validate_node_id(node_id)
        if type(revision) is not int or revision <= 0:
            raise SourceLifecycleError("revision")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-ref")
        async with self._database.transaction() as connection:
            try:
                await self._sources.get(connection, self._scope_id, source_ref)
            except Exception as error:
                raise SourceLifecycleError("source-ref") from error
            existing = await self._nodes.get(connection, self._scope_id, node)
            if existing is not None:
                self._require_same(
                    existing,
                    kind="source",
                    reference=source_ref,
                    parents=(),
                    policy=None,
                    revision=revision,
                )
                return
            await self._nodes.insert(
                connection,
                LifecycleNodeRow(
                    scope_id=self._scope_id,
                    node_id=node,
                    kind="source",
                    policy=None,
                    parents=(),
                    reference=_encoded_reference("source", source_ref),
                    available=True,
                    source_revision=revision,
                    source_sequence=0,
                    source_reason=None,
                    registration_order=await self._nodes.next_order(connection, self._scope_id),
                    artifact_id=None,
                ),
            )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        """Register one exact Memory entry as a derived node."""

        node = _validate_node_id(node_id)
        normalized_parents = _normalize_parents(parents)
        normalized_policy = _validate_policy(policy)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-citation")
        memory = self._memory()
        try:
            await memory.validate_citation(citation)
        except Exception as error:
            raise SourceLifecycleError("memory-citation") from error
        await self._register_derived(
            node,
            kind="memory",
            reference=citation,
            parents=normalized_parents,
            policy=normalized_policy,
            artifact_id=citation.memory_ref.artifact_id,
        )
        await self._apply_native_states()

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        """Register one exact Topic Revision as a derived node."""

        node = _validate_node_id(node_id)
        normalized_parents = _normalize_parents(parents)
        normalized_policy = _validate_policy(policy)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("topic-ref")
        async with self._database.transaction() as connection:
            try:
                await self._topics.get_exact(connection, self._scope_id, artifact_ref)
            except Exception as error:
                raise SourceLifecycleError("topic-ref") from error
            await self._register_derived_in(
                connection,
                node,
                kind="topic",
                reference=artifact_ref,
                parents=normalized_parents,
                policy=normalized_policy,
                artifact_id=artifact_ref.artifact_id,
            )

    async def _register_derived(
        self,
        node_id: str,
        *,
        kind: Kind,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: str,
        artifact_id: str | None,
    ) -> None:
        async with self._database.transaction() as connection:
            await self._register_derived_in(
                connection,
                node_id,
                kind=kind,
                reference=reference,
                parents=parents,
                policy=policy,
                artifact_id=artifact_id,
            )

    async def _register_derived_in(
        self,
        connection: AsyncConnection,
        node_id: str,
        *,
        kind: Kind,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: str,
        artifact_id: str | None,
    ) -> None:
        existing = await self._nodes.get(connection, self._scope_id, node_id)
        if existing is not None:
            self._require_same(
                existing,
                kind=kind,
                reference=reference,
                parents=parents,
                policy=policy,
                revision=None,
            )
            return
        rows = await self._nodes.list(connection, self._scope_id)
        by_id = {row.node_id: row for row in rows}
        if node_id in by_id:
            raise SourceLifecycleError("node-id")
        for parent in parents:
            if parent == node_id or parent not in by_id:
                raise SourceLifecycleError("parent")
        states = {row.node_id: row.available for row in rows}
        await self._nodes.insert(
            connection,
            LifecycleNodeRow(
                scope_id=self._scope_id,
                node_id=node_id,
                kind=kind,
                policy=policy,
                parents=parents,
                reference=_encoded_reference(kind, reference),
                available=_derive(parents, states, policy),
                source_revision=None,
                source_sequence=0,
                source_reason=None,
                registration_order=await self._nodes.next_order(connection, self._scope_id),
                artifact_id=artifact_id,
            ),
        )

    def _require_same(
        self,
        existing: LifecycleNodeRow,
        *,
        kind: Kind,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: str | None,
        revision: int | None,
    ) -> None:
        if (
            existing.kind != kind
            or _decode_reference(existing.kind, existing.reference) != reference
            or existing.parents != parents
            or existing.policy != policy
            or (revision is not None and existing.source_revision != revision)
        ):
            raise SourceLifecycleError("registration-conflict")

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Accept one Source event, propagate it, and reconcile native Memory state."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event")
        async with self._database.transaction() as connection:
            source = await self._nodes.get(connection, self._scope_id, event.node_id)
            if source is None or source.kind != "source":
                raise SourceLifecycleError("unknown-source")
            if source.source_revision != event.revision:
                raise SourceLifecycleError("revision")
            if event.sequence < source.source_sequence:
                raise SourceLifecycleError("sequence")
            if event.sequence == source.source_sequence and (
                event.available != source.available or event.reason != source.source_reason
            ):
                raise SourceLifecycleError("sequence")
            if event.sequence > source.source_sequence:
                await self._nodes.set_source_state(
                    connection,
                    self._scope_id,
                    event.node_id,
                    available=event.available,
                    sequence=event.sequence,
                    reason=event.reason,
                )
            rows = await self._nodes.list(connection, self._scope_id)
            states = {row.node_id: row.available for row in rows}
            if event.sequence > source.source_sequence:
                states[event.node_id] = event.available
            for row in rows:
                if row.kind == "source":
                    continue
                propagated = _derive(row.parents, states, row.policy or "all")
                if propagated != row.available:
                    states[row.node_id] = propagated
                    await self._nodes.set_available(
                        connection, self._scope_id, row.node_id, propagated
                    )
            active = sorted(node_id for node_id, available in states.items() if available)
        await self._apply_native_states()
        return active

    async def resolve(self, node_id: str, /, *, purpose: Purpose) -> SourceRef | MemoryCitation | ArtifactRef:
        """Return the original reference when the requested purpose permits it."""

        node = _validate_node_id(node_id)
        if not isinstance(purpose, str) or purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose")
        async with self._database.transaction() as connection:
            row = await self._nodes.get(connection, self._scope_id, node)
        if row is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not row.available:
            raise SourceLifecycleError("unavailable")
        return _decode_reference(row.kind, row.reference)

    async def _apply_native_states(self) -> None:
        """Make the persisted Memory node states true in the native Memory head."""

        async with self._database.transaction() as connection:
            rows = await self._nodes.list(connection, self._scope_id)
        desired: dict[str, bool] = {}
        for row in rows:
            if row.kind != "memory":
                continue
            citation = _decode_reference("memory", row.reference)
            if not isinstance(citation, MemoryCitation):
                raise SourceLifecycleError("stored-reference")
            # A shared exact entry is usable only while every claimant still is.
            desired[citation.entry_id] = desired.get(citation.entry_id, True) and row.available
        if not desired:
            return
        memory = self._memory()
        try:
            head = await memory.head(self._memory_artifact_id)
        except (ArtifactNotFoundError, RepositoryNotFoundError):
            return
        manifest = {entry.entry_id: entry for entry in head.content.manifest.entries}
        entries: dict[str, MemoryEntryVersion] = {
            entry.entry_id: entry for entry in await memory.entries(head)
        }
        retire = [
            entries[entry_id]
            for entry_id, available in desired.items()
            if not available
            and entry_id in manifest
            and entry_id in entries
            and manifest[entry_id].state != "inactive"
        ]
        restore = [
            entries[entry_id]
            for entry_id, available in desired.items()
            if available
            and entry_id in manifest
            and entry_id in entries
            and manifest[entry_id].state != "active"
        ]
        if retire:
            head = await memory.forget(head, entries=retire, reason=_TRANSITION_REASON)
        if restore:
            await memory.reactivate(head, entries=restore, reason=_TRANSITION_REASON)


class ScopedSourceLifecycleApplication:
    """Bind the Source lifecycle contract to one Runtime scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        from powercontext.builtin.sources import validate_scope_id

        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    def _service(self) -> SourceLifecycleService:
        factory = getattr(self._runtime._provider, "source_lifecycle", None)
        if factory is None:
            raise SourceLifecycleError("lifecycle-unavailable")
        service = factory(self.scope_id)
        if not isinstance(service, SourceLifecycleService):
            raise SourceLifecycleError("lifecycle-unavailable")
        return service

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        /,
        *,
        revision: int = 3,
    ) -> None:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            await self._service().register_source(node_id, source_ref, revision=revision)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            await self._service().register_memory(
                node_id, citation, parents=parents, policy=policy
            )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: str = "all",
    ) -> None:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            await self._service().register_topic(
                node_id, artifact_ref, parents=parents, policy=policy
            )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        async with self._runtime._scoped_operation(self.scope_id), self._runtime._locked(self.scope_id):
            return await self._service().apply(event)

    async def resolve(
        self,
        node_id: str,
        /,
        *,
        purpose: Purpose,
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        async with self._runtime._scoped_operation(self.scope_id):
            return await self._service().resolve(node_id, purpose=purpose)


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle service."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id)
