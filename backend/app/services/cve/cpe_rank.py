"""CPE similarity scoring and candidate ranking."""

from __future__ import annotations

import difflib

from app.services.cve.cpe import CpeCandidate, CpeMatch, CpeQuery, cpe_name, human_name

_MAX_CPE_SUGGESTIONS = 5
_MIN_CPE_SCORE = 0.45
_MIN_CPE_PRODUCT_SCORE = 0.4


def _name_score(query: str, candidate: str, *, containment: bool) -> float:
    """best of sequence ratio and token overlap, 0..1"""
    left = human_name(query).lower()
    right = human_name(candidate).lower()
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return ratio

    if containment:
        overlap = len(left_tokens & right_tokens) / len(left_tokens)
    else:
        overlap = len(left_tokens & right_tokens) / len(right_tokens)

    return max(ratio, overlap)


def rank_cpe_candidates(
    query: CpeQuery, candidates: list[CpeCandidate]
) -> list[CpeMatch]:
    """dedupe by part/vendor/product and keep the closest names"""
    best: dict[tuple[str, str, str], CpeMatch] = {}

    for candidate in candidates:
        if not candidate.product:
            continue
        if query.part and candidate.part and candidate.part != query.part:
            continue

        product_score = _name_score(query.product, candidate.product, containment=False)
        if product_score < _MIN_CPE_PRODUCT_SCORE:
            continue

        if query.vendor:
            vendor_score = _name_score(query.vendor, candidate.vendor, containment=True)
            score = 0.7 * product_score + 0.3 * vendor_score
        else:
            score = product_score

        if score < _MIN_CPE_SCORE:
            continue

        key = (candidate.part, candidate.vendor, candidate.product)
        match = CpeMatch(
            cpe_name=cpe_name(candidate.part, candidate.vendor, candidate.product),
            vendor=candidate.vendor,
            product=candidate.product,
            title=candidate.title
            or " ".join(
                word
                for word in (
                    human_name(candidate.vendor),
                    human_name(candidate.product),
                )
                if word
            ),
            score=round(score, 2),
        )
        if key not in best or match.score > best[key].score:
            best[key] = match

    ranked = sorted(best.values(), key=lambda match: (-match.score, match.product))
    return ranked[:_MAX_CPE_SUGGESTIONS]
