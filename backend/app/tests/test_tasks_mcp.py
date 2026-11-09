"""MCP get_cve_info shares app/services/cve/; the package itself is tested in
test_services/test_cve_service.py."""

import pytest

from app.api.endpoints import tasks_mcp
from app.services.cve import CveInfo


@pytest.mark.asyncio
async def test_mcp_get_cve_info_returns_service_payload(monkeypatch):
    info = CveInfo(
        cve_id="CVE-2024-12345",
        description="a vulnerable parser",
        cwes=["CWE-354"],
        cvss_score=9.8,
        severity="CRITICAL",
        cvss_vector="CVSS:3.1/AV:N/AC:L",
        references=["https://nvd.nist.gov/vuln/detail/CVE-2024-12345"],
        pocs=[],
        error=None,
    )

    async def fake_lookup(cve_id: str) -> CveInfo:
        assert cve_id == "CVE-2024-12345"
        return info

    monkeypatch.setattr(tasks_mcp, "lookup_cve", fake_lookup)

    result = await tasks_mcp.get_cve_info("CVE-2024-12345")

    assert result["cve_id"] == "CVE-2024-12345"
    assert result["cwes"] == ["CWE-354"]
    assert result["cvss_score"] == 9.8
    assert result["references"] == ["https://nvd.nist.gov/vuln/detail/CVE-2024-12345"]
    assert result["error"] is None
