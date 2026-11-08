"""
CVE service checks with mocked transport: id validation, NVD parsing, GitHub
PoC repos, partial failures and 429 backoff. No network.
"""

import json
import random

import httpx
import pytest

from app.core.config import get_settings
from app.services import cve_service as cs
from app.services.cve_service import CveInfo, PoC, cve_service

# session loop keeps the singleton's NVD gate bound to one loop
pytestmark = pytest.mark.asyncio(loop_scope="session")

_NVD_BODY = json.dumps(
    {
        "vulnerabilities": [
            {
                "cve": {
                    "descriptions": [
                        {"lang": "fr", "value": "description francaise"},
                        {"lang": "en", "value": "english description"},
                    ],
                    "weaknesses": [
                        {
                            "description": [
                                {"value": "CWE-79"},
                                {"value": "CWE-79"},  # duplicate, must dedup
                                {"value": "CWE-89"},
                            ]
                        }
                    ],
                    "metrics": {
                        "cvssMetricV31": [
                            {
                                "cvssData": {
                                    "baseScore": 7.5,
                                    "baseSeverity": "HIGH",
                                    "vectorString": "CVSS:3.1/AV:N/AC:L",
                                }
                            }
                        ]
                    },
                    "references": [
                        {"url": f"https://refs.example.com/{i}"} for i in range(12)
                    ],
                }
            }
        ]
    }
)

_GITHUB_BODY = json.dumps(
    {
        "items": [
            {
                "full_name": "acme/poc-one",
                "html_url": "https://github.com/acme/poc-one",
                "stargazers_count": 42,
                "description": "PoC for the CVE",
                "language": "Python",
            },
            {
                "full_name": "acme/poc-two",
                "html_url": "https://github.com/acme/poc-two",
                "stargazers_count": 0,
                "description": None,
                "language": None,
            },
        ]
    }
)

_NVD_OK = {"status": 200, "body": _NVD_BODY}
_GH_OK = {"status": 200, "body": _GITHUB_BODY}


@pytest.fixture(autouse=True)
def _cold_env(monkeypatch):
    """cold cache, no NVD politeness wait"""
    cve_service.clear_cve_cache()
    monkeypatch.setattr(get_settings().webtools, "nvd_min_interval", 0.0)
    yield
    cve_service.clear_cve_cache()


def _use_transport(monkeypatch, nvd, github, calls) -> None:
    """route by host; items are {"status","body","headers"} dicts, an emptied
    queue falls back to a 500 so over-queued retries fail loudly"""
    nvd_queue = list(nvd)
    gh_queue = list(github)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        queue = nvd_queue if "nvd.nist.gov" in str(request.url) else gh_queue
        item = queue.pop(0) if queue else {"status": 500, "body": "{}"}
        return httpx.Response(
            item["status"], content=item["body"], headers=item.get("headers", {})
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.cve_service.httpx.AsyncClient",
        lambda **kw: original(**{**kw, "transport": transport}),
    )


async def test_normalize_cve_id_cases() -> None:
    assert cs.normalize_cve_id(" cve-2021-44228 ") == "CVE-2021-44228"
    assert cs.normalize_cve_id("CVE-2024-3094") == "CVE-2024-3094"
    for bad in ("not-a-cve", "CVE-2021-44228111", "CVE-21-1", "1969-00000-1"):
        with pytest.raises(ValueError):
            cs.normalize_cve_id(bad)


async def test_get_info_parses_nvd_and_github_pocs(monkeypatch):
    calls: list[str] = []
    _use_transport(monkeypatch, [_NVD_OK], [_GH_OK], calls)

    info = await cve_service.get_cve_info("CVE-2023-48795")

    assert info.error is None
    assert info.description == "english description"  # en wins over first
    assert info.cwes == ["CWE-79", "CWE-89"]  # dedup
    assert info.cvss_score == 7.5  # noqa: PLR2004
    assert info.severity == "HIGH"
    assert info.cvss_vector == "CVSS:3.1/AV:N/AC:L"
    assert len(info.references) == 10  # noqa: PLR2004  # capped
    assert len(info.pocs) == 2  # noqa: PLR2004
    assert info.pocs[0] == PoC(
        repo="acme/poc-one",
        url="https://github.com/acme/poc-one",
        stars=42,  # noqa: PLR2004
        description="PoC for the CVE",
        language="Python",
    )
    assert info.pocs[1].description == ""  # nulls quieted
    assert len(calls) == 2  # one NVD + one GitHub call


async def test_nvd_429_backs_off_then_parses(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda _a, _b: 0.0)  # no jitter sleep
    calls: list[str] = []
    _use_transport(
        monkeypatch,
        [
            {
                "status": 429,
                "body": '{"statusCode":429}',
                "headers": {"Retry-After": "0"},
            },
            _NVD_OK,
        ],
        [_GH_OK],
        calls,
    )

    info = await cve_service.get_cve_info(
        "cve-2023-48795"
    )  # lowercase in, uppercased out

    assert info.cve_id == "CVE-2023-48795"
    assert info.description == "english description"
    assert len(calls) == 3  # NVD twice + GitHub once


async def test_nvd_down_still_lists_pocs(monkeypatch):
    calls: list[str] = []
    _use_transport(monkeypatch, [{"status": 500, "body": "{}"}], [_GH_OK], calls)

    info = await cve_service.get_cve_info("CVE-2023-48795")

    assert info.error is not None
    assert info.error.startswith("nvd unavailable: ")
    assert info.description == ""
    assert info.pocs  # github phase still ran
    assert len(calls) == 2


async def test_github_down_sets_error_but_nvd_stays(monkeypatch):
    _use_transport(monkeypatch, [_NVD_OK], [{"status": 502, "body": "{}"}], [])

    info = await cve_service.get_cve_info("CVE-2023-48795")

    assert info.error is not None
    assert "github unavailable: " in info.error
    assert info.description == "english description"
    assert info.pocs == []


async def test_both_down_joins_error_notes(monkeypatch):
    _use_transport(
        monkeypatch,
        [{"status": 500, "body": "{}"}],
        [{"status": 500, "body": "{}"}],
        [],
    )

    info = await cve_service.get_cve_info("CVE-2023-48795")

    assert "nvd unavailable: " in (info.error or "")
    assert "github unavailable: " in (info.error or "")


async def test_nvd_not_found_is_no_error(monkeypatch):
    _use_transport(
        monkeypatch,
        [{"status": 200, "body": json.dumps({"vulnerabilities": []})}],
        [_GH_OK],
        [],
    )

    info = await cve_service.get_cve_info("CVE-2099-0001")

    assert info.error is None
    assert info.description == ""
    assert info.cvss_score is None


async def test_success_result_is_cached(monkeypatch):
    calls: list[str] = []
    _use_transport(monkeypatch, [_NVD_OK], [_GH_OK], calls)

    first = await cve_service.get_cve_info("CVE-2023-48795")
    second = await cve_service.get_cve_info("CVE-2023-48795")

    assert first == second
    assert len(calls) == 2  # second lookup hit the cache, no extra requests


async def test_format_cve_info_compact() -> None:
    info = CveInfo(
        cve_id="CVE-2023-48795",
        description="Terrapin ssh attack.",
        cwes=["CWE-354"],
        cvss_score=5.9,
        severity="MEDIUM",
        cvss_vector="CVSS:3.1/AV:N",
        references=["https://refs.example.com/1"],
        pocs=[
            PoC(
                repo="acme/poc",
                url="https://github.com/acme/poc",
                stars=7,
                description="works",
                language="Go",
            )
        ],
        error="github unavailable: HTTPError",
    )

    text = cs.format_cve_info(info)

    assert "CVE-2023-48795" in text
    assert "Description: Terrapin ssh attack." in text
    assert "CWEs: CWE-354" in text
    assert "CVSS: 5.9 (MEDIUM) [CVSS:3.1/AV:N]" in text
    assert "- https://refs.example.com/1" in text
    assert "- https://github.com/acme/poc (acme/poc, Go, 7 stars) — works" in text
    assert "Error: github unavailable" in text
