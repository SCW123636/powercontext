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

"""Capture the selected branch of a Pi v3 session as immutable events.

The session file stores a tree through ``parentId`` links. Only the ancestor
chain of the selected leaf is evidence, and control records stay on that chain
without producing text. A Connector keeps a durable checkpoint of imported
event digests so branches, appends and restarts never duplicate or overwrite
already captured evidence.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBytes, field_validator, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
    TextEvidence,
)
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"

_NonEmptyText = Annotated[str, Field(min_length=1)]
_CHECKPOINT_VERSION = 1


def _reject_blank(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field} must not be blank")  # noqa: TRY003
    return value


class PiSessionInput(BaseModel):
    """One UTF-8 JSONL Pi session archive and an optional selected leaf."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: StrictBytes
    leaf_id: str | None = None

    @field_validator("leaf_id")
    @classmethod
    def validate_leaf_id(cls, value: str | None) -> str | None:
        if value is not None:
            _reject_blank(value, "leaf_id")
        return value


class PiEventInput(BaseModel):
    """One visible event on the selected branch with its complete record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: _NonEmptyText
    event_id: _NonEmptyText
    parent_id: str | None
    event_type: _NonEmptyText
    role: _NonEmptyText
    content: _NonEmptyText
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """The immutable captured form of one visible Pi event."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]
    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED

    @model_validator(mode="after")
    def validate_identity(self) -> PiEventSource:
        expected = pi_event_source_id(self.session_id, self.event_id)
        if self.source_id != expected:
            raise ValueError("source_id does not match the event identity")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("name must equal source_id")  # noqa: TRY003
        return self


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return a stable, bounded identity for one event in one session."""

    if not isinstance(session_id, str) or not session_id or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")  # noqa: TRY003
    if not isinstance(event_id, str) or not event_id or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")  # noqa: TRY003
    readable = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(readable) <= MAX_SOURCE_ID_LENGTH:
        return readable
    material = json.dumps([session_id, event_id], ensure_ascii=False, separators=(",", ":"))
    return f"pi:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the selected leaf's visible events from root to leaf."""

    try:
        text = value.content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error

    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Pi session content must be JSON Lines") from error
        if not isinstance(record, dict):
            raise ValueError("every Pi session record must be a JSON object")  # noqa: TRY003
        records.append(record)

    if not records:
        raise ValueError("Pi session archive must not be empty")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("the first record must be a version 3 session header")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("the session header must have a non-empty id")  # noqa: TRY003

    by_id: dict[str, dict[str, JsonValue]] = {}
    parent_by_id: dict[str, str | None] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("every Pi session record must have a non-empty id")  # noqa: TRY003
        if event_id in by_id:
            raise ValueError(f"duplicate Pi session record id: {event_id}")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError(f"Pi session record {event_id} must declare parentId")  # noqa: TRY003
        parent_id = record["parentId"]
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError(f"Pi session record {event_id} has an invalid parentId")  # noqa: TRY003
        if parent_id and parent_id not in by_id:
            raise ValueError(f"Pi session record {event_id} has a missing parent")  # noqa: TRY003
        by_id[event_id] = record
        parent_by_id[event_id] = parent_id or None
        order.append(event_id)

    if not order:
        raise ValueError("Pi session archive must contain at least one record after the header")  # noqa: TRY003
    leaf_id = value.leaf_id if value.leaf_id is not None else order[-1]
    if leaf_id not in by_id:
        raise ValueError(f"Pi session leaf does not exist: {leaf_id}")  # noqa: TRY003

    chain: list[str] = []
    cursor: str | None = leaf_id
    while cursor is not None:
        chain.append(cursor)
        cursor = parent_by_id[cursor]
    chain.reverse()

    events: list[PiEventInput] = []
    for event_id in chain:
        record = by_id[event_id]
        event = _visible_event(session_id, event_id, record)
        if event is not None:
            events.append(event)
    return tuple(events)


def _visible_event(session_id: str, event_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    parent_id = record.get("parentId")
    if parent_id is not None and not isinstance(parent_id, str):
        parent_id = None

    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(role, message)
    elif event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            return None
        role = event_type
        body = summary
    elif event_type == "custom_message":
        role = "custom"
        body = _content_text(record.get("content"))
    else:
        return None

    if not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: dict[str, JsonValue]) -> str:
    if role == "toolResult":
        name = _text(message.get("toolName"))
        call_id = _text(message.get("toolCallId"))
        return f"[tool_result {name} {call_id}] {_content_text(message.get('content'))}"
    if role == "bashExecution":
        command = _text(message.get("command"))
        output = _text(message.get("output"))
        return f"$ {command}\n{output}"
    return _content_text(message.get("content"))


def _content_text(content: JsonValue) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall":
            name = _text(block.get("name"))
            call_id = _text(block.get("id"))
            arguments = block.get("arguments")
            if arguments is None:
                arguments = {}
            encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            parts.append(f"[tool_call {name} {call_id}] {encoded}")
    return "\n".join(parts)


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


class PiEventSourceAdapter:
    """Resolve and read the captured form of one Pi event."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one event's complete original text and fixed identity."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Import only the not-yet-captured events of the selected branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        try:
            content = self._path.read_bytes()
        except OSError as error:
            raise ValueError(f"cannot read Pi session file: {self._path}") from error
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))
        previous = _load_checkpoint(session.checkpoint)
        current = _record_digests(content)

        for (stored_session, stored_event), digest in previous.items():
            present = current.get((stored_session, stored_event))
            if present is not None and present != digest:
                raise ValueError(f"captured Pi event was rewritten: {stored_event}")  # noqa: TRY003

        checkpoint = dict(previous)
        for event in events:
            key = (event.session_id, event.event_id)
            if key in checkpoint:
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                checkpoint[key] = _digest(event.record)

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_encode_checkpoint(checkpoint),
        )


def _record_digests(content: bytes) -> dict[tuple[str, str], str]:
    """Digest every visible record in the archive, not only the selected branch."""

    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return {}
    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return {}
        if not isinstance(record, dict):
            return {}
        records.append(record)
    if not records:
        return {}
    session_id = records[0].get("id")
    if not isinstance(session_id, str):
        return {}
    digests: dict[tuple[str, str], str] = {}
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str):
            continue
        event = _visible_event(session_id, event_id, record)
        if event is not None:
            digests[(session_id, event_id)] = _digest(record)
    return digests


def _digest(record: dict[str, JsonValue]) -> str:
    material = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _encode_checkpoint(checkpoint: dict[tuple[str, str], str]) -> JsonValue:
    return {
        "version": _CHECKPOINT_VERSION,
        "events": [
            {"session_id": session_id, "event_id": event_id, "digest": digest}
            for (session_id, event_id), digest in sorted(checkpoint.items())
        ],
    }


def _load_checkpoint(raw: JsonValue | None) -> dict[tuple[str, str], str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or raw.get("version") != _CHECKPOINT_VERSION:
        raise ValueError("Pi session checkpoint is not recognized")  # noqa: TRY003
    entries = raw.get("events")
    if not isinstance(entries, list):
        raise ValueError("Pi session checkpoint is not recognized")  # noqa: TRY003
    checkpoint: dict[tuple[str, str], str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Pi session checkpoint is not recognized")  # noqa: TRY003
        session_id = entry.get("session_id")
        event_id = entry.get("event_id")
        digest = entry.get("digest")
        if not all(isinstance(value, str) and value for value in (session_id, event_id, digest)):
            raise ValueError("Pi session checkpoint is not recognized")  # noqa: TRY003
        checkpoint[(session_id, event_id)] = digest
    return checkpoint


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
