"""Web tool checks over a mocked transport.

DDG parsing and redirect unwrapping, block signals, the SSRF guard on every hop,
the byte cap, caching and pagination; no real DNS or network.
"""

import socket

import httpx
import pytest

from app.agent import web_tools as wts
from app.core.config import get_settings

# one loop per session: the fetch cache and the politeness gate are shared state
pytestmark = pytest.mark.asyncio(loop_scope="session")

SEARCH_HTML = """
<div class="result">
  <h2 class="result__title"><a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&vct=2">Title A</a></h2>
  <a class="result__snippet">snippet a</a>
</div>
<div class="result">
  <h2 class="result__title"><a href="https://ads.example/y.js">Ad</a></h2>
  <a class="result__snippet">ad snippet</a>
</div>
<div class="result">
  <h2 class="result__title"><a href="https://example.com/b">Title B</a></h2>
  <a class="result__snippet">snippet b</a>
</div>
"""

PAGE_HTML = (
    "<html><head><script>evil()</script><style>.x{color:red}</style></head>"
    "<body><h1>Fetch Title</h1><p>Paragraph one with words.</p></body></html>"
)


@pytest.fixture(autouse=True)
def _cold_env(monkeypatch):
    """Cold cache, no politeness wait, offline DNS answering a public ip."""
    wts.clear_web_cache()
    cfg = get_settings().webtools
    monkeypatch.setattr(cfg, "search_min_interval", 0.0)
    monkeypatch.setattr(cfg, "cache_ttl", 60)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *_a, **_k: [
            (0, 0, 0, "", ("93.184.216.34", port)),
        ],
    )
    yield
    wts.clear_web_cache()


def _use_transport(monkeypatch, handler) -> None:
    """Put an httpx MockTransport on the module's AsyncClient, llm_stream style."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.agent.web_tools.httpx.AsyncClient",
        lambda **kw: original(**{**kw, "transport": transport}),
    )


async def test_web_search_parses_unwraps_and_skips_ads(monkeypatch):
    _use_transport(monkeypatch, lambda _req: httpx.Response(200, content=SEARCH_HTML))
    hit = await wts.web_search("query", limit=5)

    assert [h.url for h in hit] == ["https://example.com/a", "https://example.com/b"]
    assert hit[0].title == "Title A"
    assert hit[0].snippet == "snippet a"


async def test_web_search_block_signal_returns_empty(monkeypatch):
    # both attempts (fresh client on retry) look like a fingerprint block
    _use_transport(monkeypatch, lambda _req: httpx.Response(202, content=""))

    assert await wts.web_search("blocked") == []


async def test_web_search_empty_200_body_is_block(monkeypatch):
    _use_transport(monkeypatch, lambda _req: httpx.Response(200, content=""))

    assert await wts.web_search("empty") == []


async def test_web_search_429_retries_then_results(monkeypatch):
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(_request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, content=SEARCH_HTML)

    _use_transport(monkeypatch, handler)

    hits = await wts.web_search("late")
    assert len(calls) == 2  # single 429 retry back to back
    assert hits and hits[0].title == "Title A"


async def test_web_search_gives_up_on_transport_error(monkeypatch):
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    _use_transport(monkeypatch, handler)

    assert await wts.web_search("dead") == []


async def test_fetch_html_to_markdown_strips_chrome(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            200, content=PAGE_HTML, headers={"Content-Type": "text/html; charset=utf-8"}
        ),
    )

    result = await wts.fetch_url("http://example.com/page")

    assert "Fetch Title" in result.content
    assert "evil" not in result.content  # script stripped
    assert "color:red" not in result.content  # style stripped
    assert result.url == "https://example.com/page"  # normalised/upgraded
    assert result.content_type == "text/html"
    assert result.truncated is False
    assert result.total_length == len(result.content)


async def test_fetch_rejects_private_and_loopback(monkeypatch):
    # resolve every host to itself: literal private ips classify as non-global
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *_a, **_k: [(0, 0, 0, "", (host, port))],
    )

    for bad in ("https://10.1.2.3/x", "https://localhost/x", "https://127.0.0.1/x"):
        with pytest.raises(ValueError):
            await wts.fetch_url(bad)


async def test_fetch_redirect_reguards_each_hop(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            302, headers={"Location": "http://127.0.0.1/steal"}
        ),
    )
    # first hop public, redirect target resolves loopback -> guard must fire
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *_a, **_k: [
            (
                0,
                0,
                0,
                "",
                ("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34", port),
            )
        ],
    )

    with pytest.raises(ValueError):
        await wts.fetch_url("https://example.com/start")


async def test_fetch_binary_content_rejected(monkeypatch):
    _use_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            200, content=b"\x89PNG", headers={"Content-Type": "image/png"}
        ),
    )

    with pytest.raises(ValueError, match="binary content"):
        await wts.fetch_url("https://example.com/img")


async def test_fetch_byte_cap_marks_truncated(monkeypatch):
    monkeypatch.setattr(get_settings().webtools, "max_response_bytes", 100)
    _use_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            200, content=b"x" * 1000, headers={"Content-Type": "text/plain"}
        ),
    )

    result = await wts.fetch_url("https://example.com/big")

    assert result.total_length == 100  # capped, not the full 1000
    assert result.truncated is True


async def test_fetch_cache_hit_skips_network(monkeypatch):
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(_request)
        return httpx.Response(
            200, content=b"plain", headers={"Content-Type": "text/plain"}
        )

    _use_transport(monkeypatch, handler)

    first = await wts.fetch_url("https://example.com/once")
    second = await wts.fetch_url("https://example.com/once")

    assert len(calls) == 1
    assert first == second


async def test_fetch_pagination_slice_and_truncated_flag(monkeypatch):
    monkeypatch.setattr(get_settings().webtools, "fetch_max_chars", 10)
    _use_transport(
        monkeypatch,
        lambda _req: httpx.Response(
            200, content=b"0123456789abcdefghij", headers={"Content-Type": "text/plain"}
        ),
    )

    head = await wts.fetch_url("https://example.com/doc")
    tail = await wts.fetch_url("https://example.com/doc", offset=18)

    assert head.content == "0123456789"
    assert head.truncated is True
    assert tail.content == "ij"
    assert tail.truncated is False
    assert tail.total_length == 20
    assert tail.url == "https://example.com/doc"


async def test_fetch_invalid_url_raises():
    for bad in ("ftp://example.com/f", "not-a-url", "https:///no-host"):
        with pytest.raises(ValueError):
            await wts.fetch_url(bad)


async def test_normalize_url_drops_embedded_credentials():
    """Userinfo must not reach the cache key or the text the model sees."""
    assert (
        wts._normalize_url("https://user:pass@example.com/a?b=1")
        == "https://example.com/a?b=1"
    )


async def test_fetch_cache_hits_after_redirect(monkeypatch):
    """The cache is keyed by the requested url, so a redirecting url hits on repeat."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "https://example.com/new"})
        return httpx.Response(
            200, content=PAGE_HTML, headers={"Content-Type": "text/html"}
        )

    _use_transport(monkeypatch, handler)

    first = await wts.fetch_url("https://example.com/old")
    second = await wts.fetch_url("https://example.com/old")

    assert first.url == "https://example.com/new"
    assert second.url == "https://example.com/new"
    assert len(calls) == 2  # second fetch served from cache, no new requests


async def test_html_extraction_without_readability(monkeypatch):
    """Python 3.15+ has no readability-lxml; full-soup markdown still works."""
    monkeypatch.setattr(wts, "Document", None)

    text = wts._to_text(PAGE_HTML.encode(), "text/html")

    assert "Fetch Title" in text
    assert "Paragraph one with words." in text
