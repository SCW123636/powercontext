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

"""Select literal Memory-entry spans that answer the current query.

The selector is pure and offline: it scores sentence-sized units of the candidate entry
texts with a small BM25-style lexical model, packs whole runs of adjacent units into the
caller's exact UTF-8 budget, and renders the selected spans with their exact citations.
Nothing is rewritten: every fragment is a Python character slice of one Memory entry.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.memory_fragments import MemoryEntryAddress, PreparedContextFragment
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

if TYPE_CHECKING:
    from powercontext.builtin.runtime.prepared_context import PreparedMemoryCandidates

_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[a-z]+|\d+")
_STEM_SUFFIXES = ("ational", "ation", "ating", "ates", "ated", "ies", "ing", "ed", "es", "s")
_MAX_UNIT_BYTES = 1100
_MAX_RUN_UNITS = 4
_EXPANSION_PASSES = 2
_STEM_WEIGHT = 0.75
_BIGRAM_WEIGHT = 0.5
_POSITION_WEIGHT = 0.6
_MARKER_WEIGHT = 1.5
_FIRST_UNIT_WEIGHT = 0.5
_DENSITY_EXPONENT = 1.2
_ESTIMATED_ITEM_BYTES = 276
_STOPWORDS = frozenset(
    "a an the of to in on for and or but with without by as at from is are was were be been being this that these those "
    "it its their they we our can could may might will would should do does did not no than then there here which who "
    "what when where why how also more most less least such into over under between among during before after about "
    "has have had using used use new two one three".split()
)
_RESULT_MARKERS = ("results", "conclusion", "conclusions", "findings", "interpretation")


@dataclass
class _Unit:
    start: int
    end: int
    counts: Counter[str]
    length: int
    index: int
    score: float = 0.0


@dataclass
class _Document:
    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str
    text: str
    units: list[_Unit] = field(default_factory=list)
    score: float = 0.0


class _Packing:
    """Incrementally grow a set of literal spans while the rendered output fits."""

    def __init__(self, documents: list[_Document], max_bytes: int) -> None:
        self.documents = documents
        self.max_bytes = max_bytes
        self.spans: dict[int, list[list[int]]] = {index: [] for index in range(len(documents))}
        self.fragments = _fragments(documents, self.spans)
        self.usable = _fits(self.fragments, max_bytes)

    def add(self, document_index: int, start: int, end: int) -> bool:
        document = self.documents[document_index]
        spans = self.spans[document_index]
        merged: list[list[int]] = []
        for old_start, old_end in sorted([(span[0], span[1]) for span in spans] + [(start, end)]):
            if merged and _joins(document.text, merged[-1][1], old_start):
                merged[-1][1] = max(merged[-1][1], old_end)
            else:
                merged.append([old_start, old_end])
        previous = [list(span) for span in spans]
        spans[:] = merged
        candidate = _fragments(self.documents, self.spans)
        if _fits(candidate, self.max_bytes):
            self.fragments = candidate
            return True
        spans[:] = previous
        return False

    def pack_runs(self) -> None:
        cap = self.max_bytes - _ESTIMATED_ITEM_BYTES
        ranking: list[tuple[float, float, int, int, int]] = []
        for rank, document in enumerate(self.documents):
            for first in range(len(document.units)):
                score = 0.0
                for last in range(first, min(first + _MAX_RUN_UNITS, len(document.units))):
                    score += document.units[last].score
                    begin = document.units[first].start
                    finish = document.units[last].end
                    cost = len(document.text[begin:finish].encode("utf-8")) + _ESTIMATED_ITEM_BYTES
                    if cost > cap:
                        break
                    if score > 0:
                        ranking.append((score / (cost**_DENSITY_EXPONENT), score, rank, begin, finish))
        ranking.sort(key=lambda row: (-row[0], -row[1], row[2], row[3]))
        for _, _, index, start, end in ranking:
            self.add(index, start, end)

    def expand_best(self) -> None:
        document = self.documents[0]
        for _ in range(_EXPANSION_PASSES):
            chosen = self.spans[0]
            expanding = [
                unit
                for unit in document.units
                if not any(start <= unit.start and unit.end <= end for start, end in chosen)
                and any(
                    _joins(document.text, end, unit.start) or _joins(document.text, unit.end, start)
                    for start, end in chosen
                )
            ]
            expanding.sort(key=lambda unit: (-unit.score, unit.start))
            for unit in expanding:
                self.add(0, unit.start, unit.end)

    def fill_leftovers(self) -> None:
        leftovers: list[tuple[bool, float, int, _Unit]] = []
        for index, document in enumerate(self.documents):
            for unit in document.units:
                if unit.score > 0:
                    leftovers.append((index != 0, -unit.score, index, unit))
        leftovers.sort(key=lambda row: (row[0], row[1], row[2], row[3].start))
        for _, _, index, unit in leftovers:
            self.add(index, unit.start, unit.end)


def select_memory_fragments(
    *,
    query: str,
    memory_candidates: Sequence[PreparedMemoryCandidates],
    max_bytes: int,
) -> tuple[PreparedContextFragment, ...]:
    """Choose query-relevant literal spans that fit the complete rendered output."""

    documents = _documents(memory_candidates)
    if not documents:
        return ()
    _score_documents(query, documents)
    documents.sort(key=lambda document: document.score, reverse=True)
    packing = _Packing(documents, max_bytes)
    if not packing.usable:
        return ()
    packing.pack_runs()
    packing.expand_best()
    packing.fill_leftovers()
    return tuple(packing.fragments)


def render_selected_context(fragments: tuple[PreparedContextFragment, ...]) -> str:
    """Render selected fragments inside the standard trusted-envelope markers."""

    items = []
    for fragment in fragments:
        address = fragment.address
        items.append(
            {
                "citation": {
                    "scope_id": address.memory.scope_id,
                    "memory_ref": address.memory.artifact.model_dump(mode="json"),
                    "entry_id": address.entry_id,
                    "entry_version_id": address.entry_version_id,
                },
                "content": fragment.text,
                "start": fragment.start,
                "end": fragment.end,
            }
        )
    envelope = {"trust": "untrusted_history", "items": items}
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _documents(memory_candidates: Sequence[PreparedMemoryCandidates]) -> list[_Document]:
    documents: list[_Document] = []
    seen: set[tuple[str, str, str]] = set()
    for candidates in memory_candidates:
        scope_id = candidates.scope_id
        for hit in candidates.hits:
            text = hit.text
            if not text.strip():
                continue
            identity = (scope_id, hit.entry_id, hit.entry_version_id)
            if identity in seen:
                continue
            seen.add(identity)
            memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
            document = _Document(
                memory=memory,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
                text=text,
            )
            document.units = []
            for index, (start, end) in enumerate(_unit_spans(text)):
                counts = Counter(_word_tokens(text[start:end]))
                document.units.append(
                    _Unit(start=start, end=end, counts=counts, length=max(1, sum(counts.values())), index=index)
                )
            documents.append(document)
    return documents


def _word_tokens(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _stem(token: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if len(token) - len(suffix) >= 4 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _unit_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BREAK.finditer(text):
        end = match.start()
        if end > start:
            spans.append((start, end))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    pieces: list[tuple[int, int]] = []
    for span_start, span_end in spans:
        pieces.extend(_split_long_unit(text, span_start, span_end))
    return pieces


def _split_long_unit(text: str, start: int, end: int) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    while len(text[start:end].encode("utf-8")) > _MAX_UNIT_BYTES:
        limit = start
        size = 0
        while limit < end:
            size += len(text[limit].encode("utf-8"))
            if size > _MAX_UNIT_BYTES:
                break
            limit += 1
        cut = text.rfind(" ", start, limit)
        if cut <= start:
            cut = limit
        pieces.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if start < end:
        pieces.append((start, end))
    return pieces


def _score_documents(query: str, documents: list[_Document]) -> None:
    raw_terms = _word_tokens(query)
    terms = [term for term in dict.fromkeys(raw_terms) if term not in _STOPWORDS]
    if not terms:
        terms = list(dict.fromkeys(raw_terms))
    if not terms:
        for document in documents:
            for unit in document.units:
                unit.score = 1.0
        _finalize_scores(documents)
        return
    term_stems = {term: _stem(term) for term in terms}
    pairs = list(zip(raw_terms, raw_terms[1:], strict=False))

    unit_count = sum(len(document.units) for document in documents)
    frequency: Counter[str] = Counter()
    for document in documents:
        for unit in document.units:
            present = [_stem(token) for token in unit.counts]
            for term in terms:
                if term in unit.counts:
                    frequency[term] += 1
                elif term_stems[term] in present:
                    frequency[term] += 1
    idf = {term: math.log((unit_count + 1) / (frequency[term] + 1)) + 1.0 for term in terms}
    average = max(1.0, sum(unit.length for document in documents for unit in document.units) / max(unit_count, 1))

    for document in documents:
        for unit in document.units:
            text = document.text[unit.start : unit.end].casefold()
            stems = {_stem(token) for token in unit.counts}
            matched = 0
            score = 0.0
            for term in terms:
                weight = 1.0 if term in unit.counts else (_STEM_WEIGHT if term_stems[term] in stems else 0.0)
                if weight <= 0:
                    continue
                matched += 1
                term_frequency = unit.counts[term] if weight == 1.0 else 1
                norm = 1.2 * (0.25 + 0.75 * unit.length / average)
                score += idf[term] * weight * term_frequency * 2.2 / (term_frequency + norm)
            coverage = matched / len(terms)
            bigrams = sum(
                1
                for left, right in pairs
                if left in unit.counts and right in unit.counts and f"{left} {right}" in text
            )
            score = (score + _BIGRAM_WEIGHT * bigrams) * (0.35 + coverage)
            if unit.index == 0 and len(document.units) > 1:
                score *= _FIRST_UNIT_WEIGHT
            if text.lstrip().startswith(_RESULT_MARKERS):
                score *= _MARKER_WEIGHT
            unit.score = score

    _finalize_scores(documents)


def _finalize_scores(documents: list[_Document]) -> None:
    for document in documents:
        count = len(document.units)
        for unit in document.units:
            relative = unit.index / max(count - 1, 1)
            unit.score *= 1.0 + _POSITION_WEIGHT * (relative - 0.5)
        ranked = sorted((unit.score for unit in document.units), reverse=True)
        document.score = sum(ranked[:3]) + 0.3 * sum(ranked[3:6])


def _joins(text: str, end: int, start: int) -> bool:
    if start <= end:
        return True
    return not text[end:start].strip()


def _fragments(documents: list[_Document], selected: dict[int, list[list[int]]]) -> list[PreparedContextFragment]:
    fragments: list[PreparedContextFragment] = []
    for index, document in enumerate(documents):
        for start, end in sorted(selected[index]):
            fragments.append(
                PreparedContextFragment(
                    address=MemoryEntryAddress(
                        memory=document.memory,
                        entry_id=document.entry_id,
                        entry_version_id=document.entry_version_id,
                    ),
                    start=start,
                    end=end,
                    text=document.text[start:end],
                )
            )
    return fragments


def _fits(fragments: list[PreparedContextFragment], max_bytes: int) -> bool:
    return len(render_selected_context(tuple(fragments)).encode("utf-8")) <= max_bytes
