"""Untrusted DFIR document ingestion.

Everything in here treats file bytes as hostile data: the parser never executes,
resolves, fetches or interprets document content. Text is only ever evidence to be
quoted later, never an instruction. Caps are enforced before any LLM call.
"""

import io
import json
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from pypdf import PdfReader

TEXT_SUFFIXES = {".txt", ".text", ".md", ".markdown", ".log", ".json", ".csv"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES

MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}

# extractor identity travels with every evidence pointer
EXTRACTOR_VERSION = "eagle-doc/1"
MAX_PDF_PAGES = 500
# short quotes match everywhere ("root", "admin"), so they prove nothing
MIN_QUOTE_CHARS = 8


class DocumentError(ValueError):
    """upload cannot be parsed; the message is safe to show to the operator"""


@dataclass(frozen=True)
class Segment:
    segment_id: str
    page: int | None
    char_start: int
    char_end: int
    text: str


@dataclass(frozen=True)
class ParsedDocument:
    pages: list[tuple[int | None, str]]
    page_count: int
    char_count: int
    truncated: bool
    extractor: str


def suffix_of(filename: str) -> str:
    return PurePath(filename or "").suffix.lower()


def mime_of(filename: str) -> str:
    suffix = suffix_of(filename)
    return MIME_BY_SUFFIX.get(suffix, "text/plain")


def parse_bytes(
    filename: str,
    data: bytes,
    *,
    max_chars: int,
    max_pages: int = MAX_PDF_PAGES,
) -> ParsedDocument:
    """parse an upload into (page, text) pairs with a hard character cap."""
    suffix = suffix_of(filename)
    if suffix in PDF_SUFFIXES:
        pages = _parse_pdf(data, max_pages=max_pages)
        extractor = "pypdf"
    elif suffix in TEXT_SUFFIXES:
        pages = [(None, data.decode("utf-8", errors="replace"))]
        extractor = "utf8"
    else:
        raise DocumentError(
            "unsupported file type; upload PDF, TXT, MD, LOG, CSV or JSON"
        )

    total = 0
    truncated = False
    capped: list[tuple[int | None, str]] = []
    for page, raw_text in pages:
        if total >= max_chars:
            truncated = True
            break
        remaining = max_chars - total
        clipped = raw_text[:remaining]
        if len(clipped) < len(raw_text):
            truncated = True
        total += len(clipped)
        capped.append((page, clipped))

    if not any(text.strip() for _, text in capped):
        raise DocumentError("document contains no extractable text")

    page_count = len(pages) if suffix in PDF_SUFFIXES else 1
    return ParsedDocument(
        pages=capped,
        page_count=page_count,
        char_count=total,
        truncated=truncated,
        extractor=extractor,
    )


def _parse_pdf(data: bytes, *, max_pages: int) -> list[tuple[int | None, str]]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # pypdf raises a wide range of parser errors
        raise DocumentError(f"cannot read pdf: {exc}") from exc

    if reader.is_encrypted:
        raise DocumentError("encrypted pdf is not supported")

    pages: list[tuple[int | None, str]] = []
    for index, page in enumerate(reader.pages):
        if index >= max_pages:
            break
        try:
            text = page.extract_text() or ""
        except Exception:
            # a single broken page must not lose the rest of the report
            text = ""
        pages.append((index + 1, text))

    if not pages:
        raise DocumentError("pdf has no pages")

    return pages


def segment_pages(
    pages: list[tuple[int | None, str]], *, max_chars: int
) -> list[Segment]:
    """split pages into stable windows whose ids double as evidence pointers."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    segments: list[Segment] = []
    for page, text in pages:
        label = f"p{page}" if page is not None else "p0"
        start = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            window = text[start:end]
            if window.strip():
                segments.append(
                    Segment(
                        segment_id=f"{label}:c{start}-{end}",
                        page=page,
                        char_start=start,
                        char_end=end,
                        text=window,
                    )
                )
            if end >= len(text):
                break
            start = end

    return segments


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def quote_in_text(quote: str, text: str) -> bool:
    """evidence must be a verbatim span, whitespace-insensitive."""
    normalized_quote = normalize_whitespace(quote)
    if len(normalized_quote) < MIN_QUOTE_CHARS:
        return False
    return normalized_quote in normalize_whitespace(text)


def parse_json_object(raw: str) -> dict[str, Any]:
    """LLMs like code fences; extract the first JSON object and parse it."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]

    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in model output")
    value = json.loads(text[start : end + 1])

    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")

    return value
