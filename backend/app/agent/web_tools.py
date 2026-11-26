"""Web research tools: DuckDuckGo html search and guarded page fetching.

Everything returned here is untrusted text; callers treat it as evidence, never
as instructions. Fetch targets are resolved and vetted before every connection,
bodies are byte-capped, and budgets plus politeness intervals come from the
WebTools config.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify

from app.core.config import WebTools, get_settings
from app.core.rate_gate import MinIntervalGate

try:
    from readability import Document
except ImportError:  # readability-lxml caps at <3.15; full-soup extraction still works
    Document = None

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://html.duckduckgo.com/html"
_SEARCH_TIMEOUT: float = 30.0
_SEARCH_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Not;A=Brand";v="99", "Google Chrome";v="139", "Chromium";v="139"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

_FETCH_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_MAX_REDIRECTS = 10
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_READ_CHUNK = 65_536
_STRIP_TAGS = ("script", "style", "noscript")

# status codes the search and fetch paths branch on
_HTTP_OK = 200
_HTTP_ACCEPTED = 202
_HTTP_FORBIDDEN = 403
_HTTP_RATE_LIMITED = 429


class BlockedURLError(ValueError):
    """Fetch target is not an allowed public http(s) destination (SSRF guard)."""


@dataclass(frozen=True)
class SearchHit:
    """One organic DuckDuckGo result."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class FetchResult:
    """One fetched page: the requested slice plus the length of the full text."""

    url: str  # final url after redirects, normalized
    content: str  # markdown or plain text, slice [offset : offset + max_length]
    content_type: str  # lowercase base type, e.g. "text/html"
    total_length: int  # length of the full extracted text, before slicing
    truncated: bool  # body byte cap, or offset + max_length < total_length


_search_gate = MinIntervalGate()
# normalized url -> (expires, final url, full markdown, content type, byte-capped)
_cache: dict[str, tuple[float, str, str, str, bool]] = {}


def clear_web_cache() -> None:
    """Drop every fetched page, so tests and retries see a cold cache."""
    _cache.clear()


def _retry_wait_seconds(headers: Any, default: float) -> float:
    """Parse Retry-After seconds, falling back to the default and capped at 30s so
    a retry never stalls the agent tool loop."""
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return min(default, 30.0)

    try:
        wait = max(0.0, float(str(raw).strip()))
    except ValueError:
        wait = default

    return min(wait, 30.0)


async def _sleep_retry_after(headers: Any, default: float = 2.0) -> None:
    """Sleep for the Retry-After the server asked for, if it asked for one."""
    wait = _retry_wait_seconds(headers, default)
    if wait > 0:
        await asyncio.sleep(wait)


def _is_search_block(status: int, body: str) -> bool:
    """DDG answers blocked clients with 202, 403 or an empty 200."""
    return status in (_HTTP_ACCEPTED, _HTTP_FORBIDDEN) or (
        status == _HTTP_OK and not (body or "").strip()
    )


async def _search_post(query: str) -> tuple[int, str]:
    """POST the html.duckduckgo.com form, with one 429 retry honoring Retry-After."""
    form = {"q": query, "b": "", "kl": ""}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            _SEARCH_URL, data=form, headers=_SEARCH_HEADERS, timeout=_SEARCH_TIMEOUT
        )
        if response.status_code == _HTTP_RATE_LIMITED:
            await _sleep_retry_after(response.headers)
            response = await client.post(
                _SEARCH_URL,
                data=form,
                headers=_SEARCH_HEADERS,
                timeout=_SEARCH_TIMEOUT,
            )

        response.raise_for_status()

        return response.status_code, response.text


def _parse_hits(html: str, cap: int) -> list[SearchHit]:
    """Pick organic results: title, redirect-unwrapped url and snippet."""
    soup = BeautifulSoup(html or "", "html.parser")
    hits: list[SearchHit] = []

    for el in soup.select(".result"):
        title = el.select_one(".result__title")
        if title is None:
            continue

        link = title.find("a")
        if link is None:
            continue

        href = str(link.get("href", "") or "")
        if "y.js" in href:
            continue  # DDG ad unit, not an organic hit

        # organic links leave through the ddg redirector; keep the real target
        if href.startswith("//duckduckgo.com/l/?uddg="):
            href = urllib.parse.unquote(
                href.split("uddg=")[1].split("&", maxsplit=1)[0]
            )

        snippet = el.select_one(".result__snippet")
        hits.append(
            SearchHit(
                title=link.get_text(strip=True),
                url=href,
                snippet=snippet.get_text(strip=True) if snippet else "",
            )
        )

        if len(hits) >= cap:
            break

    return hits


async def web_search(query: str, limit: int | None = None) -> list[SearchHit]:
    """DuckDuckGo html search; returns [] when DDG blocks or finds nothing."""
    cfg = get_settings().webtools
    cap = limit if limit is not None else cfg.search_results

    await _search_gate.wait(cfg.search_min_interval)

    try:
        status, body = await _search_post(query)
        if _is_search_block(status, body):
            # DDG fingerprints httpx's TLS; one fresh-client retry usually clears it
            status, body = await _search_post(query)
            if _is_search_block(status, body):
                logger.warning("DDG block signal for query %r (HTTP %s)", query, status)
                return []

        return _parse_hits(body, cap)
    except Exception as exc:
        logger.warning(
            "web_search failed for %r: %s: %s", query, type(exc).__name__, exc
        )
        return []


def _normalize_url(raw: str) -> str:
    """Lowercase scheme and host, upgrade http to https, strip default ports."""
    parts = urllib.parse.urlsplit(raw.strip())

    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"
    elif scheme != "https":
        raise ValueError(f"invalid url {raw!r}: only http/https are supported")

    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"invalid url {raw!r}: missing host")

    try:
        port = parts.port
    except ValueError as e:
        raise ValueError(f"invalid url {raw!r}: bad port") from e

    netloc = host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = f"{host}:{port}"

    # userinfo is dropped on purpose: credentials pasted into a url would end up
    # in the cache key and in the text handed back to the model
    return urllib.parse.urlunsplit((scheme, netloc, parts.path, parts.query, ""))


async def _guard_public_url(url: str) -> None:
    """Reject non-public fetch targets, both the initial url and every redirect hop.

    Each resolved address (IPv4-mapped IPv6 unwrapped) must be global; is_global
    is the catch-all, and the explicit flags cover the few ranges it lets through.
    """
    cfg = get_settings().webtools
    if cfg.allow_private_urls:
        return

    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise BlockedURLError(
            f"unsupported url scheme '{parsed.scheme}://' (only http/https allowed)"
        )

    host = parsed.hostname or ""
    if not host:
        raise BlockedURLError("url has no host")

    lowered = host.lower().strip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise BlockedURLError(f"refusing to fetch loopback host '{host}'")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as e:
        raise BlockedURLError(f"invalid port in url {url!r}: {e}") from e

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise BlockedURLError(f"could not resolve host '{host}': {e}") from e

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise BlockedURLError(
                f"refusing to fetch '{host}' — resolves to non-public address {ip}"
            )


async def _one_get(
    client: httpx.AsyncClient, url: str, cfg: WebTools
) -> tuple[bytes, str, int, str, bool]:
    """One streamed GET with a byte-capped read; a 429 is retried once.

    Returns (data, content_type, status, redirect location, capped).
    """
    for _attempt in range(2):  # one 429 retry
        async with client.stream(
            "GET", url, headers={"User-Agent": _FETCH_UA}, timeout=cfg.fetch_timeout
        ) as response:
            status = response.status_code
            content_type = (
                response.headers.get("content-type", "").split(";")[0].strip().lower()
            )

            if status == _HTTP_RATE_LIMITED:
                await _sleep_retry_after(response.headers)
                continue

            if status in _REDIRECT_STATUSES:
                location = response.headers.get("location", "")
                return b"", content_type, status, location, False

            response.raise_for_status()

            chunks: list[bytes] = []
            received = 0
            capped = False
            async for chunk in response.aiter_bytes(_READ_CHUNK):
                remaining = max(cfg.max_response_bytes - received, 0)
                chunks.append(chunk[:remaining])
                received += min(len(chunk), remaining)
                if received >= cfg.max_response_bytes:
                    capped = True
                    break

            data = b"".join(chunks)
            if capped:
                logger.warning(
                    "response from %s exceeded %d bytes, text truncated",
                    url,
                    cfg.max_response_bytes,
                )

            if not data:
                raise ValueError(f"no readable content from {url}")

            return data, content_type, status, "", capped

    raise httpx.HTTPError(f"still rate-limited (429) after retry: {url}")


async def _guarded_get(url: str, cfg: WebTools) -> tuple[bytes, str, str, bool]:
    """Follow a manual redirect chain, guarding every hop before it is requested.

    Returns (data, content_type, final normalized url, capped).
    """
    async with httpx.AsyncClient(follow_redirects=False) as client:
        current = url
        for _hop in range(_MAX_REDIRECTS + 1):
            await _guard_public_url(current)
            data, content_type, status, location, capped = await _one_get(
                client, current, cfg
            )

            if status in _REDIRECT_STATUSES and location:
                current = str(httpx.URL(current).join(location))
                continue

            return data, content_type, _normalize_url(current), capped

        raise httpx.HTTPError(f"too many redirects (>{_MAX_REDIRECTS}): {url}")


def _to_text(data: bytes, content_type: str) -> str:
    """Decode the body and pull clean markdown out of html; text and json pass through."""
    if not (
        content_type.startswith("text/")
        or "json" in content_type
        or "xml" in content_type
        or "rss" in content_type
        or "atom" in content_type
    ):
        raise ValueError(
            f"binary content '{content_type or 'unknown'}', fetch not supported"
        )

    raw = data.decode("utf-8", errors="replace")
    if "html" not in content_type:
        return raw

    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()

    page = str(soup)
    # the soup tree is cyclic and `page` is all that survives, so drop it now
    soup.decompose()
    extracted = ""
    if Document is not None:
        try:
            extracted = Document(page).summary(html=True)
        except Exception as exc:
            # readability is fragile on odd pages; the full soup is the fallback
            logger.debug(
                "readability failed on %d chars, using full soup: %s", len(page), exc
            )

    # readability sometimes keeps only nav boilerplate; trust a summary only when
    # it holds a tenth of the page
    if len(extracted) >= 0.1 * max(len(page), 1):
        page = extracted

    return markdownify(page, heading_style="ATX", bullets="-").strip()


def _cache_get(key: str, cfg: WebTools) -> tuple[str, str, str, bool] | None:
    """Return (final url, full markdown, content type, capped), or None when the
    entry is missing or expired or caching is off."""
    if cfg.cache_ttl <= 0 or cfg.cache_entries <= 0:
        return None

    entry = _cache.get(key)
    if entry is None:
        return None

    if time.monotonic() >= entry[0]:
        del _cache[key]
        return None

    return entry[1:]


def _cache_put(key: str, value: tuple[str, str, str, bool], cfg: WebTools) -> None:
    """Store under a normalized url in a bounded TTL cache, oldest entry first out."""
    if cfg.cache_ttl <= 0 or cfg.cache_entries <= 0:
        return

    now = time.monotonic()

    for stale in [k for k, v in _cache.items() if now >= v[0]]:
        del _cache[stale]

    if key not in _cache and len(_cache) >= cfg.cache_entries:
        _cache.pop(next(iter(_cache)))

    _cache[key] = (now + cfg.cache_ttl, *value)


async def fetch_url(
    url: str, offset: int = 0, max_length: int | None = None
) -> FetchResult:
    """Fetch a public http(s) page, extract clean markdown and paginate it.

    Raises on normalized-url errors and the SSRF guard (both ValueError
    subclasses) as well as on transport and binary-content errors; a cache hit
    skips the network.
    """
    cfg = get_settings().webtools
    cap = cfg.fetch_max_chars if max_length is None else max_length
    normalized = _normalize_url(url)

    hit = _cache_get(normalized, cfg)
    if hit is None:
        data, content_type, final, capped = await _guarded_get(normalized, cfg)
        # parsing html to markdown is cpu work; a big page would stall the loop
        text = await asyncio.to_thread(_to_text, data, content_type)
        # key = requested url (what the next lookup uses), value keeps the
        # post-redirect url so callers still see where they landed
        _cache_put(normalized, (final, text, content_type, capped), cfg)
        result_url = final
    else:
        result_url, text, content_type, capped = hit

    end = offset + cap

    return FetchResult(
        url=result_url,
        content=text[offset:end],
        content_type=content_type,
        total_length=len(text),
        truncated=capped or end < len(text),
    )
