"""Tool schemas and the executor behind the research agent's websearch/webfetch/get_cve_info."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.web_tools import (
    FetchResult,
    SearchHit,
    fetch_url,
    web_search,
)
from app.core.config import get_settings
from app.services.cve import (
    CveInfo,
    cve_service,
    format_cve_info,
    normalize_cve_id,
)

_SUMMARY_CHARS = 300
_SEARCH_RESULTS_CAP = 20
# per-tool result caps so one call can never flood a small context window
_SEARCH_MAX_CHARS = 3_000
_CVE_MAX_CHARS = 3_000


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "websearch",
            "description": (
                "Search the web (DuckDuckGo). Use for technique details, exploit "
                "prerequisites, exact commands/flags, vendor docs (e.g. HackTricks). "
                "returns snippets; follow with webfetch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "search keywords: technique, tool or version",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "result count 1-20, keep small",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "webfetch",
            "description": (
                "Fetch one public http(s) page as clean markdown, paginated with "
                "offset. Use after websearch on the most relevant URL"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "public http(s) URL, no internal addresses",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "character offset for the next page after truncation",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "chars of the page to return, omit for default",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cve_info",
            "description": (
                "Get a CVE description, CWE ids, CVSS, references and GitHub PoC "
                "repositories. Call for every CVE-YYYY-NNN in the findings"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cve_id": {
                        "type": "string",
                        "description": "CVE id like CVE-2024-12345",
                    },
                },
                "required": ["cve_id"],
            },
        },
    },
]


@dataclass(frozen=True)
class ToolLimits:
    search_results: int = 5
    fetch_max_chars: int = 8_000


@dataclass
class ToolServices:
    """not frozen; created once, services are plain callables"""

    search: Callable[[str, int], Awaitable[list[SearchHit]]]
    fetch: Callable[[str, int, int], Awaitable[FetchResult]]
    cve: Callable[[str], Awaitable[CveInfo]]


def default_services() -> ToolServices:
    """wires the real service functions"""
    return ToolServices(
        search=web_search, fetch=fetch_url, cve=cve_service.get_cve_info
    )


def limits_from_cfg() -> ToolLimits:
    cfg = get_settings().webtools
    return ToolLimits(
        search_results=cfg.search_results,
        fetch_max_chars=cfg.fetch_max_chars,
    )


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


async def execute_tool(
    name: str,
    arguments: dict[str, Any],
    services: ToolServices,
    limits: ToolLimits,
) -> str:
    """run one tool call; ALWAYS returns text (never raises): ERROR: <what> <hint> on failure"""
    if name == "websearch":
        return await _websearch(arguments, services, limits)
    if name == "webfetch":
        return await _webfetch(arguments, services, limits)
    if name == "get_cve_info":
        return await _cve_info(arguments, services)
    return f"ERROR: unknown tool {name}."


async def _websearch(
    arguments: dict[str, Any], services: ToolServices, limits: ToolLimits
) -> str:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "ERROR: websearch needs a query string."
    max_results = min(
        max(_to_int(arguments.get("max_results"), limits.search_results), 1),
        _SEARCH_RESULTS_CAP,
    )
    try:
        hits = await services.search(query, max_results)
    except Exception as exc:
        return f"ERROR: websearch failed: {exc}. Try rephrasing or continue."
    if not hits:
        return (
            "No results. DuckDuckGo may be blocking automated requests; "
            "try rephrasing or continue without it."
        )
    lines = [f"Found {len(hits)} search results:"]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit.title}")
        lines.append(f"   URL: {hit.url}")
        lines.append(f"   Summary: {hit.snippet[:_SUMMARY_CHARS]}")
    return "\n".join(lines)[:_SEARCH_MAX_CHARS]


async def _webfetch(
    arguments: dict[str, Any], services: ToolServices, limits: ToolLimits
) -> str:
    url = str(arguments.get("url") or "").strip()
    if not url:
        return "ERROR: webfetch needs a url string."
    offset = max(_to_int(arguments.get("offset"), 0), 0)
    # one page never exceeds the configured cap; offset is how the model pages on
    max_length = min(
        _to_int(arguments.get("max_length"), limits.fetch_max_chars),
        limits.fetch_max_chars,
    )
    if max_length <= 0:
        max_length = limits.fetch_max_chars
    try:
        result = await services.fetch(url, offset, max_length)
    except Exception as exc:
        return f"ERROR: fetch failed: {exc}. Try a different URL or continue."
    lines = [f"URL: {result.url}", f"Length: {result.total_length} chars"]
    if result.truncated:
        lines.append(
            f"Truncated. Call again with offset={offset + len(result.content)} for more."
        )
    lines.append("---")
    lines.append(result.content)
    return "\n".join(lines)


async def _cve_info(arguments: dict[str, Any], services: ToolServices) -> str:
    cve_id = str(arguments.get("cve_id") or "").strip()
    try:
        cve_id = normalize_cve_id(cve_id)
    except ValueError:
        return "ERROR: get_cve_info needs a CVE id like CVE-2024-12345."
    try:
        info = await services.cve(cve_id)
    except Exception as exc:
        return f"ERROR: cve lookup failed: {exc}. Continue without it."
    text = format_cve_info(info)
    if info.error:
        text = f"Note: {info.error}.\n{text}"
    return text[:_CVE_MAX_CHARS]
