"""Deterministic agent notes. Qualitative text stays UNKNOWN without a Grok result.

RISK comes from the numeric engine. CHIEF cannot clear a RISK veto.
"""

from __future__ import annotations

from typing import Any

from layer.risk import UNKNOWN_TEXT


ROLE_ORDER = ("SCOUT", "WHALE", "RUG", "SHILL", "SNIPER", "RISK", "EXIT", "CHIEF")


def build_reviews(
    candidate: dict[str, Any],
    evaluations: list[dict[str, Any]],
    grok: dict[str, Any] | None,
    risk_decision: str | None,
) -> list[dict[str, Any]]:
    features = candidate.get("features") or {}
    grok = grok or {}
    reason = grok.get("reason") if isinstance(grok.get("reason"), str) else None
    narrative = _note_for_mint(grok, str(candidate.get("mint") or ""))
    matched = [row["strategy_id"] for row in evaluations if row.get("matched")]
    risk_verdict = "PASS" if risk_decision == "PASS" else "VETO" if risk_decision == "REJECT" else "NOT_EVALUATED"
    chief = "ACK_VETO" if risk_verdict == "VETO" else "ACK"
    reviews = [
        {
            "agent": "SCOUT",
            "verdict": "OBSERVED",
            "deterministic": True,
            "launchpad": features.get("launchpad", UNKNOWN_TEXT),
            "migration_status": features.get("migration_status", UNKNOWN_TEXT),
            "source": candidate.get("source"),
        },
        {
            "agent": "WHALE",
            "verdict": "OBSERVED" if _known_number(features.get("top_holder_pct")) else "UNKNOWN",
            "deterministic": True,
            "top_holder_pct": features.get("top_holder_pct", UNKNOWN_TEXT),
            "top10_holder_pct": features.get("top10_holder_pct", UNKNOWN_TEXT),
        },
        {
            "agent": "RUG",
            "verdict": "OBSERVED",
            "deterministic": True,
            "mint_authority": features.get("mint_authority", UNKNOWN_TEXT),
            "freeze_authority": features.get("freeze_authority", UNKNOWN_TEXT),
        },
        {
            "agent": "SHILL",
            "verdict": "GROK_API_KEY_REQUIRED" if reason == "GROK_API_KEY_REQUIRED" else "UNKNOWN" if not narrative else "NOTE",
            "deterministic": reason == "GROK_API_KEY_REQUIRED" or not narrative,
            "qualitative": narrative or UNKNOWN_TEXT,
            "reason": reason or (None if narrative else "qualitative_unknown"),
        },
        {
            "agent": "SNIPER",
            "verdict": "MATCH" if matched else "NO_MATCH",
            "deterministic": True,
            "strategies": matched,
        },
        {
            "agent": "RISK",
            "verdict": risk_verdict,
            "deterministic": True,
            "veto": risk_verdict == "VETO",
        },
        {
            "agent": "EXIT",
            "verdict": "STANDBY",
            "deterministic": True,
            "note": "exits are evaluated from live marks on open paper positions",
        },
        {
            "agent": "CHIEF",
            "verdict": chief,
            "deterministic": True,
            "cannot_override_risk": True,
        },
    ]
    return reviews


def _note_for_mint(grok: dict[str, Any], mint: str) -> str | None:
    notes = grok.get("notes")
    if not isinstance(notes, list):
        return None
    for item in notes:
        if isinstance(item, dict) and item.get("mint") == mint:
            text = item.get("narrative")
            if isinstance(text, str) and text.strip() and "KEY" not in text.upper():
                return text.strip()[:500]
    return None


def _known_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
