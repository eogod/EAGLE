"""Parsing is the trust boundary: every case here feeds hostile or broken input."""

import json

import pytest

from app.agent.parsing import (
    MIN_QUOTE_CHARS,
    DocumentError,
    mime_of,
    normalize_whitespace,
    parse_bytes,
    parse_json_object,
    quote_in_text,
    segment_pages,
    suffix_of,
)


def test_suffix_and_mime():
    assert suffix_of("Report.PDF") == ".pdf"
    assert suffix_of("") == ""
    assert mime_of("a.pdf") == "application/pdf"
    assert mime_of("a.json") == "application/json"
    assert mime_of("a.md") == "text/markdown"
    assert mime_of("a.bin") == "text/plain"


def test_parse_text_document():
    parsed = parse_bytes("notes.md", b"# host\npowershell -enc AAA", max_chars=1000)
    assert parsed.extractor == "utf8"
    assert parsed.page_count == 1
    assert parsed.char_count == len("# host\npowershell -enc AAA")
    assert parsed.truncated is False
    assert parsed.pages[0][0] is None


def test_parse_utf8_replacement_and_cap():
    parsed = parse_bytes("x.txt", b"a" * 50, max_chars=10)
    assert parsed.truncated is True
    assert parsed.char_count == 10
    assert parsed.pages == [(None, "a" * 10)]


def test_parse_rejects_unsupported_suffix():
    with pytest.raises(DocumentError):
        parse_bytes("evil.exe", b"MZ", max_chars=100)


def test_parse_rejects_blank_document():
    with pytest.raises(DocumentError):
        parse_bytes("blank.txt", b"   \n\t", max_chars=100)


def test_parse_pdf_invalid_bytes():
    with pytest.raises(DocumentError):
        parse_bytes("broken.pdf", b"not a pdf", max_chars=100)


class _FakePage:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        if self.text == "boom":
            raise RuntimeError("broken page")
        return self.text


class _FakeReader:
    is_encrypted = False

    def __init__(self, _bytes) -> None:
        self.pages = [_FakePage("page one"), _FakePage("boom"), _FakePage("page three")]


def test_parse_pdf_success_and_broken_page(monkeypatch):
    monkeypatch.setattr("app.agent.parsing.PdfReader", _FakeReader)
    parsed = parse_bytes("r.pdf", b"%PDF-1.4", max_chars=1000)
    assert parsed.extractor == "pypdf"
    assert parsed.page_count == 3
    assert parsed.pages == [(1, "page one"), (2, ""), (3, "page three")]


def test_parse_pdf_encrypted(monkeypatch):
    class Encrypted(_FakeReader):
        is_encrypted = True

    monkeypatch.setattr("app.agent.parsing.PdfReader", Encrypted)
    with pytest.raises(DocumentError, match="encrypted"):
        parse_bytes("r.pdf", b"%PDF-1.4", max_chars=1000)


def test_parse_pdf_no_pages(monkeypatch):
    class Empty:
        is_encrypted = False

        def __init__(self, _bytes) -> None:
            self.pages = []

    monkeypatch.setattr("app.agent.parsing.PdfReader", Empty)
    with pytest.raises(DocumentError, match="no pages"):
        parse_bytes("r.pdf", b"%PDF-1.4", max_chars=1000)


def test_parse_pdf_page_cap(monkeypatch):
    monkeypatch.setattr("app.agent.parsing.PdfReader", _FakeReader)
    parsed = parse_bytes("r.pdf", b"%PDF-1.4", max_chars=1000, max_pages=2)
    assert parsed.page_count == 2  # page_count reflects what was actually parsed
    assert len(parsed.pages) == 2


def test_segment_pages_stable_ids_and_windows():
    segments = segment_pages([(2, "abcdefghij")], max_chars=4)
    assert [s.segment_id for s in segments] == [
        "p2:c0-4",
        "p2:c4-8",
        "p2:c8-10",
    ]
    assert segments[0].page == 2
    assert segments[-1].text == "ij"


def test_segment_pages_skips_blank_windows_and_requires_positive_chars():
    assert segment_pages([(None, "   ")], max_chars=4) == []
    with pytest.raises(ValueError):
        segment_pages([(1, "x")], max_chars=0)


def test_quote_matching_is_whitespace_insensitive_and_min_length():
    text = "Host  WIN-01   beaconed  to 26.26"
    assert quote_in_text("WIN-01 beaconed", text) is True
    assert quote_in_text("WIN-01   beaconed", text) is True
    assert quote_in_text("nothing here", text) is False
    assert quote_in_text("a" * (MIN_QUOTE_CHARS - 1), text) is False
    assert normalize_whitespace(" a  b\n c ") == "a b c"


def test_parse_json_object_handles_fences_and_prose():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('here you go: {"a": {"b": 2}} thanks') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        parse_json_object("no json here")
    with pytest.raises(ValueError):
        parse_json_object("[1, 2, 3]")
    assert json.dumps(parse_json_object('{"k": "v"}')) == '{"k": "v"}'
