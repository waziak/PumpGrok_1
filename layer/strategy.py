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
        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError:
            loaded.append(
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
            )
            continue
        if isinstance(raw, dict):
            loaded.append(sanitize_strategy(raw, path, text))
    return loaded


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
    }
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

    return {
        "strategy_id": strategy.get("strategy_id"),
        "matched": not unknown and not failed,
        "unknown": unknown,
        "failed": failed,
        "claim_status": "hypothesis",
    }


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
