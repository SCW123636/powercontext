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

"""Incremental Context Pack transport for prepared context."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_UNSIGNED_PACK_FIELDS = ("schema", "items")
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """Raised when a Context Pack or delta violates the wire protocol."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete, self-verifying chunked context snapshot."""

    items: tuple[bytes, ...]
    digest: str

    @classmethod
    def build(cls, items: Sequence[bytes], /) -> ContextPack:
        """Build a pack from already chunked bytes."""
        normalized = _coerce_items(items)
        return cls(items=normalized, digest=_pack_digest(normalized))

    @classmethod
    def from_content(cls, content: str, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk UTF-8 content deterministically without splitting a character."""
        return cls.build(_chunk_content(content, chunk_bytes))

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and verify one wire pack."""
        raw = _coerce_raw(data)
        payload = _load_object(raw)
        if set(payload) != _PACK_FIELDS:
            raise ContextPackError("pack must contain exactly schema, items, digest, and byte_count")  # noqa: TRY003
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack schema is not supported")  # noqa: TRY003
        items = _decode_items(payload["items"], field="items")
        digest = _expect_digest(payload["digest"], field="digest")
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ContextPackError("pack byte_count must be an integer")  # noqa: TRY003
        if byte_count != len(raw):
            raise ContextPackError("pack byte_count does not match the encoded length")  # noqa: TRY003
        if _pack_digest(items) != digest:
            raise ContextPackError("pack digest does not match its items")  # noqa: TRY003
        return cls(items=items, digest=digest)

    @property
    def byte_count(self) -> int:
        """Encoded wire size, including the byte_count field itself."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Encode the canonical wire representation with a self-consistent byte count."""
        unsigned: dict[str, Any] = {"schema": PACK_SCHEMA, "items": _encode_items(self.items), "digest": self.digest}
        count = len(_canonical({**unsigned, "byte_count": 0}))
        while True:
            encoded = _canonical({**unsigned, "byte_count": count})
            if len(encoded) == count:
                return encoded
            count = len(encoded)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """The reusable base prefix plus the appended target suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Reuse the longest common item prefix; never longer than either side."""
        _verify_pack(base)
        _verify_pack(target)
        prefix = 0
        for base_item, target_item in zip(base.items, target.items, strict=False):
            if base_item != target_item:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and verify one wire delta."""
        payload = _load_object(_coerce_raw(data))
        if set(payload) != _DELTA_FIELDS:
            raise ContextPackError("delta must contain exactly the protocol fields")  # noqa: TRY003
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta schema is not supported")  # noqa: TRY003
        return cls(
            base_digest=_expect_digest(payload["base_digest"], field="base_digest"),
            target_digest=_expect_digest(payload["target_digest"], field="target_digest"),
            prefix_items=_expect_prefix(payload["prefix_items"]),
            appended=_decode_items(payload["appended"], field="appended"),
        )

    @property
    def byte_count(self) -> int:
        """Encoded wire size of the delta, excluding any byte_count field."""
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Encode the canonical wire representation."""
        _validate_delta(self)
        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild and verify the target pack from a base pack and one delta."""
    _verify_pack(base)
    _validate_delta(delta)
    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the supplied pack")  # noqa: TRY003
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base chunk count")  # noqa: TRY003
    rebuilt = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack digest does not match the delta target")  # noqa: TRY003
    return rebuilt


def _chunk_content(content: str, chunk_bytes: object) -> tuple[bytes, ...]:
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
        raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
    if not content:
        return ()
    chunks: list[bytes] = []
    current = bytearray()
    for line in content.splitlines(keepends=True):
        encoded = line.encode("utf-8")
        if len(encoded) <= chunk_bytes:
            if current and len(current) + len(encoded) > chunk_bytes:
                chunks.append(bytes(current))
                current.clear()
            current.extend(encoded)
            continue
        # Keep a short header line its own chunk so later contexts can reuse it.
        if current:
            chunks.append(bytes(current))
            current.clear()
        for character in line:
            encoded_char = character.encode("utf-8")
            if len(encoded_char) > chunk_bytes:
                raise ContextPackError("chunk_bytes is too small for one UTF-8 character")  # noqa: TRY003
            if len(current) + len(encoded_char) > chunk_bytes:
                chunks.append(bytes(current))
                current.clear()
            current.extend(encoded_char)
    if current:
        chunks.append(bytes(current))
    return tuple(chunks)


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Sequence[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(items)}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _coerce_items(items: Sequence[bytes]) -> tuple[bytes, ...]:
    if isinstance(items, bytes | bytearray):
        raise ContextPackError("items must be a sequence of byte strings")  # noqa: TRY003
    normalized = tuple(items)
    if not all(isinstance(item, bytes) for item in normalized):
        raise ContextPackError("items must be a sequence of byte strings")  # noqa: TRY003
    return normalized


def _coerce_raw(data: object) -> bytes:
    if not isinstance(data, bytes | bytearray):
        raise ContextPackError("wire payload must be bytes")  # noqa: TRY003
    return bytes(data)


def _load_object(raw: bytes) -> dict[str, Any]:
    try:
        payload: Any = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("wire payload is not valid UTF-8 JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise ContextPackError("wire payload must be a JSON object")  # noqa: TRY003
    return payload


def _decode_items(value: object, *, field: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{field} must be a list of Base64 strings")  # noqa: TRY003
    decoded: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError(f"{field} must be a list of Base64 strings")  # noqa: TRY003
        try:
            decoded.append(base64.b64decode(entry.encode("ascii"), validate=True))
        except (UnicodeEncodeError, binascii.Error) as error:
            raise ContextPackError(f"{field} contains invalid Base64") from error  # noqa: TRY003
    return tuple(decoded)


def _expect_digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
        raise ContextPackError(f"{field} must be a sha256: prefixed digest")  # noqa: TRY003
    return value


def _expect_prefix(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextPackError("prefix_items must be a non-negative integer")  # noqa: TRY003
    return value


def _validate_delta(delta: ContextPackDelta) -> None:
    _expect_digest(delta.base_digest, field="base_digest")
    _expect_digest(delta.target_digest, field="target_digest")
    _expect_prefix(delta.prefix_items)
    _coerce_items(delta.appended)


def _verify_pack(pack: ContextPack) -> None:
    if not isinstance(pack, ContextPack):
        raise ContextPackError("pack must be a ContextPack")  # noqa: TRY003
    _expect_digest(pack.digest, field="digest")
    if _pack_digest(pack.items) != pack.digest:
        raise ContextPackError("pack digest does not match its items")  # noqa: TRY003


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
