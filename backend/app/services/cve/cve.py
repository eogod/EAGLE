"""CVE records, NVD parsing and GitHub PoC search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.cve.nvd import HTTP_TIMEOUT, sleep_retry_after

_CVE_RE: re.Pattern[str] = re.compile(r"^CVE-\d{4}-\d{4,7}$", re.IGNORECASE)
_CVSS_ORDER = ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2")
_MAX_REFS = 10
_MAX_POC_REPOS = 10
_HTTP_FORBIDDEN = 403
_HTTP_RATE_LIMITED = 429
_GITHUB_URL = "https://api.github.com/search/repositories"
# repo rule: no GitHub auth, keep the plain curl UA
_GITHUB_HEADERS: dict[str, str] = {
    "User-Agent": "curl/7.54.1",
    "Accept": "application/vnd.github+json",
}


@dataclass(frozen=True)
class PoC:
    """one GitHub proof-of-concept repository found by CVE id"""

    repo: str
    url: str
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
    error: str | None


@dataclass(frozen=True)
class CveFacts:
    """description, CWE/CVSS fields and references parsed from one NVD record"""

    description: str
    cwes: list[str]
    cvss_score: float | None
    severity: str
    cvss_vector: str
    references: list[str]


@dataclass(frozen=True)
class CveHit:
    """light CVE row; references and PoCs stay in get_cve_info"""

    cve_id: str
    description: str
    cvss_score: float | None
    severity: str
    published: str


def exc_desc(exc: BaseException) -> str:
    """compact <Type>: <msg> for error notes"""
    message = str(exc).strip()

    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def normalize_cve_id(cve_id: str) -> str:
    """uppercase/validate; ValueError on bad format."""
    normalized = cve_id.strip().upper()
    if _CVE_RE.match(normalized) is None:
        raise ValueError(
            f"invalid CVE id {cve_id!r}, expected format CVE-YYYY-NNNN(NN)"
        )

    return normalized


def parse_nvd(record: dict[str, Any] | None) -> CveFacts:
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

    return CveFacts(
        description=description,
        cwes=cwes,
        cvss_score=score,
        severity=severity,
        cvss_vector=vector,
        references=references,
    )


def cve_hit(record: dict[str, Any]) -> CveHit | None:
    """light hit from one cve record, None when the id is missing"""
    cve_id = str(record.get("id") or "")
    if not cve_id:
        return None

    facts = parse_nvd(record)
    return CveHit(
        cve_id=cve_id,
        description=facts.description,
        cvss_score=facts.cvss_score,
        severity=facts.severity,
        published=str(record.get("published") or ""),
    )


def sort_cve_hits(hits: list[CveHit]) -> list[CveHit]:
    """highest CVSS then newest first"""
    return sorted(
        hits,
        key=lambda hit: (hit.cvss_score or 0.0, hit.published),
        reverse=True,
    )


def parse_cve_hits(payload: dict[str, Any]) -> list[CveHit]:
    """parse cves/2.0 rows into sorted light hits"""
    hits: list[CveHit] = []
    for entry in payload.get("vulnerabilities") or []:
        record = (entry or {}).get("cve") or {}
        if not isinstance(record, dict):
            continue
        hit = cve_hit(record)
        if hit is not None:
            hits.append(hit)

    return sort_cve_hits(hits)


async def github_pocs(cve_id: str) -> list[PoC]:
    """GitHub repo search sorted by stars; one 403/429 retry honoring Retry-After"""
    params: dict[str, str | int] = {
        "q": cve_id,
        "sort": "stars",
        "order": "desc",
        "per_page": _MAX_POC_REPOS,
    }

    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            _GITHUB_URL, params=params, headers=_GITHUB_HEADERS, timeout=HTTP_TIMEOUT
        )
        if response.status_code in (_HTTP_FORBIDDEN, _HTTP_RATE_LIMITED):
            await sleep_retry_after(response.headers)
            response = await client.get(
                _GITHUB_URL,
                params=params,
                headers=_GITHUB_HEADERS,
                timeout=HTTP_TIMEOUT,
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
