"""Compact CVE/CPE text for the research agent."""

from __future__ import annotations

from app.services.cve.cpe import CpeSearchResult
from app.services.cve.cve import CveInfo

_CPE_DESC_CHARS = 200


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


def format_cpe_search(result: CpeSearchResult) -> str:
    """compact text for an LLM: resolved CPE, CVE rows and similar CPE names."""
    lines = [f"Query: {result.query}"]

    if result.cpe:
        source = (
            f" (matched by {result.matched_by})" if result.matched_by != "cpe" else ""
        )
        lines.append(f"CPE: {result.cpe}{source}")

    if result.cves:
        shown = (
            f"{result.total} total, showing {len(result.cves)}"
            if result.total
            else f"showing {len(result.cves)}"
        )
        lines.append(f"CVEs: {shown}")
        for hit in result.cves:
            meta: list[str] = []
            if hit.cvss_score is not None:
                meta.append(f"{hit.cvss_score} {hit.severity}".strip())
            if hit.published:
                meta.append(hit.published[:10])
            prefix = f" | {' | '.join(meta)}" if meta else ""
            summary = (
                f" | {hit.description[:_CPE_DESC_CHARS]}" if hit.description else ""
            )
            lines.append(f"- {hit.cve_id}{prefix}{summary}")

        lines.append("Call get_cve_info for references, PoCs and full CVSS.")

    if result.similar:
        lines.append("Similar CPEs:")
        for match in result.similar:
            title = f" {match.title}" if match.title else ""
            lines.append(f"- {match.cpe_name} (score {match.score:.2f}){title}")

    if not result.cves and not result.similar:
        lines.append("No CVEs or similar CPEs found.")

    if result.error:
        lines.append(f"Error: {result.error}")

    return "\n".join(lines)
