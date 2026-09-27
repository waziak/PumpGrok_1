"""Paper cycle: strategies, one buy per mint, automatic exits, restart."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from layer.config import REPO_ROOT, load_thresholds
from layer.cycle import percentile, run_cycle
from layer.db import ResearchDB
from layer.strategy import load_strategies


NOW = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
MINT = "9PaperMint1111111111111111111111111111111"


def strategies():
    return load_strategies(REPO_ROOT / "research" / "strategies")


def passing(**overrides):
    payload = {
        "candidate_id": MINT,
        "mint": MINT,
        "symbol": "PAPER",
        "name": "Paper",
        "source": "pump.fun",
        "program": "pump",
        "observed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposed_size_sol": 0.005,
        "slippage_bps": 100,
        "enriched": True,
        "features": {
            "mint_authority": "revoked",
            "freeze_authority": "revoked",
            "liquidity_sol": 20,
            "top_holder_pct": 0.08,
            "top10_holder_pct": 0.3,
            "route_ok": True,
            "price_impact_bps": 40,
            "market_cap_usd": 12000,
            "volume_sol_5m": 8,
            "price_sol": 0.00002,
            "launchpad": "pump",
            "migration_status": "new_pair",
            "pullback_from_ath_pct": 0.5,
            "holder_reward_flag": True,
        },
    }
    features = overrides.pop("features", None)
    if features:
        payload["features"].update(features)
    payload.update(overrides)
    return payload


class CycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cycle.sqlite"
        self.db = ResearchDB(self.path)

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_wal_and_percentiles(self) -> None:
        mode = self.db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(str(mode).lower(), "wal")
        self.assertIsNone(percentile([], 0.5))
        self.assertEqual(percentile([10], 0.95), 10)

    def test_strategies_are_independent_and_do_not_double_buy(self) -> None:
        first = run_cycle(
            self.db,
            load_thresholds({}),
            strategies(),
            {"candidates": [passing()], "grok": {"available": False, "reason": "GROK_API_KEY_REQUIRED"}},
            now=NOW,
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first["qualified_entries"], 1)
        self.assertEqual(first["paper_trades"], 1)
        self.assertEqual(first["grok_reason"], "GROK_API_KEY_REQUIRED")
        rows = self.db.conn.execute(
            "SELECT strategy_id, execution_class, matched, entry_reason, rejection_reason FROM strategy_evaluations"
        ).fetchall()
        by_id = {row["strategy_id"]: row for row in rows}
        self.assertEqual(by_id["prebond-volume-regime"]["execution_class"], "FAST")
        self.assertEqual(by_id["prebond-volume-regime"]["entry_reason"], "risk_pass_and_rules_matched")
        self.assertEqual(by_id["video3-scalping-community-filters"]["execution_class"], "NORMAL")
        self.assertEqual(by_id["video3-scalping-community-filters"]["rejection_reason"], "duplicate_mint")
        self.assertEqual(int(by_id["video3-scalping-community-filters"]["matched"]), 1)
        self.db.close()
        self.db = ResearchDB(self.path)
        self.assertEqual(by_id["narrative-ceiling-exits"]["execution_class"], "RESEARCH")
        failed = json.loads(
            self.db.conn.execute(
                "SELECT rules_failed_json FROM strategy_evaluations WHERE strategy_id = ?",
                ("narrative-ceiling-exits",),
            ).fetchone()[0]
        )
        self.assertIn("market_cap_usd", failed)

        reviews = self.db.conn.execute(
            "SELECT review_json FROM agent_reviews WHERE candidate_id = ?",
            (MINT,),
        ).fetchall()
        blob = " ".join(row[0] for row in reviews)
        self.assertIn("GROK_API_KEY_REQUIRED", blob)
        self.assertIn("cannot_override_risk", blob)
        self.assertNotIn("sk-", blob)

        again = run_cycle(
            self.db,
            load_thresholds({}),
            strategies(),
            {"candidates": [passing()], "grok": {"available": False, "reason": "GROK_API_KEY_REQUIRED"}},
            now=NOW,
        )
        self.assertEqual(again["paper_trades"], 1)
        self.assertEqual(again["qualified_entries"], 0)

    def test_auto_exit_and_restart(self) -> None:
        run_cycle(
            self.db,
            load_thresholds({}),
            strategies(),
            {"candidates": [passing()], "samples": [{"stage": "discover", "duration_ms": 25}]},
            now=NOW,
        )
        self.db.close()
        self.db = ResearchDB(self.path)
        self.assertEqual(len(self.db.open_positions()), 1)
        exited = run_cycle(
            self.db,
            load_thresholds({}),
            strategies(),
            {"candidates": [], "marks": [{"mint": MINT, "price_sol": 0.000001, "liquidity_sol": 20, "volume_sol_5m": 8}]},
            now=NOW,
        )
        self.assertTrue(any(item.get("exited") for item in exited["exits"]))
        self.assertEqual(self.db.open_positions(), [])
        self.assertGreaterEqual(self.db.counts()["trade_exits"], 1)
        durations = self.db.pipeline_durations("discover")
        self.assertEqual(durations, [25])
        self.assertIsNotNone(percentile(self.db.pipeline_durations("cycle"), 0.5))

    def test_risk_veto_blocks_entry(self) -> None:
        payload = passing()
        payload["risk_override"] = True
        payload["agent_reviews"] = [{"agent": "CHIEF", "verdict": "CLEAR"}]
        result = run_cycle(self.db, load_thresholds({}), strategies(), {"candidates": [payload]}, now=NOW)
        self.assertEqual(result["paper_trades"], 0)
        self.assertEqual(result["qualified_entries"], 0)
        reasons = result["entries"][0]["reasons"]
        self.assertIn("ai_risk_override", reasons)

    def test_qualify_only_does_not_paper_trade_and_keeps_risk_veto(self) -> None:
        funded = {
            "candidates": [passing()],
            "qualify_only": True,
            "wallet": {"balanceLamports": 1_000_000_000},
            "open_exposure_sol": 0,
            "grok": {"available": False, "reason": "GROK_API_KEY_REQUIRED"},
        }
        passed = run_cycle(self.db, load_thresholds({}), strategies(), funded, now=NOW)
        self.assertEqual(passed["paper_trades"], 0)
        self.assertEqual(passed["sent"], False)
        self.assertEqual(passed["real_trades"], False)
        self.assertEqual(passed["qualified_entries"], 1)
        self.assertEqual(passed["qualified_live"][0]["execution_class"], "FAST")
        self.assertEqual(passed["scope"][0]["decision"], "PASS")
        veto = passing(candidate_id="other", mint="8PaperMint1111111111111111111111111111111")
        veto["features"]["liquidity_sol"] = 1
        blocked = run_cycle(
            self.db,
            load_thresholds({}),
            strategies(),
            {"candidates": [veto], "qualify_only": True, "wallet": {"balanceLamports": 1_000_000_000}},
            now=NOW,
        )
        self.assertEqual(blocked["qualified_entries"], 0)
        self.assertEqual(blocked["paper_trades"], 0)
        self.assertIn("low_liquidity", blocked["scope"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
