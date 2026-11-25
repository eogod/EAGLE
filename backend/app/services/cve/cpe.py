"""CPE domain types and query parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.services.cve.cve import CveHit

CpePart = Literal["a", "o", "h", ""]
MatchedBy = Literal["cpe", "keyword", "similar", "none"]

_CPE_PREFIX = "cpe:2.3:"
_CPE_FIELDS = 13
_CPE22_MIN_FIELDS = 3
_CPE_SHORTHAND_MIN_FIELDS = 2
_CPE_SHORTHAND_MAX_FIELDS = 3
_CPE_PARTS: tuple[CpePart, ...] = ("a", "o", "h")
# trailing wildcard CPE attributes
_CPE_TAIL = ":*:*:*:*:*:*:*"
_VERSION_RE: re.Pattern[str] = re.compile(r"^v?\d[\w.\-]*$", re.IGNORECASE)


@dataclass(frozen=True)
class CpeQuery:
    """normalized CPE search input: parsed fields plus the original text"""

    raw: str
    part: CpePart
    vendor: str
    product: str
    version: str


@dataclass(frozen=True)
class CpeCandidate:
    """one CPE dictionary or CVE-configuration product"""

    part: CpePart
    vendor: str
    product: str
    title: str = ""


@dataclass(frozen=True)
class CpeMatch:
    """one similar CPE candidate, reported at product level"""

    cpe_name: str
    vendor: str
    product: str
    title: str
    score: float


@dataclass(frozen=True)
class CpeSearchResult:
    """CVE hits for a CPE/software query, plus similar CPE fallbacks"""

    query: str
    cpe: str
    matched_by: MatchedBy
    cves: list[CveHit]
    total: int
    similar: list[CpeMatch]
    error: str | None


def _split_cpe_fields(value: str) -> list[str]:
    """split on unescaped colons, keeping backslash escapes intact"""
    fields: list[str] = []
    current: list[str] = []
    escaped = False

    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)

    fields.append("".join(current))
    return fields


def _cpe_token(value: str) -> str:
    """lowercase a CPE field, spaces to underscores, drop wildcard tokens"""
    token = value.strip().lower().replace(" ", "_")
    token = re.sub(r"[^a-z0-9._\-]", "", token)
    return "" if token in ("", "*", "-") else token


def _cpe_version(value: str) -> str:
    """normalize an explicit version, dropping a leading v and wildcards"""
    version = value.strip().lower()
    if version in ("", "*", "-"):
        return ""

    if version.startswith("v") and len(version) > 1 and version[1].isdigit():
        version = version[1:]

    return re.sub(r"[^a-z0-9._\-~]", "", version)


def _cpe_part(value: str) -> CpePart:
    part = value.strip().lower()
    return part if part in _CPE_PARTS else ""


def cpe_name(part: CpePart, vendor: str, product: str, version: str = "*") -> str:
    """build a CPE 2.3 string with wildcard tail fields"""
    return (
        f"{_CPE_PREFIX}{part or 'a'}:{vendor or '*'}:{product}:"
        f"{version or '*'}{_CPE_TAIL}"
    )


def human_name(value: str) -> str:
    """CPE token to readable words for scoring and titles"""
    return value.replace("_", " ").replace("-", " ").strip()


def parse_cpe_name(cpe_name_value: str) -> CpeCandidate | None:
    """parse a CPE 2.3 name, None when malformed"""
    fields = _split_cpe_fields(cpe_name_value)
    if len(fields) != _CPE_FIELDS or fields[0].lower() != "cpe" or fields[1] != "2.3":
        return None

    product = _cpe_token(fields[4])
    if not product:
        return None

    return CpeCandidate(
        part=_cpe_part(fields[2]),
        vendor=_cpe_token(fields[3]),
        product=product,
    )


def _split_software_name(value: str) -> tuple[str, str, str]:
    """(vendor, product, version) from a free-form software name"""
    tokens = value.replace(",", " ").split()
    version = ""
    if tokens and _VERSION_RE.match(tokens[-1]):
        version = _cpe_version(tokens.pop())

    if not tokens:
        return "", "", version
    if len(tokens) == 1:
        return "", _cpe_token(tokens[0]), version

    return _cpe_token(tokens[0]), _cpe_token(" ".join(tokens[1:])), version


def parse_cpe_query(value: str, version: str | None = None) -> CpeQuery:
    """accept CPE 2.3/2.2, vendor:product[:version], or a software name;
    ValueError when no product can be parsed"""
    raw = value.strip()
    if not raw:
        raise ValueError("empty CPE query, expected a CPE string or software name")

    explicit_version = _cpe_version(version or "")
    lowered = raw.lower()

    if lowered.startswith(_CPE_PREFIX):
        fields = _split_cpe_fields(raw)
        if len(fields) != _CPE_FIELDS:
            raise ValueError(
                f"invalid CPE 2.3 {value!r}, expected 13 colon-separated fields"
            )

        product = _cpe_token(fields[4])
        if not product:
            raise ValueError(f"invalid CPE {value!r}, missing product")

        return CpeQuery(
            raw=raw,
            part=_cpe_part(fields[2]),
            vendor=_cpe_token(fields[3]),
            product=product,
            version=explicit_version or _cpe_version(fields[5]),
        )

    if lowered.startswith("cpe:/"):
        fields = _split_cpe_fields(raw[5:])
        if len(fields) < _CPE22_MIN_FIELDS:
            raise ValueError(
                f"invalid CPE 2.2 {value!r}, expected cpe:/part:vendor:product"
            )

        product = _cpe_token(fields[2])
        if not product:
            raise ValueError(f"invalid CPE {value!r}, missing product")

        parsed_version = fields[3] if len(fields) > _CPE22_MIN_FIELDS else ""
        return CpeQuery(
            raw=raw,
            part=_cpe_part(fields[0]),
            vendor=_cpe_token(fields[1]),
            product=product,
            version=explicit_version or _cpe_version(parsed_version),
        )

    if lowered.startswith("cpe:"):
        raise ValueError(f"invalid CPE {value!r}, expected CPE 2.3 or 2.2 format")

    if ":" in raw:
        fields = _split_cpe_fields(raw)
        field_count = len(fields)
        if (
            _CPE_SHORTHAND_MIN_FIELDS <= field_count <= _CPE_SHORTHAND_MAX_FIELDS
            and all(field.strip() and " " not in field for field in fields)
        ):
            vendor = _cpe_token(fields[0])
            product = _cpe_token(fields[1])
            if vendor and product:
                parsed_version = (
                    fields[2] if field_count == _CPE_SHORTHAND_MAX_FIELDS else ""
                )
                return CpeQuery(
                    raw=raw,
                    part="",
                    vendor=vendor,
                    product=product,
                    version=explicit_version or _cpe_version(parsed_version),
                )

    vendor, product, parsed_version = _split_software_name(raw)
    if not product:
        raise ValueError(
            f"invalid software name {value!r}, expected vendor/product or a CPE"
        )

    return CpeQuery(
        raw=raw,
        part="",
        vendor=vendor,
        product=product,
        version=explicit_version or parsed_version,
    )


def cpe_cache_key(query: CpeQuery) -> str:
    """normalized key so formatting variants share one cache entry"""
    return (
        f"{query.part or 'a'}|{query.vendor or '*'}|{query.product}|"
        f"{query.version or '*'}"
    )


def cpe_string(query: CpeQuery) -> str:
    """CPE 2.3 match string for the CVE endpoint; missing vendor becomes *"""
    return cpe_name(query.part, query.vendor, query.product, query.version)
