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

"""Capture every physical PDF page and retrieve exact page-level evidence.

A PDF is split only at physical page boundaries. Page text stays verbatim, so a
citation can be traced back to the original file digest and the page number
counted from the first page of the PDF rather than a printed page label.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, StrictBytes, field_validator, model_validator
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.artifacts import MemoryCitation

PDF_PAGE_SOURCE_NAME = "pdf-page"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(r"[a-z]+|\d+")

# Query-side function words and generic execution instructions. Kept deliberately
# small: domain words such as "statement", "income" and "cash" must survive.
_QUERY_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    few for from further had has have having he her here hers him his how i if in into
    is it its itself just me more most my no nor not now of off on once only or other
    our out over own same she should so some such than that the their theirs them then
    there these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your
    answer answers assume assuming calculate calculated calculation company define defined
    details drove explain explicitly give given like metric much outlined please question
    questions rely report reported reporting respond response shown state stated stated use
    useful using within perspective able
    """.split()
)

# Conservative expansions for finance-benchmark paraphrases. Each expansion only
# adds a weak alternative term; the original wording always keeps full weight.
_EXPANSIONS = {
    "acquisition": ("acquire",),
    "acquire": ("acquisition",),
    "battle": ("proceeding", "litigation"),
    "battles": ("proceeding", "litigation"),
    "capex": ("capital", "expenditure"),
    "geographic": ("region",),
    "geographies": ("geographic", "region"),
    "geography": ("geographic", "region"),
    "grow": ("growth",),
    "growth": ("grow",),
    "improve": ("improvement",),
    "improving": ("improve", "improvement"),
    "litigation": ("proceeding",),
    "lawsuit": ("litigation", "proceeding"),
    "operate": ("operation", "operating", "geographic"),
    "operates": ("operation", "operating", "geographic"),
    "quantity": ("amount",),
    "region": ("geographic",),
    "regions": ("geographic",),
    "retain": ("retention",),
    "retention": ("retain",),
    "revenue": ("sale",),
    "sale": ("revenue",),
}

_INCOME_TERMS = frozenset(
    "cost earning ebit ebitda expense gross income margin net operating operations profit revenue sale tax".split()
)
_BALANCE_TERMS = frozenset(
    "asset balance capital current liability liquidity quick ratio working".split()
)
_CASH_TERMS = frozenset(
    "amortization buyback capex cash depreciation dividend expenditure flow repurchase".split()
)
_SEGMENT_TERMS = frozenset("category product segment service".split())
_CHANGE_TERMS = frozenset(
    "change compare decline decrease drive drove grow growth improve improvement increase".split()
)

# Headings that mark a page as part of the consolidated financial statements block.
_INCOME_HEADS = (
    "consolidated statement of income",
    "consolidated statements of income",
    "consolidated statement of operations",
    "consolidated statements of operations",
    "consolidated statement of earnings",
    "consolidated statements of earnings",
    "summary of financial performance",
)
_BALANCE_HEADS = ("consolidated balance sheet", "consolidated balance sheets")
_CASH_HEADS = (
    "consolidated statement of cash flow",
    "consolidated statements of cash flows",
)
# Segment tables name their own rows; a question about a segment is answered there.
_SEGMENT_HEADS = (
    "by business segment",
    "net sales by segment",
    "summary of business segment data",
    "segment information",
    "disaggregation of revenue",
)

_K1 = 1.2
_B = 0.35
_BIGRAM_WEIGHT = 0.35
_SECTION_WEIGHT = 1.5
_STATEMENT_WEIGHT = 2.0

# The consolidated statements form one block, and a question about the figures in a
# filing is answered by that block even when it names no single statement.
_FAMILY_WEIGHT = 1.0

# A filing the question does not point at is a guess, so it is never allowed to take
# the whole result set: every other filing keeps this many slots of the pool.
_SECONDARY_SLOTS = 2
_AFFINITY_POWER = 2.0

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_WORD = re.compile(r"[a-z0-9]+")
_LEADING_NAME = re.compile(r"[a-z0-9]{2,}")

# A name this short can hide inside unrelated words, so it only counts as a mention
# when it stands alone in the question.
_MIN_PHRASE_NAME = 4

# A heading is printed in the opening block of a page. Matching a statement title
# anywhere on the page would also match the many body paragraphs that mention it
# ("our Consolidated Balance Sheets"), which then outrank the statement itself.
_LEAD_CHARS = 240

# The fixed pipeline stores one Memory entry per page, and a durable entry body is
# bounded by the Memory contract. A denser page has to fit that bound or the whole
# document fails to load, so its tail is cut at a character boundary.
_MAX_PAGE_BYTES = 8192


def _reject_blank(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field} must not be blank")  # noqa: TRY003
    return value


class PDFDocumentInput(BaseModel):
    """One complete original PDF to split into physical-page Sources."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    content: StrictBytes

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _reject_blank(value, "document_id")

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: bytes) -> bytes:
        if not value:
            raise ValueError("content must not be empty")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """The verbatim extractable text of one physical PDF page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _reject_blank(value, "document_id")

    @field_validator("document_sha256")
    @classmethod
    def validate_document_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("document_sha256 must be 64 lowercase hexadecimal characters")  # noqa: TRY003
        return value

    @field_validator("page_number")
    @classmethod
    def validate_page_number(cls, value: int) -> int:
        if value < 1:
            raise ValueError("page_number must be at least 1")  # noqa: TRY003
        return value


class PDFPageSource(Source):
    """One captured physical page; ``source_id`` binds document, digest and page."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str
    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        expected = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.source_id != expected:
            raise ValueError("source_id does not match the page identity")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("name must equal source_id")  # noqa: TRY003
        return self


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Return a stable, collision-free identity bounded by the Source id limit."""

    _reject_blank(document_id, "document_id")
    if _SHA256.fullmatch(document_sha256) is None:
        raise ValueError("document_sha256 must be 64 lowercase hexadecimal characters")  # noqa: TRY003
    if type(page_number) is not int or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    readable = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(readable) <= MAX_SOURCE_ID_LENGTH:
        return readable
    material = f"{document_id}\0{document_sha256}\0{page_number}".encode()
    return f"pdf:{hashlib.sha256(material).hexdigest()}"


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse and validate every physical page before returning any of them."""

    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
    except Exception as error:
        raise ValueError("content must be a readable PDF document") from error

    try:
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("PDF requires a non-empty password")  # noqa: TRY003
        raw_pages = reader.pages
        page_count = len(raw_pages)
        if page_count < 1:
            raise ValueError("PDF document must contain at least one page")  # noqa: TRY003
        texts = tuple(_page_text(page, value.document_id) for page in raw_pages)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF document could not be parsed completely") from error

    document_sha256 = hashlib.sha256(value.content).hexdigest()
    return tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=document_sha256,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(texts, start=1)
    )


def _page_text(page: Any, document_id: str) -> str:
    """Return one page's extractable text, fitted to what a Memory entry can hold.

    A dense table page can exceed the durable entry bound, and then the fixed pipeline
    refuses the whole document instead of storing it. Cutting the tail keeps the page
    loadable and keeps its heading and the start of its tables, which is where the
    questions point; the surviving words still describe the same page.
    """

    text = page.extract_text()
    if not isinstance(text, str) or not text:
        return ""
    budget = _MAX_PAGE_BYTES - len(document_id.encode("utf-8")) - 1
    if budget <= 0 or len(text.encode("utf-8")) <= budget:
        return text
    return text.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip()


class PDFPageSourceAdapter:
    """Resolve and read the page Source owned by this Definition."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one page as ``DOCUMENT_ID\\nPAGE_TEXT`` plus its exact location."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)


def _singular(token: str) -> str:
    """Fold common English inflections without touching numbers or short words."""

    if token.isdigit() or len(token) <= 3:
        return token
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("ity") and len(token) > 6:
        return token[:-3]
    if token.endswith("es") and len(token) > 4 and token[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _tokens(text: str) -> list[str]:
    return [_singular(token) for token in _TOKEN.findall(text.casefold())]


def _query(question: str) -> tuple[dict[str, float], list[str]]:
    weights: dict[str, float] = {}
    sequence: list[str] = []
    for token in _TOKEN.findall(question.casefold()):
        if token in _QUERY_STOPWORDS or (len(token) == 1 and not token.isdigit()):
            continue
        term = _singular(token)
        weights.setdefault(term, 1.0)
        sequence.append(term)
    for term in tuple(weights):
        for expansion in _EXPANSIONS.get(term, ()):
            expanded = _singular(expansion)
            if expanded not in weights:
                weights[expanded] = 0.5
    return weights, sequence


class _Page:
    __slots__ = ("citation", "counts", "document_id", "length", "bigrams", "lead")

    def __init__(self, citation: Any, text: str) -> None:
        document_id, _, body = text.partition("\n")
        tokens = _tokens(body)
        self.citation = citation
        self.document_id = document_id.strip()
        self.counts = Counter(tokens)
        self.length = max(len(tokens), 1)
        self.bigrams = frozenset(zip(tokens, tokens[1:], strict=False))
        self.lead = body[:_LEAD_CHARS].casefold()


def _identity_of(document_id: str) -> str:
    """Return the leading name part of a document id, if it holds anything nameable."""

    match = _LEADING_NAME.match(document_id.casefold())
    return match.group() if match is not None else ""


def _named_filings(documents: dict[str, list[_Page]], question: str) -> set[str]:
    """Return the filings named in the question, judged by the leading part of the id.

    A long name is matched against the question with punctuation removed, so
    "American Express" points at ``AMERICANEXPRESS_2022_10K``. A short name such as
    ``3m`` or ``aes`` would otherwise hide inside numbers and word endings, so it has
    to appear as a word of its own.
    """

    squashed = _NON_ALNUM.sub("", question.casefold())
    words = set(_WORD.findall(question.casefold()))
    named = set()
    for document in documents:
        identity = _identity_of(document)
        if not identity:
            continue
        if len(identity) >= _MIN_PHRASE_NAME:
            if identity in squashed:
                named.add(document)
        elif identity in words:
            named.add(document)
    return named


def _document_claim(
    documents: dict[str, list[_Page]], question: str, weights: dict[str, float]
) -> dict[str, float]:
    """Return how strongly the question points at each filing, on a 0 to 1 scale.

    A filing named in the question is certain. Otherwise a term that fills most pages
    of one filing and hardly appears in the others points at it. The value only orders
    the filings and splits the retrieval slots between them; it never rescales what a
    page is worth, because a filing that is merely unpopular still has to be able to
    show its own best pages.
    """

    if len(documents) < 2:
        return {name: 1.0 for name in documents}
    named = _named_filings(documents, question)
    spread: dict[str, float] = {name: 0.0 for name in documents}
    for term, weight in weights.items():
        coverage = {
            name: sum(1 for item in pages if term in item.counts) / len(pages)
            for name, pages in documents.items()
        }
        mean = sum(coverage.values()) / len(coverage)
        for name, value in coverage.items():
            if value > mean:
                spread[name] += weight * (value - mean)
    best = max(spread.values(), default=0.0)
    if best == 0.0:
        return {name: 1.0 if name in named else 0.0 for name in documents}
    return {
        name: 1.0
        if name in named
        else (spread[name] / best) ** _AFFINITY_POWER
        for name in documents
    }


def _document_slots(
    order: list[str], candidates: dict[str, int], named: set[str], limit: int
) -> dict[str, int]:
    """Split ``limit`` retrieval slots between filings, best claim first.

    A question that names its filing has the whole pool for that filing: pages of
    another company are not evidence, however well their words match. A question that
    names none is only a guess from term coverage, so the other filings keep
    ``_SECONDARY_SLOTS`` each and at most half the pool. A filing that cannot fill its
    share leaves the rest to be taken in claim order.
    """

    leaders = [name for name in order if name in named]
    if len(leaders) != 1:
        leaders = leaders or order[:1]
    secondary = [name for name in order if name not in leaders]
    slots = dict.fromkeys(order, 0)
    if len(leaders) > 1:
        share = max(1, limit // len(leaders))
        for index, name in enumerate(leaders):
            capacity = limit - sum(slots.values()) if index == len(leaders) - 1 else share
            slots[name] = min(capacity, candidates[name])
        return slots
    reserve = 0 if named else limit // 2
    for name in secondary:
        take = min(_SECONDARY_SLOTS, reserve, candidates[name])
        slots[name] = take
        reserve -= take
    leader = leaders[0]
    slots[leader] = min(candidates[leader], limit - sum(slots.values()))
    return slots



async def search_pdf_pages(
    memory: Any, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` exact citations for distinct pages matching a question.

    Candidates come from the public scoped-Memory search plus its current entry
    listing. Both are then reranked offline with a BM25-style lexical score, so a
    long natural-language question does not lose an evidence page merely because
    the backend's admission floor counted query terms differently.
    """

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-blank string")  # noqa: TRY003
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")  # noqa: TRY003
    if limit == 0:
        return ()

    from powercontext.artifacts import MemoryCitation
    from powercontext.builtin.runtime.models import GetMemoryEntryRequest, SearchMemoryRequest

    page = await memory.search(
        SearchMemoryRequest(query=question, mode="fts", limit=max(limit * 5, 50))
    )
    page_entries = await memory.list()

    records = {record.entry.entry_id: record for record in page_entries.entries}
    # Search reaches entries even when a scope's listing projection is narrower;
    # ranking below still sees the same public exact citations.
    for hit in page.hits:
        if hit.entry_id in records:
            continue
        records[hit.entry_id] = await memory.get(
            GetMemoryEntryRequest(
                citation=MemoryCitation(
                    memory_ref=hit.memory_ref,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
            )
        )

    citations: dict[str, Any] = {}
    texts: dict[str, str] = {}
    for record in records.values():
        if not record.entry.sources:
            continue
        key = record.entry.sources[0].source_id
        citations.setdefault(key, record.citation)
        texts.setdefault(key, record.entry.text)

    if not citations:
        return ()

    weights, sequence = _query(question)
    if not weights:
        return tuple(list(citations.values())[:limit])

    pages = {key: _Page(citations[key], text) for key, text in texts.items()}
    documents: dict[str, list[_Page]] = {}
    for item in pages.values():
        documents.setdefault(item.document_id, []).append(item)
    owner = _named_filings(documents, question)
    basis = pages
    if len(owner) == 1:
        # Length and term frequency are measured inside the named filing, so its own
        # page sizes and vocabulary set the scale instead of an average over every
        # other filing that happens to share the pool. Rivals are still scored.
        only = next(iter(owner))
        pinned = {key: item for key, item in pages.items() if item.document_id == only}
        if pinned:
            basis = pinned
    claim = _document_claim(documents, question, weights)
    total = len(basis)
    average = sum(item.length for item in basis.values()) / total
    document_frequency = {
        term: sum(1 for item in basis.values() if term in item.counts) for term in weights
    }
    idf = {
        term: math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
        for term, frequency in document_frequency.items()
    }
    bigrams = {
        (left, right)
        for left, right in zip(sequence, sequence[1:], strict=False)
        if left in weights and right in weights and left != right
    }
    scores: dict[str, float] = {}
    for key, item in pages.items():
        score = 0.0
        for term, weight in weights.items():
            frequency = item.counts.get(term, 0)
            if not frequency:
                continue
            saturation = frequency * (_K1 + 1.0) / (
                frequency + _K1 * (1.0 - _B + _B * item.length / average)
            )
            score += weight * idf[term] * saturation
        for left, right in bigrams:
            if (left, right) in item.bigrams or (right, left) in item.bigrams:
                score += _BIGRAM_WEIGHT * (idf[left] + idf[right])
        scores[key] = score
    best = max(scores.values(), default=0.0)
    if best > 0.0:
        for key, item in pages.items():
            scores[key] += _section_bonus(item.lead, weights, best)
    ranked: dict[str, list[str]] = {}
    for key, item in pages.items():
        if scores[key] > 0.0:
            ranked.setdefault(item.document_id, []).append(key)
    if not ranked:
        return ()
    for keys in ranked.values():
        keys.sort(key=lambda key: (-scores[key], key))
    order = sorted(ranked, key=lambda name: (-claim.get(name, 0.0), name))
    slots = _document_slots(
        order, {name: len(keys) for name, keys in ranked.items()}, owner, limit
    )
    chosen: list[str] = []
    taken: set[str] = set()
    for name in order:
        for key in ranked[name][: slots[name]]:
            chosen.append(key)
            taken.add(key)
    for name in order:
        if len(chosen) >= limit:
            break
        for key in ranked[name]:
            if key in taken:
                continue
            chosen.append(key)
            taken.add(key)
            if len(chosen) >= limit:
                break
    return tuple(citations[key] for key in chosen[:limit])


def _section_bonus(head: str, weights: dict[str, float], best: float) -> float:
    """Reward a page whose own heading is the requested statement or section.

    The statement the question names is worth most. A question about the figures that
    names no single statement still points at the consolidated statements, so every page
    of that block is treated as a partial answer.
    """

    terms = {term for term, weight in weights.items() if weight >= 1.0}
    figures = terms & (_INCOME_TERMS | _BALANCE_TERMS | _CASH_TERMS)
    statements = _INCOME_HEADS + _BALANCE_HEADS + _CASH_HEADS
    bonus = 0.0
    if terms & _INCOME_TERMS and any(title in head for title in _INCOME_HEADS):
        bonus = _STATEMENT_WEIGHT * best
    if terms & _BALANCE_TERMS and any(title in head for title in _BALANCE_HEADS):
        bonus = max(bonus, _STATEMENT_WEIGHT * best)
    if terms & _CASH_TERMS and any(title in head for title in _CASH_HEADS):
        bonus = max(bonus, _STATEMENT_WEIGHT * best)
    if bonus == 0.0 and figures and any(title in head for title in statements):
        bonus = _FAMILY_WEIGHT * best
    if terms & _SEGMENT_TERMS and any(title in head for title in _SEGMENT_HEADS):
        bonus = max(bonus, _SECTION_WEIGHT * best)
    if (
        terms & _CHANGE_TERMS
        and terms & (_INCOME_TERMS | _BALANCE_TERMS | _CASH_TERMS)
        and "results of operations" in head
    ):
        bonus = max(bonus, _SECTION_WEIGHT * best)
    if "legal" in terms and "item 3. legal proceedings" in head:
        bonus = max(bonus, _SECTION_WEIGHT * best)
    if terms & {"geography", "geographic", "region", "operate"} and "item 1. business" in head:
        bonus = max(bonus, _SECTION_WEIGHT * best)
    return bonus


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
