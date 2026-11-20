"""NVD fallback search: exact CPE, CVE keyword, then CPE dictionary."""

from __future__ import annotations

from typing import Any

from app.services.cve.cpe import (
    CpeCandidate,
    CpeMatch,
    CpeQuery,
    CpeSearchResult,
    MatchedBy,
    cpe_string,
    human_name,
    parse_cpe_name,
)
from app.services.cve.cpe_rank import rank_cpe_candidates
from app.services.cve.cve import (
    CveHit,
    cve_hit,
    exc_desc,
    parse_cve_hits,
    sort_cve_hits,
)
from app.services.cve.nvd import NVD_CPES, NVD_CVES, NvdClient

_MAX_CPE_CVES = 10
_MAX_CPE_PRODUCTS = 200


def _dictionary_candidates(payload: dict[str, Any]) -> list[CpeCandidate]:
    """cpe candidates from a cpes/2.0 payload, deprecated entries skipped"""
    candidates: list[CpeCandidate] = []

    for product in payload.get("products") or []:
        cpe = (product or {}).get("cpe") or {}
        if not isinstance(cpe, dict) or cpe.get("deprecated"):
            continue

        candidate = parse_cpe_name(str(cpe.get("cpeName") or ""))
        if candidate is None:
            continue

        title = ""
        for entry in cpe.get("titles") or []:
            if isinstance(entry, dict) and entry.get("title"):
                if entry.get("lang") == "en":
                    title = str(entry["title"])
                    break
                if not title:
                    title = str(entry["title"])

        candidates.append(
            CpeCandidate(
                part=candidate.part,
                vendor=candidate.vendor,
                product=candidate.product,
                title=title,
            )
        )

    return candidates


def _configuration_cpes(record: dict[str, Any]) -> list[CpeCandidate]:
    """vulnerable CPE match criteria from a CVE record's configurations"""
    candidates: list[CpeCandidate] = []
    nodes = [
        node
        for config in record.get("configurations") or []
        if isinstance(config, dict)
        for node in config.get("nodes") or []
        if isinstance(node, dict)
    ]

    while nodes:
        node = nodes.pop()
        for match in node.get("cpeMatch") or []:
            if not isinstance(match, dict) or match.get("vulnerable") is False:
                continue
            candidate = parse_cpe_name(str(match.get("criteria") or ""))
            if candidate is not None:
                candidates.append(candidate)

        for child in node.get("children") or []:
            if isinstance(child, dict):
                nodes.append(child)

    return candidates


def _keyword_result(
    payload: dict[str, Any], query: CpeQuery
) -> tuple[list[CveHit], list[CpeMatch]]:
    """keyword CVEs whose configurations contain a close CPE, plus those CPEs"""
    hits: list[CveHit] = []
    candidates: list[CpeCandidate] = []
    seen: set[str] = set()

    for entry in payload.get("vulnerabilities") or []:
        record = (entry or {}).get("cve") or {}
        if not isinstance(record, dict):
            continue

        record_candidates = _configuration_cpes(record)
        if not rank_cpe_candidates(query, record_candidates):
            continue

        hit = cve_hit(record)
        if hit is None or hit.cve_id in seen:
            continue

        seen.add(hit.cve_id)
        hits.append(hit)
        candidates.extend(record_candidates)

    return (
        sort_cve_hits(hits)[:_MAX_CPE_CVES],
        rank_cpe_candidates(query, candidates),
    )


async def _exact_cves(
    nvd: NvdClient, query: CpeQuery
) -> tuple[list[CveHit], int, str | None]:
    requested = cpe_string(query)
    try:
        payload = await nvd.get(
            NVD_CVES,
            {"virtualMatchString": requested, "resultsPerPage": _MAX_CPE_CVES},
            requested,
        )
    except Exception as exc:
        return [], 0, f"nvd cve search unavailable: {exc_desc(exc)}"

    hits = parse_cve_hits(payload)[:_MAX_CPE_CVES]
    return hits, int(payload.get("totalResults") or len(hits)), None


async def _keyword_cves(
    nvd: NvdClient, query: CpeQuery
) -> tuple[list[CveHit], int, list[CpeMatch], str | None]:
    keyword = " ".join(
        term for term in (human_name(query.product), query.version) if term
    )
    try:
        payload = await nvd.get(
            NVD_CVES,
            {"keywordSearch": keyword, "resultsPerPage": _MAX_CPE_CVES},
            keyword,
        )
    except Exception as exc:
        return [], 0, [], f"nvd keyword search unavailable: {exc_desc(exc)}"

    hits, similar = _keyword_result(payload, query)
    return hits, int(payload.get("totalResults") or len(hits)), similar, None


async def _dictionary_cpes(
    nvd: NvdClient, query: CpeQuery
) -> tuple[list[CpeMatch], str | None]:
    dictionary = " ".join(
        term for term in (human_name(query.vendor), human_name(query.product)) if term
    )
    try:
        payload = await nvd.get(
            NVD_CPES,
            {"keywordSearch": dictionary, "resultsPerPage": _MAX_CPE_PRODUCTS},
            dictionary,
        )
    except Exception as exc:
        return [], f"nvd cpe search unavailable: {exc_desc(exc)}"

    return rank_cpe_candidates(query, _dictionary_candidates(payload)), None


async def lookup_cves_by_cpe(nvd: NvdClient, query: CpeQuery) -> CpeSearchResult:
    """exact CPE first, then CVE keyword, then similar CPE names"""
    errors: list[str] = []

    hits, total, note = await _exact_cves(nvd, query)
    if note:
        errors.append(note)
    matched_by: MatchedBy = "cpe" if hits else "none"

    similar: list[CpeMatch] = []
    if not hits:
        hits, total, similar, note = await _keyword_cves(nvd, query)
        if note:
            errors.append(note)
        if hits:
            matched_by = "keyword"

    if not hits:
        similar, note = await _dictionary_cpes(nvd, query)
        if note:
            errors.append(note)
        if similar:
            matched_by = "similar"

    if matched_by == "cpe":
        resolved = cpe_string(query)
    else:
        resolved = similar[0].cpe_name if similar else ""

    return CpeSearchResult(
        query=query.raw,
        cpe=resolved,
        matched_by=matched_by,
        cves=hits,
        total=total,
        similar=similar,
        error="; ".join(errors) if errors else None,
    )
