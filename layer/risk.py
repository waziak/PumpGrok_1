"""Deterministic risk gate.

A PASS is produced only by these checks. Agent text, CHIEF notes, and any
override field cannot turn a REJECT into a PASS.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from layer.config import PROGRAM_ALIASES, Thresholds, exceeds, sol_round


UNKNOWN_TEXT = "UNKNOWN"
BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
OVERRIDE_KEYS = {
    "risk_override",
    "override_risk",
    "ignore_risk",
    "chief_override",
    "veto_override",
    "force_clear",
    "bypass_risk",
    "risk_bypass",
}
OVERRIDE_VERDICTS = {"OVERRIDE", "VETO_OVERRIDE", "FORCE_PASS", "IGNORE_RISK", "KILL_REVERSED"}
FEATURE_KEYS = (
    "mint_authority",
    "freeze_authority",
    "liquidity_sol",
    "top_holder_pct",
    "top10_holder_pct",
    "route_ok",
    "price_impact_bps",
    "market_cap_usd",
    "volume_sol_5m",
    "price_sol",
    "holder_reward_flag",
    "structure_valid",
    "launchpad",
    "migration_status",
    "age_minutes",
    "dev_holding_pct",
    "insiders_pct",
    "prot_traders",
    "token_type",
    "pullback_from_ath_pct",
    "bundle_sol_balances_similar",
    "bundle_funding_clustered",
    "launch_candle_single_green",
    "ca_in_bio",
    "pinned_post_has_ca",
    "discussion_quality",
)
NUMERIC_FEATURES = {
    "liquidity_sol",
    "top_holder_pct",
    "top10_holder_pct",
    "price_impact_bps",
    "market_cap_usd",
    "volume_sol_5m",
    "price_sol",
    "age_minutes",
    "dev_holding_pct",
    "insiders_pct",
    "prot_traders",
    "pullback_from_ath_pct",
}
FRACTION_FEATURES = {"dev_holding_pct", "insiders_pct", "pullback_from_ath_pct"}
BOOL_FEATURES = {
    "route_ok",
    "holder_reward_flag",
    "structure_valid",
    "bundle_sol_balances_similar",
    "bundle_funding_clustered",
    "launch_candle_single_green",
    "ca_in_bio",
    "pinned_post_has_ca",
}
ENUM_FEATURES = {
    "migration_status": {
        "non_migrated": "non_migrated",
        "non-migrated": "non_migrated",
        "not_migrated": "non_migrated",
        "bonding_curve": "non_migrated",
        "bonding-curve": "non_migrated",
        "final_stretch": "final_stretch",
        "final-stretch": "final_stretch",
        "new_pair": "new_pair",
        "new-pair": "new_pair",
        "new_pairs": "new_pair",
        "migrated": "migrated",
        "graduated": "migrated",
    },
    "token_type": {
        "community": "community",
        "tweet_speculation": "tweet_speculation",
        "tweet-speculation": "tweet_speculation",
        "profile_project": "profile_project",
        "profile-project": "profile_project",
    },
    "discussion_quality": {
        "real_discussion": "real_discussion",
        "link_spam": "link_spam",
    },
    "launchpad": {
        "pump": "pump",
        "pump.fun": "pump",
        "pumpfun": "pump",
        "meteora": "meteora",
        "meteora-dlmm": "meteora",
    },
}
AUTHORITY_FEATURES = {"mint_authority", "freeze_authority"}

# Fixed-cost assumption used only to keep the reserve check conservative.
# These lamport figures are not a live quote.
RESERVE_FEE_SOL = sol_round((5_000 + 10_000 + 10_000) / 1_000_000_000)


@dataclass
class RiskResult:
    decision: str
    reasons: list[str] = field(default_factory=list)
    override_attempt: bool = False
    size_sol: float | None = None
    program: str | None = None
    malformed: bool = False

    @property
    def passed(self) -> bool:
        return self.decision == "PASS"


def contains_secret_material(value: Any) -> bool:
    """Detect key-like payloads without echoing them."""
    if isinstance(value, list) and len(value) in {32, 64}:
        if value and all(isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255 for item in value):
            return True
    if isinstance(value, dict):
        for key, item in value.items():
            if _key_looks_secret(str(key)) and _value_populated(item):
                return True
            if contains_secret_material(item):
                return True
    if isinstance(value, list):
        return any(contains_secret_material(item) for item in value)
    if isinstance(value, str):
        words = [part for part in value.strip().lower().split() if part.isalpha()]
        if len(words) in {12, 15, 18, 24} and all(len(word) >= 3 for word in words):
            # A phrase of that shape is refused even if it is not a real wordlist.
            if " " in value.strip() and value.strip() == value.strip().lower():
                return True
    return False


def _key_looks_secret(key: str) -> bool:
    folded = key.lower().replace("-", " ").replace("_", " ")
    return any(token in folded for token in ("private key", "secret key", "mnemonic", "seed phrase"))


def _value_populated(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip() == "":
        return False
    return True


def normalize_candidate(payload: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return a stored candidate, or (None, reason). Does not invent feature values."""
    if not isinstance(payload, dict):
        return None, "malformed_output"
    if contains_secret_material(payload):
        return None, "secret_material_refused"
    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        return None, "malformed_output"
    raw_features = payload.get("features", {})
    if raw_features is None:
        raw_features = {}
    if not isinstance(raw_features, dict):
        return None, "malformed_output"
    reviews = payload.get("agent_reviews", [])
    if reviews is None:
        reviews = []
    if not isinstance(reviews, list) or any(not isinstance(item, dict) for item in reviews):
        return None, "malformed_output"
    proposed = payload.get("proposed_size_sol", None)
    if proposed is not None and (isinstance(proposed, bool) or not isinstance(proposed, (int, float))):
        return None, "malformed_output"
    if isinstance(proposed, float) and (math.isnan(proposed) or math.isinf(proposed)):
        return None, "malformed_output"
    slippage = payload.get("slippage_bps", None)
    if slippage is not None and (isinstance(slippage, bool) or not isinstance(slippage, (int, float))):
        return None, "malformed_output"

    features: dict[str, Any] = {}
    for key in FEATURE_KEYS:
        if key not in raw_features:
            features[key] = UNKNOWN_TEXT
            continue
        features[key] = _canonicalize_feature(key, raw_features[key])

    mint = payload.get("mint", UNKNOWN_TEXT)
    if mint is None:
        mint = UNKNOWN_TEXT
    if not isinstance(mint, str):
        return None, "malformed_output"

    observed_at = payload.get("observed_at")
    if observed_at is not None and not isinstance(observed_at, str):
        return None, "malformed_output"

    program_raw = payload.get("program", UNKNOWN_TEXT)
    if program_raw is None:
        program_raw = UNKNOWN_TEXT
    if not isinstance(program_raw, str):
        return None, "malformed_output"

    symbol = payload.get("symbol", UNKNOWN_TEXT)
    if symbol is None or not isinstance(symbol, str) or not symbol.strip():
        symbol = UNKNOWN_TEXT

    market_cap = features.get("market_cap_usd")
    stored = {
        "candidate_id": candidate_id.strip(),
        "mint": mint.strip() if isinstance(mint, str) else UNKNOWN_TEXT,
        "symbol": symbol,
        "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
        "source": payload.get("source") if isinstance(payload.get("source"), str) else "UNKNOWN",
        "program": program_raw.strip().lower(),
        "market_cap_usd": market_cap if isinstance(market_cap, float) else None,
        "observed_at": observed_at,
        "proposed_size_sol": None if proposed is None else float(proposed),
        "slippage_bps": None if slippage is None else int(slippage),
        "features": features,
        "agent_reviews": reviews,
        "override_attempt": _override_attempt(payload),
        "strategy_id": payload.get("strategy_id") if isinstance(payload.get("strategy_id"), str) else None,
    }
    # Persist the original proposal minus nothing secret (already refused).
    stored["payload"] = payload
    return stored, None


def _canonicalize_feature(key: str, value: Any) -> Any:
    if _is_unknown_token(value):
        return UNKNOWN_TEXT
    if key in AUTHORITY_FEATURES:
        return _authority_token(value)
    if key in NUMERIC_FEATURES:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return UNKNOWN_TEXT
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return UNKNOWN_TEXT
        number = float(value)
        if key in FRACTION_FEATURES and not 0 <= number <= 1:
            return UNKNOWN_TEXT
        if number < 0:
            return UNKNOWN_TEXT
        return number
    if key in ENUM_FEATURES:
        if not isinstance(value, str):
            return UNKNOWN_TEXT
        token = value.strip().lower().replace(" ", "_")
        mapped = ENUM_FEATURES[key].get(token)
        if mapped:
            return mapped
        if key == "launchpad" and token:
            return token
        return UNKNOWN_TEXT
    if key in BOOL_FEATURES:
        if isinstance(value, bool):
            return value
        return UNKNOWN_TEXT
    return UNKNOWN_TEXT


def _is_unknown_token(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.strip().upper() in {"", "UNKNOWN", "BLIND", "UNAVAILABLE"}:
        return True
    return False


def _authority_token(value: Any) -> str:
    """Missing stays UNKNOWN. JSON null and the text 'revoked' mean authority is off."""
    if value is None:
        return "revoked"
    if isinstance(value, bool):
        return "present" if value else "revoked"
    if not isinstance(value, str):
        return UNKNOWN_TEXT
    token = value.strip().lower()
    if token in {"", "unknown", "blind", "unavailable"}:
        return UNKNOWN_TEXT
    if token in {"null", "none", "revoked", "disabled"}:
        return "revoked"
    return "present"


def _override_attempt(payload: dict[str, Any]) -> bool:
    for key in OVERRIDE_KEYS:
        if _truthy_override(payload.get(key)):
            return True
    source = str(payload.get("risk_verdict_source") or "").upper()
    verdict = str(payload.get("risk_verdict") or "").upper()
    if source == "CHIEF" and verdict in {"CLEAR", "PASS", "OVERRIDE"}:
        return True
    reviews = payload.get("agent_reviews") or []
    if isinstance(reviews, list):
        for review in reviews:
            if not isinstance(review, dict):
                continue
            agent = str(review.get("agent") or "").upper()
            review_verdict = str(review.get("verdict") or "").upper()
            if review_verdict in OVERRIDE_VERDICTS:
                return True
            if agent == "CHIEF" and review_verdict in {"CLEAR", "PASS", "OVERRIDE"}:
                return True
    return False


def _truthy_override(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "pass", "clear", "override"}:
        return True
    return False


def feature_rows(candidate: dict[str, Any], source: str) -> list[dict[str, Any]]:
    rows = []
    for key, value in candidate["features"].items():
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, float):
            text = json.dumps(value)
        else:
            text = str(value)
        rows.append(
            {
                "feature_key": key,
                "feature_value": text,
                "source": source,
                "observed_at": candidate.get("observed_at"),
            }
        )
    return rows


def evaluate(
    candidate: dict[str, Any],
    thresholds: Thresholds,
    *,
    now: datetime,
    open_exposure_sol: float,
    wallet_sol: float | None,
    execution_delay_sec: int = 0,
) -> RiskResult:
    reasons: list[str] = []
    features = candidate["features"]

    if candidate.get("override_attempt"):
        reasons.append("ai_risk_override")

    mint_reason = _mint_reason(candidate.get("mint"))
    if mint_reason:
        reasons.append(mint_reason)

    program = PROGRAM_ALIASES.get(str(candidate.get("program") or "").lower())
    if program is None:
        reasons.append("unsupported_program")

    for key, reason_present, reason_unknown in (
        ("freeze_authority", "freeze_authority_risk", "freeze_authority_unknown"),
        ("mint_authority", "mint_authority_risk", "mint_authority_unknown"),
    ):
        state = features.get(key, UNKNOWN_TEXT)
        if state == "present":
            reasons.append(reason_present)
        elif state != "revoked":
            reasons.append(reason_unknown)

    liquidity = features.get("liquidity_sol")
    if not isinstance(liquidity, float):
        reasons.append("liquidity_unknown")
    elif liquidity < thresholds.min_liquidity_sol:
        reasons.append("low_liquidity")

    top1 = features.get("top_holder_pct")
    if not isinstance(top1, float) or not 0 <= top1 <= 1:
        reasons.append("holder_concentration_unknown")
    elif top1 > thresholds.max_top_holder_pct:
        reasons.append("high_holder_concentration")

    top10 = features.get("top10_holder_pct")
    if not isinstance(top10, float) or not 0 <= top10 <= 1:
        reasons.append("holder_concentration_unknown")
    elif top10 > thresholds.max_top10_holder_pct:
        reasons.append("high_holder_concentration")

    observed = _parse_time(candidate.get("observed_at"))
    if observed is None:
        reasons.append("observed_at_unknown")
    else:
        age = (now - observed).total_seconds() + execution_delay_sec
        if age > thresholds.max_candidate_age_sec:
            reasons.append("stale_candidate")
        if age < -30:
            reasons.append("stale_candidate")

    route_ok = features.get("route_ok")
    impact = features.get("price_impact_bps")
    if route_ok is not True or not isinstance(impact, float):
        reasons.append("bad_routing")
    elif impact > thresholds.max_price_impact_bps or impact < 0:
        reasons.append("bad_routing")

    slippage = candidate.get("slippage_bps")
    if slippage is not None and (
        not isinstance(slippage, int) or slippage < 0 or slippage > thresholds.max_slippage_bps
    ):
        reasons.append("bad_routing")

    price = features.get("price_sol")
    if not isinstance(price, float) or price <= 0:
        reasons.append("price_unknown")

    proposed = candidate.get("proposed_size_sol")
    if proposed is None:
        size = thresholds.paper_buy_sol
    else:
        size = float(proposed)
    if size <= 0:
        reasons.append("malformed_output")
        size_ok = None
    elif exceeds(size, thresholds.max_buy_sol) or exceeds(size, thresholds.paper_buy_sol):
        reasons.append("excess_size")
        size_ok = None
    else:
        size_ok = sol_round(size)

    if size_ok is not None and exceeds(open_exposure_sol + size_ok, thresholds.max_total_exposure_sol):
        reasons.append("excess_exposure")

    if wallet_sol is None:
        reasons.append("sol_reserve")
    elif size_ok is not None:
        remaining = sol_round(wallet_sol - size_ok - RESERVE_FEE_SOL)
        if remaining < sol_round(thresholds.min_sol_reserve):
            reasons.append("sol_reserve")

    # Deduplicate while keeping order.
    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)

    decision = "PASS" if not deduped else "REJECT"
    return RiskResult(
        decision=decision,
        reasons=deduped,
        override_attempt=bool(candidate.get("override_attempt")),
        size_sol=size_ok if decision == "PASS" else None,
        program=program,
        malformed=False,
    )


def _mint_reason(mint: Any) -> str | None:
    if not isinstance(mint, str):
        return "unknown_mint"
    token = mint.strip()
    if token.upper() in {"", "UNKNOWN", "BLIND", "UNAVAILABLE", "NONE"}:
        return "unknown_mint"
    if not 32 <= len(token) <= 44:
        return "unknown_mint"
    if any(char not in BASE58 for char in token):
        return "unknown_mint"
    return None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
