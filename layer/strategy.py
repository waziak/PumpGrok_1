"""Paper-testable hypotheses.

Strategy files may describe entries and exits. They cannot raise hard caps,
lower the SOL reserve, or set status to live.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from layer.config import HARD_MAX_BUY_SOL, HARD_MAX_TOTAL_EXPOSURE_SOL, HARD_MIN_SOL_RESERVE, HARD_PAPER_BUY_SOL, PROGRAM_ALIASES
from layer.risk import UNKNOWN_TEXT


IMMUTABLE = {
    "MAX_BUY_SOL": HARD_MAX_BUY_SOL,
    "MAX_TOTAL_EXPOSURE_SOL": HARD_MAX_TOTAL_EXPOSURE_SOL,
    "MIN_SOL_RESERVE": HARD_MIN_SOL_RESERVE,
    "PAPER_BUY_SOL": HARD_PAPER_BUY_SOL,
}
IMMUTABLE_KEYS = set(IMMUTABLE) | {key.lower() for key in IMMUTABLE}


def load_strategies(directory: Path) -> list[dict[str, Any]]:
    loaded = []
    if not directory.exists():
        return loaded
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
        if not match:
            continue
        loaded.extend(_load_raw(path, match.group(1), text))
    for path in sorted(directory.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        notes = ""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("notes"), str):
                notes = parsed["notes"]
        except json.JSONDecodeError:
            notes = ""
        loaded.extend(_load_raw(path, text, notes or text))
    return loaded


def _load_raw(path: Path, raw_text: str, prose: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError:
        return [
            {
                "strategy_id": path.stem,
                "version": "invalid",
                "status": "paper-only",
                "hypothesis": "malformed strategy file ignored",
                "params": {},
                "attempted_limit_changes": [],
                "attempted_live_promotion": False,
                "source_path": str(path),
            }
        ]
    if isinstance(raw, dict):
        return [sanitize_strategy(raw, path, prose)]
    return []


def sanitize_strategy(raw: dict[str, Any], path: Path, text: str = "") -> dict[str, Any]:
    attempted: list[dict[str, Any]] = []
    cleaned = _strip_limits(raw, attempted)
    status = str(cleaned.get("status") or "paper-only").lower()
    promoted = status in {"live", "micro-live", "mainnet"}
    if promoted:
        attempted.append({"field": "status", "rejected": status, "kept": "paper-only"})
    hypothesis = _hypothesis_text(text, cleaned)
    params = {
        "entry": cleaned.get("entry") if isinstance(cleaned.get("entry"), dict) else {},
        "exits": cleaned.get("exits") if isinstance(cleaned.get("exits"), dict) else {},
        "chain": cleaned.get("chain") or "solana",
        "multi_chain": cleaned.get("multi_chain") or "out_of_scope_v1",
        "source_url": cleaned.get("source_url"),
        "claim_status": cleaned.get("claim_status") or "hypothesis",
        "evidence_status": cleaned.get("evidence_status") or "hypothesis_not_evidence",
    }
    for key in (
        "factual_filter_settings_claimed",
        "opinion_vs_measurable",
        "hypotheses",
        "feature_schema",
        "non_binding_research_note",
    ):
        if key in cleaned:
            params[key] = cleaned[key]
    if isinstance(params["exits"], dict):
        params["exits"]["blind_2x"] = False
    return {
        "strategy_id": str(cleaned.get("strategy_id") or path.stem),
        "version": str(cleaned.get("version") or "0.0.0"),
        "status": "paper-only",
        "hypothesis": hypothesis,
        "params": params,
        "entry": params["entry"],
        "exits": params["exits"],
        "chain": params["chain"],
        "multi_chain": params["multi_chain"],
        "claim_status": params["claim_status"],
        "evidence_status": params["evidence_status"],
        "factual_filter_settings_claimed": params.get("factual_filter_settings_claimed"),
        "opinion_vs_measurable": params.get("opinion_vs_measurable"),
        "hypotheses": params.get("hypotheses") or [],
        "feature_schema": params.get("feature_schema"),
        "non_binding_research_note": params.get("non_binding_research_note"),
        "attempted_limit_changes": attempted,
        "attempted_live_promotion": promoted,
        "source_path": str(path),
        "source_url": params["source_url"],
    }


def _strip_limits(value: Any, attempted: list[dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_strip_limits(item, attempted) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in IMMUTABLE_KEYS or key.upper() in IMMUTABLE:
            attempted.append(
                {
                    "field": key,
                    "rejected": item,
                    "kept": IMMUTABLE.get(key.upper(), IMMUTABLE.get(key)),
                }
            )
            continue
        out[key] = _strip_limits(item, attempted)
    return out


def _hypothesis_text(text: str, cleaned: dict[str, Any]) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("```"):
            return stripped[:500]
    return str(cleaned.get("notes") or "hypothesis")


def match_strategy(
    strategy: dict[str, Any],
    features: dict[str, Any],
    program: str | None,
) -> dict[str, Any]:
    """UNKNOWN features do not satisfy a hypothesis."""
    entry = strategy.get("entry") or strategy.get("params", {}).get("entry") or {}
    unknown: list[str] = []
    failed: list[str] = []
    chain = strategy.get("chain") or (strategy.get("params") or {}).get("chain") or "solana"
    if chain != "solana":
        failed.append("chain_out_of_scope")

    wanted = entry.get("program")
    if wanted:
        canon = PROGRAM_ALIASES.get(str(program or "").lower())
        if canon is None:
            unknown.append("program")
        elif canon != wanted:
            failed.append("program")

    _range_check(features, "market_cap_usd", entry.get("market_cap_usd_min"), entry.get("market_cap_usd_max"), unknown, failed)
    if "min_volume_sol_5m" in entry:
        volume = features.get("volume_sol_5m")
        if not isinstance(volume, (int, float)) or isinstance(volume, bool):
            unknown.append("volume_sol_5m")
        elif float(volume) < float(entry["min_volume_sol_5m"]):
            failed.append("volume_sol_5m")

    if entry.get("require_holder_reward_flag") is True:
        flag = features.get("holder_reward_flag", UNKNOWN_TEXT)
        if flag is True:
            pass
        elif flag is False:
            failed.append("holder_reward_flag")
        else:
            unknown.append("holder_reward_flag")

    unobserved: list[str] = []
    _apply_scalp_filters(entry, features, program, unknown, failed, unobserved)

    return {
        "strategy_id": strategy.get("strategy_id"),
        "matched": not unknown and not failed,
        "unknown": unknown,
        "failed": failed,
        "unobserved": unobserved,
        "claim_status": "hypothesis",
        "evidence_status": strategy.get("evidence_status") or "hypothesis_not_evidence",
    }


def _apply_scalp_filters(
    entry: dict[str, Any],
    features: dict[str, Any],
    program: str | None,
    unknown: list[str],
    failed: list[str],
    unobserved: list[str],
) -> None:
    """Video-3 style filters. Missing required inputs stay unknown. Optional social inputs do not get invented."""
    allow = entry.get("launchpad_allowlist")
    if isinstance(allow, list) and allow:
        allowed = {_launchpad_name(item) for item in allow}
        raw_pad = features.get("launchpad", UNKNOWN_TEXT)
        if raw_pad in (None, UNKNOWN_TEXT, ""):
            canon = PROGRAM_ALIASES.get(str(program or "").lower())
            if canon is None:
                unknown.append("launchpad")
            elif canon not in allowed:
                failed.append("launchpad")
        else:
            pad = _launchpad_name(raw_pad)
            if pad not in allowed:
                failed.append("launchpad")

    if "migration_allowlist" in entry or entry.get("skip_migrated") is True:
        status = _migration_name(features.get("migration_status", UNKNOWN_TEXT))
        if status is None:
            unknown.append("migration_status")
        else:
            allow_status = entry.get("migration_allowlist") or []
            if entry.get("skip_migrated") is True and status == "migrated":
                failed.append("migration_status")
            elif allow_status and status not in allow_status:
                failed.append("migration_status")
            elif status == "new_pair":
                _min_number(features, "market_cap_usd", entry.get("new_pair_min_market_cap_usd"), unknown, failed)
            elif status == "final_stretch":
                _final_stretch(entry.get("final_stretch") or {}, features, unknown, failed)
            elif status == "non_migrated":
                _min_number(features, "market_cap_usd", entry.get("new_pair_min_market_cap_usd"), unknown, failed)

    if "min_pullback_from_ath_pct" in entry:
        _min_number(features, "pullback_from_ath_pct", entry.get("min_pullback_from_ath_pct"), unknown, failed)

    _optional_label(
        features,
        "token_type",
        entry.get("token_type_allowlist"),
        entry.get("token_type_reject"),
        failed,
        unobserved,
    )
    _optional_label(
        features,
        "discussion_quality",
        entry.get("discussion_quality_allowlist"),
        entry.get("discussion_quality_reject"),
        failed,
        unobserved,
    )
    for key in entry.get("optional_bool_flags") or []:
        value = features.get(key, UNKNOWN_TEXT)
        if value in (None, UNKNOWN_TEXT, ""):
            unobserved.append(key)
        elif value is False:
            failed.append(key)
        elif value is not True:
            unobserved.append(key)


def _final_stretch(
    rules: dict[str, Any],
    features: dict[str, Any],
    unknown: list[str],
    failed: list[str],
) -> None:
    _max_number(features, "age_minutes", rules.get("age_max_minutes"), unknown, failed)
    _max_number(features, "dev_holding_pct", rules.get("max_dev_holding_pct"), unknown, failed)
    _max_number(features, "insiders_pct", rules.get("max_insiders_pct"), unknown, failed)
    _min_number(features, "prot_traders", rules.get("min_prot_traders"), unknown, failed)
    _min_number(features, "market_cap_usd", rules.get("min_market_cap_usd"), unknown, failed)


def _optional_label(
    features: dict[str, Any],
    key: str,
    allow: Any,
    reject: Any,
    failed: list[str],
    unobserved: list[str],
) -> None:
    if allow is None and reject is None:
        return
    value = features.get(key, UNKNOWN_TEXT)
    if value in (None, UNKNOWN_TEXT, ""):
        unobserved.append(key)
        return
    if isinstance(reject, list) and value in reject:
        failed.append(key)
        return
    if isinstance(allow, list) and value not in allow:
        failed.append(key)


def _min_number(
    features: dict[str, Any],
    key: str,
    floor: Any,
    unknown: list[str],
    failed: list[str],
) -> None:
    if floor is None:
        return
    value = features.get(key, UNKNOWN_TEXT)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        unknown.append(key)
        return
    if float(value) < float(floor):
        failed.append(key)


def _max_number(
    features: dict[str, Any],
    key: str,
    ceiling: Any,
    unknown: list[str],
    failed: list[str],
) -> None:
    if ceiling is None:
        return
    value = features.get(key, UNKNOWN_TEXT)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        unknown.append(key)
        return
    if float(value) > float(ceiling):
        failed.append(key)


def _launchpad_name(value: Any) -> str:
    token = str(value or "").strip().lower().replace(" ", "")
    if token in {"pump", "pump.fun", "pumpfun"}:
        return "pump"
    if token in {"meteora", "meteora-dlmm", "meteoradlmm"}:
        return "meteora"
    return token


def _migration_name(value: Any) -> str | None:
    if value in (None, UNKNOWN_TEXT, ""):
        return None
    token = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "non_migrated": "non_migrated",
        "not_migrated": "non_migrated",
        "bonding_curve": "non_migrated",
        "final_stretch": "final_stretch",
        "new_pair": "new_pair",
        "new_pairs": "new_pair",
        "migrated": "migrated",
        "graduated": "migrated",
    }
    return aliases.get(token)


def _range_check(
    features: dict[str, Any],
    key: str,
    low: Any,
    high: Any,
    unknown: list[str],
    failed: list[str],
) -> None:
    if low is None and high is None:
        return
    value = features.get(key, UNKNOWN_TEXT)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        unknown.append(key)
        return
    if low is not None and float(value) < float(low):
        failed.append(key)
    if high is not None and float(value) > float(high):
        failed.append(key)
