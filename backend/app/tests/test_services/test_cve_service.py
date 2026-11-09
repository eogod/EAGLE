"""
CVE/CPE service checks with mocked transport: id and CPE validation, NVD
parsing, keyword/similar-CPE fallbacks, GitHub PoC repos, partial failures and
429 backoff. No network.
"""

import json

import httpx
import pytest

from app.core.config import get_settings
from app.services.cve import (
    CpeMatch,
    CveInfo,
    PoC,
    cve_service,
    format_cpe_search,
    format_cve_info,
    normalize_cve_id,
    parse_cpe_query,
)
from app.services.cve import nvd as nvd_module
from app.tests.factories import make_cpe_result, make_cve_hit

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

_EMPTY_CVES = {"status": 200, "body": json.dumps({"vulnerabilities": []})}

_EXACT_CPE_BODY = json.dumps(
    {
        "totalResults": 88,
        "vulnerabilities": [
            {
                "cve": {
                    "id": "CVE-2021-41773",
                    "published": "2021-10-05T09:15:07.427",
                    "descriptions": [
                        {"lang": "en", "value": "path traversal in httpd"}
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
                }
            }
        ],
    }
)

_KEYWORD_BODY = json.dumps(
    {
        "totalResults": 2,
        "vulnerabilities": [
            {
                "cve": {
                    "id": "CVE-2021-41524",
                    "published": "2021-10-05T09:15:07.427",
                    "descriptions": [
                        {"lang": "en", "value": "fuzzing the 2.4.49 httpd"}
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
                    "configurations": [
                        {
                            "nodes": [
                                {
                                    "cpeMatch": [
                                        {
                                            "criteria": "cpe:2.3:a:apache:http_server:2.4.49:*:*:*:*:*:*:*",
                                            "vulnerable": True,
                                        },
                                        {
                                            "criteria": "cpe:2.3:o:fedoraproject:fedora:34:*:*:*:*:*:*:*",
                                            "vulnerable": True,
                                        },
                                    ]
                                }
                            ]
                        }
                    ],
                }
            },
            {
                "cve": {
                    "id": "CVE-2099-9999",
                    "published": "2099-01-01T00:00:00.000",
                    "descriptions": [{"lang": "en", "value": "unrelated httpd fork"}],
                    "configurations": [
                        {
                            "nodes": [
                                {
                                    "cpeMatch": [
                                        {
                                            "criteria": "cpe:2.3:a:vector:ultra_mini_httpd:1.21:*:*:*:*:*:*:*",
                                            "vulnerable": True,
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                }
            },
        ],
    }
)

_CPE_BODY = json.dumps(
    {
        "products": [
            {
                "cpe": {
                    "cpeName": "cpe:2.3:a:apache:http_server:2.4.49:*:*:*:*:*:*:*",
                    "deprecated": False,
                    "titles": [{"title": "Apache HTTP Server 2.4.49", "lang": "en"}],
                }
            },
            {
                "cpe": {
                    "cpeName": "cpe:2.3:a:apache:tomcat:9.0.0:*:*:*:*:*:*:*",
                    "deprecated": False,
                    "titles": [{"title": "Apache Tomcat 9.0.0", "lang": "en"}],
                }
            },
            {
                "cpe": {
                    "cpeName": "cpe:2.3:a:old:product:1.0:*:*:*:*:*:*:*",
                    "deprecated": True,
                    "titles": [{"title": "old product", "lang": "en"}],
                }
            },
        ]
    }
)


_EXACT_CPE_OK = {"status": 200, "body": _EXACT_CPE_BODY}
_KEYWORD_OK = {"status": 200, "body": _KEYWORD_BODY}
_CPE_OK = {"status": 200, "body": _CPE_BODY}


@pytest.fixture(autouse=True)
def _cold_env(monkeypatch):
    """cold cache, no NVD politeness wait"""
    cve_service.clear_cve_cache()
    cve_service.clear_cpe_cache()
    monkeypatch.setattr(get_settings().webtools, "nvd_min_interval", 0.0)
    yield
    cve_service.clear_cve_cache()
    cve_service.clear_cpe_cache()


def _use_transport(monkeypatch, nvd, github, calls, cpes=None) -> None:
    """route by endpoint; items are {"status","body","headers"} dicts, an emptied
    queue falls back to a 500 so over-queued retries fail loudly"""
    nvd_queue = list(nvd)
    gh_queue = list(github)
    cpe_queue = list(cpes or [])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/cpes/" in url:
            queue = cpe_queue
        elif "nvd.nist.gov" in url:
            queue = nvd_queue
        else:
            queue = gh_queue
        item = queue.pop(0) if queue else {"status": 500, "body": "{}"}
        return httpx.Response(
            item["status"], content=item["body"], headers=item.get("headers", {})
        )

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.cve.nvd.httpx.AsyncClient",
        lambda **kw: original(**{**kw, "transport": transport}),
    )


async def test_normalize_cve_id_cases() -> None:
    assert normalize_cve_id(" cve-2021-44228 ") == "CVE-2021-44228"
    assert normalize_cve_id("CVE-2024-3094") == "CVE-2024-3094"
    for bad in ("not-a-cve", "CVE-2021-44228111", "CVE-21-1", "1969-00000-1"):
        with pytest.raises(ValueError):
            normalize_cve_id(bad)


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
    monkeypatch.setattr(
        nvd_module._RNG, "uniform", lambda _a, _b: 0.0
    )  # no jitter sleep
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

    text = format_cve_info(info)

    assert "CVE-2023-48795" in text
    assert "Description: Terrapin ssh attack." in text
    assert "CWEs: CWE-354" in text
    assert "CVSS: 5.9 (MEDIUM) [CVSS:3.1/AV:N]" in text
    assert "- https://refs.example.com/1" in text
    assert "- https://github.com/acme/poc (acme/poc, Go, 7 stars) — works" in text
    assert "Error: github unavailable" in text


async def test_parse_cpe_query_cases() -> None:
    full = parse_cpe_query("cpe:2.3:a:apache:http_server:2.4.49:*:*:*:*:*:*:*")
    assert (full.part, full.vendor, full.product, full.version) == (
        "a",
        "apache",
        "http_server",
        "2.4.49",
    )

    legacy = parse_cpe_query("cpe:/a:apache:http_server:2.4.49")
    assert (legacy.part, legacy.vendor, legacy.product, legacy.version) == (
        "a",
        "apache",
        "http_server",
        "2.4.49",
    )

    shorthand = parse_cpe_query("apache:http_server:2.4.49")
    assert (shorthand.vendor, shorthand.product, shorthand.version) == (
        "apache",
        "http_server",
        "2.4.49",
    )

    named = parse_cpe_query("Apache HTTP Server 2.4.49")
    assert (named.vendor, named.product, named.version) == (
        "apache",
        "http_server",
        "2.4.49",
    )

    overridden = parse_cpe_query("apache http server", "2.4.50")
    assert overridden.version == "2.4.50"

    comma = parse_cpe_query("Apache HTTP Server, 2.4.49")
    assert (comma.vendor, comma.product, comma.version) == (
        "apache",
        "http_server",
        "2.4.49",
    )

    product_only = parse_cpe_query("OpenSSL 3.0.1")
    assert (product_only.vendor, product_only.product, product_only.version) == (
        "",
        "openssl",
        "3.0.1",
    )

    two_words = parse_cpe_query("Microsoft Exchange Server 2019")
    assert (two_words.vendor, two_words.product, two_words.version) == (
        "microsoft",
        "exchange_server",
        "2019",
    )

    bare = parse_cpe_query("log4j")
    assert (bare.vendor, bare.product, bare.version) == ("", "log4j", "")

    prefixed = parse_cpe_query("openssl", "v3.0.1")
    assert prefixed.version == "3.0.1"

    for bad in ("", "cpe:2.3:a:apache", "cpe:/a:apache"):
        with pytest.raises(ValueError):
            parse_cpe_query(bad)


async def test_search_by_cpe_normalizes_software_name(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(monkeypatch, [_EXACT_CPE_OK], [], calls)

    result = await cve_service.search_cves_by_cpe("Apache HTTP Server", "2.4.49")

    assert result.matched_by == "cpe"
    assert httpx.URL(calls[0]).params["virtualMatchString"] == (
        "cpe:2.3:a:apache:http_server:2.4.49:*:*:*:*:*:*:*"
    )


async def test_search_by_cpe_product_only_uses_wildcard_vendor(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(monkeypatch, [_EXACT_CPE_OK], [], calls)

    result = await cve_service.search_cves_by_cpe("OpenSSL 3.0.1")

    assert result.matched_by == "cpe"
    assert httpx.URL(calls[0]).params["virtualMatchString"] == (
        "cpe:2.3:a:*:openssl:3.0.1:*:*:*:*:*:*:*"
    )


async def test_search_by_cpe_exact(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(monkeypatch, [_EXACT_CPE_OK], [], calls)

    result = await cve_service.search_cves_by_cpe(
        "cpe:2.3:a:apache:http_server:2.4.49:*:*:*:*:*:*:*"
    )

    assert result.matched_by == "cpe"
    assert result.cpe == "cpe:2.3:a:apache:http_server:2.4.49:*:*:*:*:*:*:*"
    assert result.total == 88
    assert result.cves[0].cve_id == "CVE-2021-41773"
    assert result.error is None
    assert len(calls) == 1
    assert httpx.URL(calls[0]).params["virtualMatchString"] == result.cpe


async def test_search_by_cpe_falls_back_to_keyword(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(monkeypatch, [_EMPTY_CVES, _KEYWORD_OK], [], calls)

    result = await cve_service.search_cves_by_cpe("apache httpd", "2.4.49")

    assert result.matched_by == "keyword"
    assert result.cpe == "cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:*"
    assert [match.product for match in result.similar] == ["http_server"]
    assert [hit.cve_id for hit in result.cves] == ["CVE-2021-41524"]
    assert result.total == 2
    assert result.error is None
    assert len(calls) == 2
    assert httpx.URL(calls[1]).params["keywordSearch"] == "httpd 2.4.49"


async def test_search_by_cpe_falls_back_to_similar_names(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(
        monkeypatch,
        [_EMPTY_CVES, _EMPTY_CVES],
        [],
        calls,
        cpes=[_CPE_OK],
    )

    result = await cve_service.search_cves_by_cpe("apache httpd 2.4.49")

    assert result.matched_by == "similar"
    assert result.cves == []
    assert result.cpe == "cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:*"
    assert [match.product for match in result.similar] == ["http_server"]
    assert result.similar[0].title == "Apache HTTP Server 2.4.49"
    assert len(calls) == 3
    assert httpx.URL(calls[2]).params["keywordSearch"] == "apache httpd"


async def test_search_by_cpe_reports_partial_failures(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(
        monkeypatch,
        [{"status": 500, "body": "{}"}, {"status": 500, "body": "{}"}],
        [],
        calls,
        cpes=[{"status": 500, "body": "{}"}],
    )

    result = await cve_service.search_cves_by_cpe("apache httpd", "2.4.49")

    assert result.matched_by == "none"
    assert result.cpe == ""
    assert result.cves == []
    assert result.similar == []
    assert "nvd cve search unavailable" in (result.error or "")
    assert "nvd keyword search unavailable" in (result.error or "")
    assert "nvd cpe search unavailable" in (result.error or "")
    assert len(calls) == 3


async def test_search_by_cpe_success_is_cached(monkeypatch) -> None:
    calls: list[str] = []
    _use_transport(monkeypatch, [_EXACT_CPE_OK], [], calls)

    first = await cve_service.search_cves_by_cpe("apache http server", "2.4.49")
    second = await cve_service.search_cves_by_cpe("apache http_server 2.4.49")

    assert first == second
    assert len(calls) == 1


async def test_format_cpe_search_compact() -> None:
    result = make_cpe_result(
        cves=[make_cve_hit()],
        similar=[
            CpeMatch(
                cpe_name="cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:*",
                vendor="apache",
                product="http_server",
                title="Apache HTTP Server",
                score=0.65,
            )
        ],
        error="nvd cve search unavailable: HTTPError",
    )

    text = format_cpe_search(result)

    assert (
        "CPE: cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:* (matched by keyword)" in text
    )
    assert "CVEs: 1 total, showing 1" in text
    assert "- CVE-2021-41524 | 7.5 HIGH | 2021-10-05 | a flaw" in text
    assert "Similar CPEs:" in text
    assert "(score 0.65) Apache HTTP Server" in text
    assert "Error: nvd cve search unavailable: HTTPError" in text

    empty = make_cpe_result(
        query="unknown",
        cpe="",
        matched_by="none",
        cves=[],
        total=0,
    )
    assert "No CVEs or similar CPEs found." in format_cpe_search(empty)
