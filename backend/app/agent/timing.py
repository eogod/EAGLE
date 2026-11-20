"""Dwell normalisation for a drafted chain.

The model proposes how long the attacker spent before each step; this module clamps
those numbers to the phase bands and fits the whole chain into the 20-90 minute
window. Nothing here waits - chain_exec owns execution, and it only waits when the
operator turned timing on.
"""

from __future__ import annotations

from typing import Any

# TODO: model step duration, post-step delay, active hours and a run seed.
# per-phase (min, max) seconds for one step; research-heavy phases get more room
PHASE_BANDS: dict[str, tuple[int, int]] = {
    "Reconnaissance": (60, 600),
    "Resource Development": (300, 1200),
    "Delivery": (120, 600),
    "Social Engineering": (120, 600),
    "Exploitation": (180, 1200),
    "Persistence": (120, 600),
    "Defense Evasion": (300, 1200),
    "Command & Control": (120, 600),
    "Pivoting": (120, 600),
    "Discovery": (60, 600),
    "Privilege Escalation": (300, 1200),
    "Execution": (60, 300),
    "Credential Access": (180, 900),
    "Lateral Movement": (120, 600),
    "Collection": (120, 600),
    "Exfiltration": (120, 600),
    "Impact": (60, 300),
    "Objectives": (60, 300),
}

DEFAULT_BAND = (60, 600)
# moving into a phase costs at least this much before its first step
PHASE_OPENING_FLOOR = 180
MIN_TOTAL = 20 * 60
MAX_TOTAL = 90 * 60
# runtime cap, one bad row must not park a chain for a day
MAX_STEP_DELAY = 60 * 60

REPORT_SOURCE = "report"


def band_of(phase: str) -> tuple[int, int]:
    return PHASE_BANDS.get(phase, DEFAULT_BAND)


def clamp_step_delay(phase: str, seconds: int) -> int:
    """0 stays 0: a step can be a direct continuation of the previous one."""
    if seconds <= 0:
        return 0
    low, high = band_of(phase)
    return max(low, min(high, int(seconds)))


def runtime_delay(seconds: int | None) -> int:
    return max(0, min(MAX_STEP_DELAY, int(seconds or 0)))


def apply_dwell(steps: list[dict[str, Any]]) -> list[str]:
    """clamp every step to its band, then fit the chain into the total budget.

    Idempotent: running it again on its own output changes nothing, which matters
    because a draft is re-validated when the operator imports it.
    """
    warnings: list[str] = []
    if not steps:
        return warnings

    prefilled = not any(step.get("delay_seconds") for step in steps)
    if prefilled:
        # the model ignored the pacing rules: fall back to the band minimums
        for step in steps:
            step["delay_seconds"] = band_of(str(step.get("phase") or ""))[0]
        warnings.append("no dwell estimates in the draft: phase minimums used")

    total = 0
    previous_phase: str | None = None
    for step in steps:
        phase = str(step.get("phase") or "")
        seconds = int(step.get("delay_seconds") or 0)
        clamped = clamp_step_delay(phase, seconds)
        if phase != previous_phase and clamped < PHASE_OPENING_FLOOR:
            clamped = PHASE_OPENING_FLOOR
        previous_phase = phase
        if clamped != seconds and not prefilled:
            warnings.append(
                f"step {step.get('order', '?')}: dwell {seconds}s clamped to {clamped}s"
            )
        step["delay_seconds"] = clamped
        total += clamped

    if total < MIN_TOTAL:
        total += _grow_openers(steps, MIN_TOTAL - total)
        if total >= MIN_TOTAL:
            warnings.append(
                f"dwell total raised to {total // 60} min to reach the 20 min floor"
            )
        else:
            warnings.append("under the 20 min floor even at the band maxima")
    elif total > MAX_TOTAL:
        before = total
        total -= _shrink_estimates(steps, total - MAX_TOTAL)
        if total > MAX_TOTAL:
            # report-sourced gaps are evidence and are never scaled
            warnings.append("over 90 min: report-sourced gaps kept as observed")
        else:
            warnings.append(
                f"dwell total scaled from {before // 60} to {total // 60} min"
            )

    return warnings


def _shrink_estimates(steps: list[dict[str, Any]], excess: int) -> int:
    """scale estimated gaps down proportionally, leaving observed ones alone."""
    shrinkable = [
        step
        for step in steps
        if step["delay_seconds"] > band_of(str(step.get("phase") or ""))[0]
        and step.get("delay_source") != REPORT_SOURCE
    ]
    weighted = sum(step["delay_seconds"] for step in shrinkable)
    if weighted <= 0:
        return 0

    factor = max(0.0, 1 - excess / weighted)
    saved = 0
    for step in shrinkable:
        low = band_of(str(step.get("phase") or ""))[0]
        before = step["delay_seconds"]
        step["delay_seconds"] = max(low, int(before * factor))
        saved += before - step["delay_seconds"]

    return saved


def _grow_openers(steps: list[dict[str, Any]], missing: int) -> int:
    """spend the missing seconds on phase-opening steps, up to their band max."""
    added = 0
    previous_phase: str | None = None

    for step in steps:
        phase = str(step.get("phase") or "")
        opens_phase = phase != previous_phase
        previous_phase = phase

        if not opens_phase or missing <= 0:
            continue

        room = band_of(phase)[1] - step["delay_seconds"]
        if room <= 0:
            continue

        spend = min(room, missing)
        step["delay_seconds"] += spend
        missing -= spend
        added += spend

    return added
