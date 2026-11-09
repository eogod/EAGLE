"""NVD 2.0 transport with retries and a shared rate gate."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.rate_gate import MinIntervalGate

logger = logging.getLogger(__name__)

NVD_API_URL = "https://services.nvd.nist.gov/rest/json"
NVD_CVES = "cves/2.0"
NVD_CPES = "cpes/2.0"
HTTP_TIMEOUT: float = 30.0
_MAX_RETRIES = 3
_MAX_RETRY_WAIT = 30.0
_HTTP_RATE_LIMITED = 429
# backoff jitter only, never cryptographic material
_RNG = random.SystemRandom()


def parse_retry_after(headers: Any) -> float | None:
    """Retry-After seconds, or None when missing or malformed"""
    raw = headers.get("retry-after") if headers else None
    if raw is None:
        return None

    try:
        return min(max(0.0, float(str(raw).strip())), _MAX_RETRY_WAIT)
    except ValueError:
        return None


def retry_wait_seconds(headers: Any, default: float = 2.0) -> float:
    """Retry-After seconds with a default for missing/malformed headers"""
    wait = parse_retry_after(headers)
    return default if wait is None else wait


async def sleep_retry_after(headers: Any, default: float = 2.0) -> None:
    wait = retry_wait_seconds(headers, default)
    if wait > 0:
        await asyncio.sleep(wait)


class NvdClient:
    """unauthenticated NVD 2.0 requests behind one politeness gate"""

    def __init__(self) -> None:
        self._gate = MinIntervalGate()

    async def get(
        self, path: str, params: dict[str, str | int], label: str
    ) -> dict[str, Any]:
        """one NVD GET with up to three 429 retries honoring Retry-After"""
        cfg = get_settings().webtools
        await self._gate.wait(cfg.nvd_min_interval)

        url = f"{NVD_API_URL}/{path}"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
            for attempt in range(1, _MAX_RETRIES + 1):
                if response.status_code != _HTTP_RATE_LIMITED:
                    break

                # no header, or a header of zero: back off on the gate interval
                delay = parse_retry_after(response.headers) or 0.0
                delay = delay or cfg.nvd_min_interval * (2**attempt)
                delay = min(_MAX_RETRY_WAIT, delay + _RNG.uniform(0, 1))
                logger.warning(
                    "NVD rate-limited for %s, backing off %.1fs (attempt %d/%d)",
                    label,
                    delay,
                    attempt,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                response = await client.get(url, params=params, timeout=HTTP_TIMEOUT)

            response.raise_for_status()
            payload = response.json() or {}

        return payload if isinstance(payload, dict) else {}

    async def details(self, cve_id: str) -> dict[str, Any] | None:
        """NVD 2.0 details record, None when NVD has no entry for the id."""
        payload = await self.get(NVD_CVES, {"cveId": cve_id}, cve_id)
        rows = payload.get("vulnerabilities") or []
        if not rows:
            return None

        record = (rows[0] or {}).get("cve") or {}

        return record if isinstance(record, dict) and record else None
