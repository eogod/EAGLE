"""
Provides MITRE ATT&CK technique lookups and UKC phase mapping
for the EAGLE attack chain system.
Check UKC from https://www.unifiedkillchain.com/assets/The-Unified-Kill-Chain.pdf
"""

from __future__ import annotations

from typing import Any

from app.core.config import phases
from app.mitre_loader import TTP, APTChainIndex, AttackGraph

# unknown names sort before all phases
_PHASE_IDX: dict[str, int] = {p: i for i, p in enumerate(phases)}

UKC_PHASE_DESCRIPTIONS: dict[str, str] = {
    "Reconnaissance": (
        "Researching, identifying and selecting targets using active or "
        "passive reconnaissance."
    ),
    "Resource Development": (
        "Preparatory activities aimed at setting up the infrastructure "
        "required for the attack."
    ),
    "Delivery": (
        "Techniques resulting in the transmission of a weaponized object "
        "to the targeted environment."
    ),
    "Social Engineering": (
        "Techniques aimed at the manipulation of people to perform unsafe actions."
    ),
    "Exploitation": (
        "Techniques to exploit vulnerabilities in systems that may, amongst "
        "others, result in code execution."
    ),
    "Persistence": (
        "Any access, action or change to a system that gives an attacker "
        "persistent presence on the system."
    ),
    "Defense Evasion": (
        "Techniques an attacker may specifically use for evading detection "
        "or avoiding other defenses."
    ),
    "Command & Control": (
        "Techniques that allow attackers to communicate with controlled "
        "systems within a target network."
    ),
    "Pivoting": (
        "Tunneling traffic through a controlled system to other systems "
        "that are not directly accessible."
    ),
    "Discovery": (
        "Techniques that allow an attacker to gain knowledge about a system "
        "and its network environment."
    ),
    "Privilege Escalation": (
        "The result of techniques that provide an attacker with higher "
        "permissions on a system or network."
    ),
    "Execution": (
        "Techniques that result in execution of attacker-controlled code "
        "on a local or remote system."
    ),
    "Credential Access": (
        "Techniques resulting in the access of, or control over, system, "
        "service or domain credentials."
    ),
    "Lateral Movement": (
        "Techniques that enable an adversary to horizontally access and "
        "control other remote systems."
    ),
    "Collection": (
        "Techniques used to identify and gather data from a target network "
        "prior to exfiltration."
    ),
    "Exfiltration": (
        "Techniques that result or aid in an attacker removing data from "
        "a target network."
    ),
    "Impact": (
        "Techniques aimed at manipulating, interrupting or destroying the "
        "target system or data."
    ),
    "Objectives": (
        "Socio-technical objectives of an attack that are intended to "
        "achieve a strategic goal."
    ),
}

# keys are STIX kill_chain_phases phase_name values,
# direct mappings have a single UKC phase
ATTACK_TACTIC_TO_UKC: dict[str, list[str]] = {
    "reconnaissance": ["Reconnaissance"],
    "resource-development": ["Resource Development"],
    "initial-access": ["Delivery", "Social Engineering", "Exploitation"],
    "execution": ["Execution"],
    "persistence": ["Persistence"],
    "privilege-escalation": ["Privilege Escalation"],
    "defense-evasion": ["Defense Evasion"],
    "credential-access": ["Credential Access"],
    "discovery": ["Discovery"],
    "lateral-movement": ["Pivoting", "Lateral Movement"],
    "collection": ["Collection"],
    "command-and-control": ["Command & Control"],
    "exfiltration": ["Exfiltration"],
    "impact": ["Impact", "Objectives"],
}

_DELIVERY_KEYWORDS = {"delivery", "weaponized", "supply chain", "drive-by"}
_SOCIAL_KEYWORDS = {
    "phishing",
    "spearphishing",
    "social engineering",
    "pretexting",
    "user action",
    "click",
    "credential phish",
}
_EXPLOIT_KEYWORDS = {
    "exploit",
    "client exploit",
    "server exploit",
    "code execution",
    "vulnerability",
    "CVE",
    "overflow",
}
_PIVOT_KEYWORDS = {
    "pivot",
    "proxy",
    "tunnel",
    "port forward",
    "ssh tunnel",
    "socks",
    "network gateways",
}


def _phase_matches_technique(phase_name: str, ttp: TTP) -> bool:
    text = (ttp.name + " " + ttp.description).lower()

    if phase_name == "Delivery":
        return any(kw in text for kw in _DELIVERY_KEYWORDS)
    if phase_name == "Social Engineering":
        return any(kw in text for kw in _SOCIAL_KEYWORDS)
    if phase_name == "Exploitation":
        return any(kw in text for kw in _EXPLOIT_KEYWORDS)
    if phase_name == "Pivoting":
        return any(kw in text for kw in _PIVOT_KEYWORDS)
    return True


class TTPInfoService:
    """for MITRE ATT&CK techniques and map them to UKC"""

    def __init__(
        self,
        graph: AttackGraph,
        chain_index: APTChainIndex | None = None,
    ) -> None:
        self._graph = graph
        self._chain_index = chain_index

    def get_ttps_by_apt(self, apt_mitre_id: str) -> list[TTP]:
        """all TTPs used by given APT"""
        apt = self._graph.apts.get(apt_mitre_id)
        if apt is None:
            return []
        return [
            self._graph.ttps[tid]
            for tid in apt.technique_ids
            if tid in self._graph.ttps
        ]

    def get_ttp_info(self, ttp_mitre_id: str) -> TTP | None:
        return self._graph.ttps.get(ttp_mitre_id)

    def get_ttps_by_phase(self, phase_name: str) -> list[TTP]:
        tactic_ids = self._get_tactics_for_phase(phase_name)
        if not tactic_ids:
            return []

        tactic_set = set(tactic_ids)
        result: list[TTP] = []

        for ttp in self._graph.ttps.values():
            matched_tactics = tactic_set.intersection(ttp.tactic_ids)
            if not matched_tactics:
                continue
            # clarify phases that share a tactic
            shared_tactic = any(
                len(ATTACK_TACTIC_TO_UKC.get(t, [])) > 1 for t in matched_tactics
            )
            if shared_tactic and not _phase_matches_technique(phase_name, ttp):
                continue
            result.append(ttp)

        return result

    def get_phase_for_ttp(self, ttp_mitre_id: str) -> list[str]:
        ttp = self._graph.ttps.get(ttp_mitre_id)
        if ttp is None:
            return []

        matched: list[str] = []
        ttp_tactics = set(ttp.tactic_ids)
        for tactic, ukc_phases in ATTACK_TACTIC_TO_UKC.items():
            if tactic not in ttp_tactics:
                continue
            if len(ukc_phases) > 1:
                # tactic shared by several UKC phases
                matched.extend(
                    p for p in ukc_phases if _phase_matches_technique(p, ttp)
                )
            else:
                matched.extend(ukc_phases)

        return matched

    def suggest_next_ttps(
        self,
        current_mitre_id: str,
        limit: int = 10,
        same_phase_only: bool = False,
        target_phase: str | None = None,
    ) -> list[dict[str, Any]]:
        """suggest next techniques based on APT/TTP patterns"""
        if self._chain_index is None:
            return []

        current_ttp = self._graph.ttps.get(current_mitre_id)
        if current_ttp is None:
            return []

        current_phases = self.get_phase_for_ttp(current_mitre_id)
        if not current_phases:
            return []

        # current phase index = max index current UKC phases
        current_phase_idx = max(_PHASE_IDX.get(p, -1) for p in current_phases)

        suggestions = self._chain_index.get_all(current_mitre_id)

        results: list[dict[str, Any]] = []
        for s in suggestions:
            # reject if suggested phases before current phase
            suggested_max_idx = (
                max(_PHASE_IDX.get(p, -1) for p in s.ukc_phases) if s.ukc_phases else -1
            )
            if suggested_max_idx < current_phase_idx:
                continue

            # reject if no overlap with current phases
            if same_phase_only and not set(s.ukc_phases).intersection(current_phases):
                continue
            if target_phase and target_phase not in s.ukc_phases:
                continue

            ttp = self._graph.ttps.get(s.mitre_id)
            results.append(
                {
                    "mitre_id": s.mitre_id,
                    "name": s.name,
                    "description": ttp.description[:300]
                    if ttp and ttp.description
                    else "",
                    "apt_chain_count": s.apt_count,
                    "ukc_phases": s.ukc_phases,
                }
            )

            if len(results) >= limit:
                break

        return results

    @staticmethod
    def _get_tactics_for_phase(phase_name: str) -> list[str]:
        # invert the mapping: find which tactics belong to this phase
        result: list[str] = []
        for tactic, tactic_phases in ATTACK_TACTIC_TO_UKC.items():
            if phase_name in tactic_phases:
                result.append(tactic)
        return result
