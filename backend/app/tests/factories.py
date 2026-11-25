"""Builders shared by the CVE service, tool and MCP tests."""

from dataclasses import replace
from typing import Any

from app.services.cve import CpeSearchResult, CveHit


def make_cve_hit(
    cve_id: str = "CVE-2021-41524",
    *,
    description: str = "a flaw",
    cvss_score: float | None = 7.5,
    severity: str = "HIGH",
    published: str = "2021-10-05",
) -> CveHit:
    return CveHit(
        cve_id=cve_id,
        description=description,
        cvss_score=cvss_score,
        severity=severity,
        published=published,
    )


_BASE_CPE_RESULT = CpeSearchResult(
    query="apache httpd 2.4.49",
    cpe="cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:*",
    matched_by="keyword",
    cves=[make_cve_hit()],
    total=1,
    similar=[],
    error=None,
)


def make_cpe_result(**changes: Any) -> CpeSearchResult:
    return replace(_BASE_CPE_RESULT, **changes)
