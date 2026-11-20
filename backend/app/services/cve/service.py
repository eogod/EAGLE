"""Stateful CVE/CPE lookups with a small in-process cache."""

from __future__ import annotations

import time

from app.services.cve.cpe import CpeSearchResult, cpe_cache_key, parse_cpe_query
from app.services.cve.cpe_search import lookup_cves_by_cpe
from app.services.cve.cve import (
    CveInfo,
    exc_desc,
    github_pocs,
    normalize_cve_id,
    parse_nvd,
)
from app.services.cve.nvd import NvdClient

# 1h TTL and a small bound keep repeated lookups off the NVD quota
_CACHE_TTL: float = 3600.0
_CACHE_MAX = 100


def _cache_get[Value](cache: dict[str, tuple[float, Value]], key: str) -> Value | None:
    """live value for key, or None after dropping an expired entry"""
    cached = cache.get(key)
    if cached is None:
        return None

    expires, value = cached
    if time.monotonic() < expires:
        return value

    del cache[key]
    return None


def _cache_put[Value](
    cache: dict[str, tuple[float, Value]],
    key: str,
    value: Value,
    ttl: float,
    max_entries: int,
) -> None:
    """store one value, dropping stale and oldest entries first"""
    now = time.monotonic()

    for stale in [k for k, v in cache.items() if now >= v[0]]:
        del cache[stale]

    if key not in cache and len(cache) >= max_entries:
        cache.pop(next(iter(cache)))

    cache[key] = (now + ttl, value)


class CveService:
    """stateful CVE lookups: owns the caches and one NVD client"""

    def __init__(self, nvd: NvdClient | None = None) -> None:
        self._cache: dict[str, tuple[float, CveInfo]] = {}
        self._cpe_cache: dict[str, tuple[float, CpeSearchResult]] = {}
        self._nvd = nvd or NvdClient()

    async def get_cve_info(self, cve_id: str) -> CveInfo:
        """NVD 2.0 details + GitHub PoC repos; network failures land in error."""
        normalized = normalize_cve_id(cve_id)
        cached = _cache_get(self._cache, normalized)
        if cached is not None:
            return cached

        errors: list[str] = []
        try:
            record = await self._nvd.details(normalized)
        except Exception as exc:
            record = None
            errors.append(f"nvd unavailable: {exc_desc(exc)}")

        facts = parse_nvd(record)

        try:
            pocs = await github_pocs(normalized)
        except Exception as exc:
            pocs = []
            errors.append(f"github unavailable: {exc_desc(exc)}")

        info = CveInfo(
            cve_id=normalized,
            description=facts.description,
            cwes=facts.cwes,
            cvss_score=facts.cvss_score,
            severity=facts.severity,
            cvss_vector=facts.cvss_vector,
            references=facts.references,
            pocs=pocs,
            error="; ".join(errors) if errors else None,
        )
        if info.error is None:
            _cache_put(self._cache, normalized, info, _CACHE_TTL, _CACHE_MAX)

        return info

    async def search_cves_by_cpe(
        self, cpe: str, version: str | None = None
    ) -> CpeSearchResult:
        """CVEs for a CPE or software name/version, with similar-CPE fallback."""
        query = parse_cpe_query(cpe, version)
        key = cpe_cache_key(query)
        cached = _cache_get(self._cpe_cache, key)
        if cached is not None:
            return cached

        result = await lookup_cves_by_cpe(self._nvd, query)
        if result.error is None:
            _cache_put(self._cpe_cache, key, result, _CACHE_TTL, _CACHE_MAX)

        return result

    def clear_cve_cache(self) -> None:
        """drop every cached CVE so tests and retries see a cold cache"""
        self._cache.clear()

    def clear_cpe_cache(self) -> None:
        """drop every cached CPE search so tests and retries see a cold cache"""
        self._cpe_cache.clear()


# shared by the research tool services and the MCP CVE tools
cve_service = CveService()
