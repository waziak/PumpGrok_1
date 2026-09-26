"""Ingest candidate JSON. Missing features stay UNKNOWN. No network calls."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from layer.config import Thresholds
from layer.db import DuplicateCandidate, ResearchDB
from layer.risk import evaluate, feature_rows, normalize_candidate


def load_payloads(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        data = json.loads(stripped)
        if not isinstance(data, list):
            raise ValueError("array_expected")
        return data
    if stripped.startswith("{"):
        return [json.loads(stripped)]
    rows = []
    for line in stripped.splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def ingest(
    db: ResearchDB,
    payloads: list[Any],
    thresholds: Thresholds,
    *,
    evaluate_risk: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    results = []
    for payload in payloads:
        results.append(_one(db, payload, thresholds, evaluate_risk=evaluate_risk, now=now))
    ok = all(item.get("ok") for item in results)
    return {"ok": ok, "count": len(results), "results": results, "real_trades": False, "mode": "paper"}


def _one(
    db: ResearchDB,
    payload: Any,
    thresholds: Thresholds,
    *,
    evaluate_risk: bool,
    now: datetime,
) -> dict[str, Any]:
    candidate, error = normalize_candidate(payload)
    if candidate is None:
        return {"ok": False, "error": error, "stored": False}
    if db.candidate_exists(candidate["candidate_id"]):
        return {
            "ok": False,
            "error": "duplicate_candidate",
            "candidate_id": candidate["candidate_id"],
            "stored": False,
        }
    decision = None
    reasons: list[str] = []
    status = "open"
    if evaluate_risk:
        wallet = db.simulated_cash(thresholds.paper_wallet_sol)
        result = evaluate(
            candidate,
            thresholds,
            now=now,
            open_exposure_sol=db.open_exposure_sol(),
            wallet_sol=wallet,
            execution_delay_sec=0,
        )
        decision = result.decision
        reasons = result.reasons
        status = "risk_pass" if result.passed else "rejected"
    try:
        db.insert_candidate(
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
                "status": status,
            }
        )
    except DuplicateCandidate:
        return {
            "ok": False,
            "error": "duplicate_candidate",
            "candidate_id": candidate["candidate_id"],
            "stored": False,
        }
    db.upsert_features(candidate["candidate_id"], feature_rows(candidate, candidate["source"]))
    for review in candidate["agent_reviews"]:
        db.add_review(candidate["candidate_id"], review)
    if decision is not None:
        db.add_risk_decision(
            {
                "candidate_id": candidate["candidate_id"],
                "decision": decision,
                "reasons": reasons,
                "thresholds": thresholds.as_public_dict(),
                "override_attempt": candidate["override_attempt"],
            }
        )
    return {
        "ok": True,
        "stored": True,
        "candidate_id": candidate["candidate_id"],
        "decision": decision,
        "reasons": reasons,
        "traded": False,
    }


def scan_path(db: ResearchDB, path: Path, thresholds: Thresholds, *, evaluate_risk: bool = True) -> dict[str, Any]:
    try:
        payloads = load_payloads(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "error": "malformed_output", "detail": exc.__class__.__name__}
    return ingest(db, payloads, thresholds, evaluate_risk=evaluate_risk)


def paper_path(
    db: ResearchDB,
    path: Path,
    thresholds: Thresholds,
    *,
    strategy: dict[str, Any] | None = None,
    desk: Path | None = None,
) -> dict[str, Any]:
    engine = PaperEngine(db, thresholds, desk=desk)
    try:
        payloads = load_payloads(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"ok": False, "error": "malformed_output", "real_trades": False}
    results = [engine.buy(item, strategy=strategy) for item in payloads]
    return {
        "ok": all(item.get("ok") for item in results),
        "results": results,
        "mode": "paper",
        "real_trades": False,
    }
