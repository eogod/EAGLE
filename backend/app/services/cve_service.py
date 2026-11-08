"""
CVE intel shared by the research agent tools and the MCP get_cve_info tool:
unauthenticated NVD 2.0 details and GitHub PoC repo search. Modeled on
KernelVulnAuditP's remote feeds; the NVD politeness interval is read from
WebTools config at call time.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.rate_gate import MinIntervalGate

logger = logging.getLogger(__name__)

_CVE_RE: re.Pattern[str] = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.IGNORECASE)
_NVD_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_GITHUB_URL = "https://api.github.com/search/repositories"
# repo rule: no GitHub auth, keep the plain curl UA
_GITHUB_HEADERS: dict[str, str] = {
    "User-Agent": "curl/7.54.1",
    "Accept": "application/vnd.github+json",
}
# first non-None baseScore wins, newest metric family first
_CVSS_ORDER = ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2")
_MAX_RETRIES = 3
_MAX_REFS = 10
_MAX_POC_REPOS = 10
_MAX_RETRY_WAIT = 30.0
_HTTP_TIMEOUT: float = 30.0
_HTTP_FORBIDDEN = 403
_HTTP_RATE_LIMITED = 429
# 1h TTL, bounded to 100 ids, so repeated CVE lookups cost no extra quotas
_CVE_TTL: float = 3600.0
_CVE_CACHE_MAX = 100


@dataclass(frozen=True)
class PoC:
    """one GitHub proof-of-concept repository found by CVE id"""

    repo: str  # full_name
    url: str  # html_url
    stars: int
    description: str
    language: str


@dataclass(frozen=True)
class CveInfo:
    """everything an agent needs next to a CVE id, plus a partial-failure note"""

    cve_id: str
    description: str
    cwes: list[str]
    cvss_score: float | None
    severity: str
    cvss_vector: str
    references: list[str]
    pocs: list[PoC]
    error: str | None  # non-fatal partial-failure note, e.g. "github unavailable"


def _exc_desc(exc: BaseException) -> str:
    """compact <Type>: <msg> for error notes, mirrors KernelVulnAuditP"""
    message = str(exc).strip()

    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _retry_wait_seconds(headers: Any, default: float = 2.0) -> float:
    """Retry-After seconds (bad/missing -> default exponential caller value is
    handled where used), capped at 30s so retries never stall the agent loop"""
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return default

    try:
        return min(max(0.0, float(str(raw).strip())), _MAX_RETRY_WAIT)
    except ValueError:
        return default


async def _sleep_retry_after(headers: Any, default: float = 2.0) -> None:
    wait = _retry_wait_seconds(headers, default)

    if wait > 0:
        await asyncio.sleep(wait)


def normalize_cve_id(cve_id: str) -> str:
    """uppercase/validate; ValueError on bad format."""
    normalized = cve_id.strip().upper()
    if _CVE_RE.match(normalized) is None:
        raise ValueError(
            f"invalid CVE id {cve_id!r}, expected format CVE-YYYY-NNNN(NN)"
        )

    return normalized


def _parse_nvd(
    record: dict[str, Any] | None,
) -> tuple[str, list[str], float | None, str, str, list[str]]:
    """extract english description, cwes, best cvss and references (max 10)"""
    descriptions = (record or {}).get("descriptions") or []
    description = ""
    for item in descriptions:
        if isinstance(item, dict) and item.get("lang") == "en" and item.get("value"):
            description = str(item["value"])
            break

    if not description and descriptions and isinstance(descriptions[0], dict):
        description = str(descriptions[0].get("value") or "")

    cwes: list[str] = []
    for weakness in (record or {}).get("weaknesses") or []:
        for entry in weakness.get("description") or []:
            value = str((entry or {}).get("value") or "")
            if value.startswith("CWE-") and value not in cwes:
                cwes.append(value)

    score: float | None = None
    severity = ""
    vector = ""
    metrics = (record or {}).get("metrics") or {}
    for family in _CVSS_ORDER:
        for metric in metrics.get(family) or []:
            cvss_data = metric.get("cvssData") or {}
            if cvss_data.get("baseScore") is not None:
                score = float(cvss_data["baseScore"])
                severity = str(cvss_data.get("baseSeverity") or "")
                vector = str(cvss_data.get("vectorString") or "")
                break

        if score is not None:
            break

    references = [
        str(ref.get("url") or "")
        for ref in (record or {}).get("references") or []
        if ref.get("url")
    ][:_MAX_REFS]

    return description, cwes, score, severity, vector, references


async def _github_pocs(cve_id: str) -> list[PoC]:
    """GitHub repo search sorted by stars; one 403/429 retry honoring Retry-After"""
    params = {
        "q": cve_id,
        "sort": "stars",
        "order": "desc",
        "per_page": _MAX_POC_REPOS,
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            _GITHUB_URL, params=params, headers=_GITHUB_HEADERS, timeout=_HTTP_TIMEOUT
        )
        if response.status_code in (_HTTP_FORBIDDEN, _HTTP_RATE_LIMITED):
            await _sleep_retry_after(response.headers)
            response = await client.get(
                _GITHUB_URL,
                params=params,
                headers=_GITHUB_HEADERS,
                timeout=_HTTP_TIMEOUT,
            )

        response.raise_for_status()
        items = ((response.json() or {}).get("items")) or []

    return [
        PoC(
            repo=str(item.get("full_name") or ""),
            url=str(item.get("html_url") or ""),
            stars=int(item.get("stargazers_count") or 0),
            description=str(item.get("description") or ""),
            language=str(item.get("language") or ""),
        )
        for item in items
        if isinstance(item, dict)
    ]


class CveService:
    """stateful CVE lookups: owns the result cache and the NVD rate gate"""

    def __init__(self) -> None:
        # cve id -> (expires, CveInfo); clean results only, failures must retry
        self._cache: dict[str, tuple[float, CveInfo]] = {}
        self._nvd_gate = MinIntervalGate()

    async def _nvd_details(self, cve_id: str) -> dict[str, Any] | None:
        """NVD 2.0 details record, None when NVD has no entry for the id.
        Min-interval gate read from WebTools config at call time; up to three
        429 retries honoring Retry-After or interval*2^n with 0..1s jitter,
        capped at 30s."""
        cfg = get_settings().webtools
        await self._nvd_gate.wait(cfg.nvd_min_interval)

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                _NVD_URL, params={"cveId": cve_id}, timeout=_HTTP_TIMEOUT
            )
            for attempt in range(1, _MAX_RETRIES + 1):
                if response.status_code != _HTTP_RATE_LIMITED:
                    break

                retry_after = response.headers.get("retry-after")
                try:
                    delay = (
                        max(0.0, float(str(retry_after).strip()))
                        if retry_after is not None
                        else 0.0
                    )
                except ValueError:
                    delay = 0.0

                # no header, or a header of zero: back off on the gate interval
                delay = delay or cfg.nvd_min_interval * (2**attempt)
                delay = min(
                    _MAX_RETRY_WAIT,
                    delay + random.uniform(0, 1),  # noqa: S311
                )
                logger.warning(
                    "NVD rate-limited for %s, backing off %.1fs (attempt %d/%d)",
                    cve_id,
                    delay,
                    attempt,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                response = await client.get(
                    _NVD_URL, params={"cveId": cve_id}, timeout=_HTTP_TIMEOUT
                )

            response.raise_for_status()
            payload = (response.json() or {}).get("vulnerabilities") or []

        if not payload:
            return None  # NVD has no entry for the id

        record = (payload[0] or {}).get("cve") or {}

        return record if isinstance(record, dict) and record else None

    def _remember_cve(self, cve_id: str, info: CveInfo) -> None:
        """1h TTL cache, bounded to 100 entries, oldest evicted first"""
        now = time.monotonic()

        for stale in [k for k, v in self._cache.items() if now >= v[0]]:
            del self._cache[stale]

        if cve_id not in self._cache and len(self._cache) >= _CVE_CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))

        self._cache[cve_id] = (now + _CVE_TTL, info)

    async def get_cve_info(self, cve_id: str) -> CveInfo:
        """NVD 2.0 details + GitHub PoC repos; never raises for network errors
        (failure notes land in CveInfo.error), raises ValueError on bad id."""
        normalized = normalize_cve_id(cve_id)

        cached = self._cache.get(normalized)
        if cached is not None:
            expires, info = cached
            if time.monotonic() < expires:
                return info

            del self._cache[normalized]

        errors: list[str] = []

        record: dict[str, Any] | None
        try:
            record = await self._nvd_details(normalized)
        except Exception as exc:
            record = None
            errors.append(f"nvd unavailable: {_exc_desc(exc)}")

        description, cwes, score, severity, vector, references = _parse_nvd(record)

        try:
            pocs = await _github_pocs(normalized)
        except Exception as exc:
            pocs = []
            errors.append(f"github unavailable: {_exc_desc(exc)}")

        info = CveInfo(
            cve_id=normalized,
            description=description,
            cwes=cwes,
            cvss_score=score,
            severity=severity,
            cvss_vector=vector,
            references=references,
            pocs=pocs,
            error="; ".join(errors) if errors else None,
        )
        if info.error is None:
            # failures stay uncached so the next lookup can heal itself
            self._remember_cve(normalized, info)

        return info

    def clear_cve_cache(self) -> None:
        """drop every cached record so tests and retries see a cold cache"""
        self._cache.clear()


def format_cve_info(info: CveInfo) -> str:
    """compact text for an LLM: description, CWEs, CVSS, references, PoC repos."""
    lines = [info.cve_id]

    if info.description:
        lines.append(f"Description: {info.description}")

    if info.cwes:
        lines.append(f"CWEs: {', '.join(info.cwes)}")

    if info.cvss_score is not None:
        severity = f" ({info.severity})" if info.severity else ""
        lines.append(f"CVSS: {info.cvss_score}{severity} [{info.cvss_vector}]")

    if info.references:
        lines.append("References:")
        lines.extend(f"- {url}" for url in info.references)

    if info.pocs:
        lines.append("PoC repositories (GitHub):")
        for poc in info.pocs:
            stars = f", {poc.stars} stars" if poc.stars else ""
            language = f", {poc.language}" if poc.language else ""
            summary = f" — {poc.description}" if poc.description else ""
            lines.append(f"- {poc.url} ({poc.repo}{language}{stars}){summary}")

    if info.error:
        lines.append(f"Error: {info.error}")

    return "\n".join(lines)


# shared by the research tool services and the MCP get_cve_info tool
cve_service = CveService()
