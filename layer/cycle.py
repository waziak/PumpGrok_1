"""One paper cycle: store discoveries, score strategies, enter at most once, exit on marks.

This process does not sign or broadcast. Hard caps stay in the risk engine.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from layer.db import ResearchDB, utc_now
from layer.config import Thresholds
from layer.paper import PaperEngine
from layer.reviews import build_reviews
from layer.risk import evaluate, feature_rows, normalize_candidate
from layer.strategy import match_strategy

# FAST may paper-enter on deterministic rules. RESEARCH never executes.
EXECUTION_CLASS = {
    "prebond-volume-regime": "FAST",
    "narrative-ceiling-exits": "RESEARCH",
    "video3-scalping-community-filters": "NORMAL",
}
STRATEGY_RANK = {
    "prebond-volume-regime": 0,
    "video3-scalping-community-filters": 1,
    "narrative-ceiling-exits": 2,
}
EXECUTABLE = {"FAST", "NORMAL"}
PUBLIC_WALLET = "2LmzxcxCfijZANvbiDrf8DcVRgUqY6PFwfB7wPVbsXCp"


def percentile(values: list[int], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return float(ordered[low])
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def run_cycle(
    db: ResearchDB,
    thresholds: Thresholds,
    strategies: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "malformed_cycle", "real_trades": False}
    grok = payload.get("grok") if isinstance(payload.get("grok"), dict) else {}
    if not grok:
        grok = {"available": False, "reason": "GROK_API_KEY_REQUIRED"}
    ordered = sorted(strategies, key=lambda item: STRATEGY_RANK.get(str(item.get("strategy_id")), 9))
    for strategy in ordered:
        strategy["execution_class"] = EXECUTION_CLASS.get(str(strategy.get("strategy_id")), "RESEARCH")

    engine = PaperEngine(db, thresholds)
    stored = 0
    skipped = 0
    entries: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    evaluations = 0
    qualified = 0
    qualified_live: list[dict[str, Any]] = []
    scope: list[dict[str, Any]] = []

    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        return {"ok": False, "error": "malformed_cycle", "real_trades": False}

    for raw in candidates:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        candidate, error = normalize_candidate(raw)
        if candidate is None:
            skipped += 1
            entries.append({"ok": False, "error": error, "traded": False})
            continue
        db.touch_candidate(
            {
                "candidate_id": candidate["candidate_id"],
                "mint": candidate["mint"],
                "symbol": candidate["symbol"],
                "name": candidate["name"],
                "source": candidate["source"],
                "program": candidate["program"],
                "market_cap_usd": candidate["market_cap_usd"],
                "observed_at": candidate["observed_at"],
                "payload_json": json.dumps(candidate["payload"], sort_keys=True),
            },
            status_if_new="discovered",
        )
        db.upsert_features(candidate["candidate_id"], feature_rows(candidate, candidate["source"]))
        stored += 1
        rows = _evaluate_all(ordered, candidate)
        chosen = _choose_executable(rows)
        risk_decision = None
        enter = None
        if chosen is not None:
            strategy = next(item for item in ordered if item["strategy_id"] == chosen["strategy_id"])
            if payload.get("qualify_only") is True:
                enter = _qualify_fast(
                    db,
                    thresholds,
                    candidate,
                    strategy,
                    payload,
                    now=now,
                )
            else:
                if strategy["execution_class"] == "FAST":
                    # Numbers only. Qualitative notes do not change the entry.
                    pass
                enter = engine.enter_discovered(candidate, strategy, now=now)
            risk_decision = "PASS" if enter.get("traded") or enter.get("qualified") else "REJECT" if enter.get("decision") == "REJECT" else None
            if enter.get("traded") or enter.get("qualified"):
                qualified += 1
                chosen["entry_reason"] = "risk_pass_and_rules_matched"
                chosen["rejection_reason"] = None
            elif enter.get("reason") == "duplicate_mint":
                chosen["rejection_reason"] = "duplicate_mint"
                chosen["entry_reason"] = None
            else:
                reasons = enter.get("reasons") or []
                chosen["rejection_reason"] = ",".join(reasons) if reasons else enter.get("reason") or "rejected"
                chosen["entry_reason"] = None
            for row in rows:
                if row is chosen:
                    continue
                if row["matched"] and row["execution_class"] in EXECUTABLE and (enter.get("traded") or enter.get("qualified")):
                    row["entry_reason"] = None
                    row["rejection_reason"] = "duplicate_mint"
        for row in rows:
            if row["execution_class"] == "RESEARCH" and row["matched"]:
                row["entry_reason"] = None
                row["rejection_reason"] = "research_no_execution"
            db.upsert_strategy_evaluation(row | {"observed_at": candidate.get("observed_at"), "updated_at": utc_now()})
            evaluations += 1
        if raw.get("enriched") is True:
            reviews = build_reviews(candidate, rows, grok, risk_decision if chosen else _risk_if_enriched(candidate, thresholds, db, now))
            for review in reviews:
                db.add_review(candidate["candidate_id"], review)
        if enter is not None:
            entries.append(enter)
            if enter.get("qualified") and enter.get("execution_class") == "FAST":
                qualified_live.append(enter)
        if payload.get("qualify_only") is True:
            scope.append(_scope_row(candidate, rows, enter))

    marks = _marks(payload)
    for position in db.open_positions():
        mark = marks.get(position["mint"])
        if not mark:
            continue
        result = engine.exit(position["position_id"], mark, now=now)
        exits.append(result)

    for sample in payload.get("samples") or []:
        if isinstance(sample, dict) and isinstance(sample.get("stage"), str) and isinstance(sample.get("duration_ms"), (int, float)):
            db.add_pipeline_sample(sample["stage"], int(sample["duration_ms"]))
    elapsed_ms = int((time.monotonic() - started) * 1000)
    db.add_pipeline_sample("cycle", elapsed_ms)

    health = {
        "mode": "paper",
        "real_trades": False,
        "private_key_exposed": False,
        "live_send_disabled": True,
        "grok_reason": grok.get("reason"),
        "grok_available": bool(grok.get("available")),
        "tokens_observed": stored,
        "qualified_entries": qualified,
        "wallet": payload.get("wallet") if isinstance(payload.get("wallet"), dict) else {"pubkey": PUBLIC_WALLET},
        "scanner": payload.get("scanner") if isinstance(payload.get("scanner"), dict) else {},
        "updated_at": utc_now(),
        "signal": "QUALIFIED" if qualified else "LIVE SCANNER ACTIVE — NO QUALIFIED SIGNAL YET",
    }
    if "reason" not in grok and not grok.get("available"):
        health["grok_reason"] = "GROK_API_KEY_REQUIRED"
    db.write_health(health)
    return {
        "ok": True,
        "mode": "paper",
        "real_trades": False,
        "sent": False,
        "private_key_exposed": False,
        "stored": stored,
        "skipped": skipped,
        "evaluations": evaluations,
        "entries": entries,
        "exits": exits,
        "qualified_entries": qualified,
        "qualified_live": qualified_live,
        "scope": scope,
        "open_positions": len(db.open_positions()),
        "open_mints": [row["mint"] for row in db.open_positions()],
        "paper_trades": db.counts()["paper_trades"],
        "paper_exits": db.counts()["trade_exits"],
        "grok_reason": health["grok_reason"],
        "live_send_disabled": True,
        "cycle_ms": elapsed_ms,
    }


def _payload_wallet_sol(payload: dict[str, Any]) -> float | None:
    wallet = payload.get("wallet")
    if not isinstance(wallet, dict):
        return None
    lamports = wallet.get("balanceLamports")
    if isinstance(lamports, int):
        return lamports / 1_000_000_000
    sol = wallet.get("sol")
    if isinstance(sol, (int, float)) and not isinstance(sol, bool):
        return float(sol)
    return None


def _qualify_fast(
    db: ResearchDB,
    thresholds: Thresholds,
    candidate: dict[str, Any],
    strategy: dict[str, Any],
    payload: dict[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Score a live candidate. Does not open a paper fill and does not broadcast."""
    mint = str(candidate.get("mint") or "")
    open_mints = {item for item in (payload.get("open_mints") or []) if isinstance(item, str)}
    base = {
        "ok": True,
        "traded": False,
        "qualified": False,
        "sent": False,
        "real_trades": False,
        "mode": "qualify",
        "mint": mint,
        "program": candidate.get("program"),
        "symbol": candidate.get("symbol"),
        "strategy_id": strategy.get("strategy_id"),
        "execution_class": strategy.get("execution_class"),
        "slippage_bps": candidate.get("slippage_bps"),
        "price_sol": candidate.get("features", {}).get("price_sol"),
        "candidate_id": candidate.get("candidate_id"),
    }
    if mint in open_mints:
        return base | {"decision": "REJECT", "reason": "duplicate_mint", "reasons": ["duplicate_mint"]}
    exposure = payload.get("open_exposure_sol")
    open_exposure = float(exposure) if isinstance(exposure, (int, float)) and not isinstance(exposure, bool) else 0.0
    result = evaluate(
        candidate,
        thresholds,
        now=now,
        open_exposure_sol=open_exposure,
        wallet_sol=_payload_wallet_sol(payload),
    )
    db.add_risk_decision(
        {
            "candidate_id": candidate["candidate_id"],
            "decision": result.decision,
            "reasons": result.reasons,
            "thresholds": thresholds.as_public_dict(),
            "override_attempt": result.override_attempt,
        }
    )
    if not result.passed or result.size_sol is None:
        return base | {"decision": "REJECT", "reasons": result.reasons}
    if strategy.get("execution_class") != "FAST":
        return base | {"decision": "REJECT", "reasons": ["not_fast"], "size_sol": result.size_sol}
    return base | {
        "qualified": True,
        "decision": "PASS",
        "reasons": [],
        "size_sol": result.size_sol,
    }


def _scope_row(
    candidate: dict[str, Any],
    rows: list[dict[str, Any]],
    enter: dict[str, Any] | None,
) -> dict[str, Any]:
    fast = next((row for row in rows if row["strategy_id"] == "prebond-volume-regime"), None)
    reasons: list[str] = []
    if enter and isinstance(enter.get("reasons"), list):
        reasons = [str(item) for item in enter["reasons"]]
    elif enter and enter.get("reason"):
        reasons = [str(enter["reason"])]
    elif fast and fast.get("rejection_reason"):
        reasons = [str(fast["rejection_reason"])]
    return {
        "mint": candidate.get("mint"),
        "symbol": candidate.get("symbol"),
        "program": candidate.get("program"),
        "strategy_id": None if fast is None else fast.get("strategy_id"),
        "execution_class": None if fast is None else fast.get("execution_class"),
        "matched": bool(fast and fast.get("matched")),
        "decision": "PASS" if enter and enter.get("qualified") else "REJECT",
        "reasons": reasons,
        "qualified": bool(enter and enter.get("qualified")),
    }


def _evaluate_all(strategies: list[dict[str, Any]], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for strategy in strategies:
        note = match_strategy(strategy, candidate["features"], candidate.get("program"))
        matched = bool(note["matched"])
        failed = list(note.get("failed") or [])
        unknown = list(note.get("unknown") or [])
        if matched:
            entry_reason = None
            rejection = None
        else:
            entry_reason = None
            rejection = ",".join(failed + unknown) if (failed or unknown) else "not_matched"
        rows.append(
            {
                "mint": candidate["mint"],
                "strategy_id": strategy.get("strategy_id"),
                "execution_class": strategy.get("execution_class") or "RESEARCH",
                "matched": matched,
                "rules_passed": ["entry"] if matched else [],
                "rules_failed": failed,
                "rules_unknown": unknown,
                "entry_reason": entry_reason,
                "rejection_reason": rejection,
            }
        )
    return rows


def _choose_executable(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if row["matched"] and row["execution_class"] in EXECUTABLE:
            return row
    return None


def _risk_if_enriched(candidate: dict[str, Any], thresholds: Thresholds, db: ResearchDB, now: datetime) -> str:
    result = evaluate(
        candidate,
        thresholds,
        now=now,
        open_exposure_sol=db.open_exposure_sol(),
        wallet_sol=db.simulated_cash(thresholds.paper_wallet_sol),
    )
    db.add_risk_decision(
        {
            "candidate_id": candidate["candidate_id"],
            "decision": result.decision,
            "reasons": result.reasons,
            "thresholds": thresholds.as_public_dict(),
            "override_attempt": result.override_attempt,
        }
    )
    return result.decision


def _marks(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    marks = payload.get("marks") or []
    if not isinstance(marks, list):
        return found
    for mark in marks:
        if not isinstance(mark, dict):
            continue
        mint = mark.get("mint")
        if isinstance(mint, str) and mint:
            found[mint] = mark
    return found
