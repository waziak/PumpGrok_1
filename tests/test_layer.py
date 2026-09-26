"""Paper-layer tests. No network and no live transactions."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from layer.config import (
    HARD_MAX_BUY_SOL,
    HARD_MIN_SOL_RESERVE,
    REPO_ROOT,
    Thresholds,
    load_thresholds,
)
from layer.db import DuplicateReceipt, ResearchDB
from layer.paper import (
    DEFAULT_EXIT_PARAMS,
    PaperEngine,
    evaluate_exits,
    _maybe_tighten_ceiling,
)
from layer.research_run import run_daily
from layer.risk import evaluate, normalize_candidate
from layer.scan import ingest
from layer.strategy import load_strategies, match_strategy, sanitize_strategy


NOW = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)
MINT = "9PaperMint1111111111111111111111111111111"


def thresholds() -> Thresholds:
    return load_thresholds({})


def candidate(**overrides):
    payload = {
        "candidate_id": "cand-1",
        "mint": MINT,
        "symbol": "PAPER",
        "source": "scout",
        "program": "pump",
        "observed_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proposed_size_sol": 0.005,
        "slippage_bps": 100,
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
        },
        "agent_reviews": [{"agent": "SCOUT", "verdict": "LEAD"}],
    }
    features = overrides.pop("features", None)
    if features:
        payload["features"].update(features)
    payload.update(overrides)
    return payload


class LayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db = ResearchDB(Path(self.tmp.name) / "research.sqlite")
        self.engine = PaperEngine(self.db, thresholds(), desk=Path(self.tmp.name) / "desk")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_malformed_candidate(self) -> None:
        result = self.engine.buy("not-a-candidate", now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "malformed_output")
        self.assertEqual(self.db.counts()["candidates"], 0)
        missing = self.engine.buy({"candidate_id": "x", "features": ["bad"]}, now=NOW)
        self.assertEqual(missing["error"], "malformed_output")

    def test_duplicate_candidate(self) -> None:
        first = self.engine.buy(candidate(), now=NOW)
        self.assertTrue(first["traded"])
        second = self.engine.buy(candidate(), now=NOW)
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "duplicate_candidate")
        self.assertEqual(self.db.counts()["paper_trades"], 1)

    def test_stale_candidate(self) -> None:
        old = (NOW - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = evaluate(
            normalize_candidate(candidate(observed_at=old))[0],
            thresholds(),
            now=NOW,
            open_exposure_sol=0,
            wallet_sol=0.1,
        )
        self.assertEqual(result.decision, "REJECT")
        self.assertIn("stale_candidate", result.reasons)

    def test_high_holder_concentration(self) -> None:
        result = self._risk(features={"top_holder_pct": 0.4})
        self.assertIn("high_holder_concentration", result.reasons)
        self.assertEqual(result.decision, "REJECT")

    def test_authority_risk(self) -> None:
        freeze = self._risk(features={"freeze_authority": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"})
        mint = self._risk(features={"mint_authority": "present"})
        unknown = self._risk(features={"freeze_authority": "UNKNOWN"})
        self.assertIn("freeze_authority_risk", freeze.reasons)
        self.assertIn("mint_authority_risk", mint.reasons)
        self.assertIn("freeze_authority_unknown", unknown.reasons)
        self.assertNotEqual(freeze.decision, "PASS")

    def test_low_liquidity_and_unknown_not_fabricated(self) -> None:
        low = self._risk(features={"liquidity_sol": 1.5})
        self.assertIn("low_liquidity", low.reasons)
        payload = candidate()
        del payload["features"]["liquidity_sol"]
        stored, _ = normalize_candidate(payload)
        self.assertEqual(stored["features"]["liquidity_sol"], "UNKNOWN")
        unknown = evaluate(stored, thresholds(), now=NOW, open_exposure_sol=0, wallet_sol=0.1)
        self.assertIn("liquidity_unknown", unknown.reasons)
        self.assertNotIn("low_liquidity", unknown.reasons)
        scan = ingest(self.db, [payload | {"candidate_id": "cand-unknown-liq"}], thresholds(), now=NOW)
        self.assertTrue(scan["results"][0]["stored"])
        self.assertEqual(self.db.feature_map("cand-unknown-liq")["liquidity_sol"], "UNKNOWN")

    def test_excess_position_and_exposure(self) -> None:
        size = self._risk(proposed_size_sol=0.02)
        self.assertIn("excess_size", size.reasons)
        exposure = evaluate(
            normalize_candidate(candidate(candidate_id="cand-exp"))[0],
            thresholds(),
            now=NOW,
            open_exposure_sol=0.028,
            wallet_sol=0.1,
        )
        self.assertIn("excess_exposure", exposure.reasons)
        reserve = evaluate(
            normalize_candidate(candidate(candidate_id="cand-res"))[0],
            thresholds(),
            now=NOW,
            open_exposure_sol=0,
            wallet_sol=0.024,
        )
        self.assertIn("sol_reserve", reserve.reasons)

    def test_ai_risk_override_and_chief_cannot_clear(self) -> None:
        overridden = self.engine.buy(candidate(candidate_id="cand-over", risk_override=True), now=NOW)
        self.assertFalse(overridden["traded"])
        self.assertIn("ai_risk_override", overridden["reasons"])
        self.assertEqual(overridden["decision"], "REJECT")
        chief = candidate(
            candidate_id="cand-chief",
            features={"liquidity_sol": 1.5},
            risk_verdict="CLEAR",
            risk_verdict_source="CHIEF",
            agent_reviews=[{"agent": "CHIEF", "verdict": "OVERRIDE"}],
        )
        result = self.engine.buy(chief, now=NOW)
        self.assertEqual(result["decision"], "REJECT")
        self.assertIn("low_liquidity", result["reasons"])
        self.assertIn("ai_risk_override", result["reasons"])
        self.assertNotEqual(result["decision"], "PASS")

    def test_unknown_mint_unsupported_program_bad_route(self) -> None:
        self.assertIn("unknown_mint", self._risk(mint="UNKNOWN").reasons)
        self.assertIn("unsupported_program", self._risk(program="raydium-generic").reasons)
        self.assertIn("bad_routing", self._risk(features={"route_ok": False}).reasons)
        self.assertIn("bad_routing", self._risk(features={"price_impact_bps": 900}).reasons)

    def test_paper_buy_partial_and_full_exit(self) -> None:
        bought = self.engine.buy(candidate(candidate_id="cand-buy"), now=NOW)
        self.assertTrue(bought["traded"])
        self.assertFalse(bought["sent"])
        self.assertEqual(bought["size_sol"], 0.005)
        self.assertGreater(bought["costs"]["slippage_sol"], 0)
        self.assertGreater(bought["costs"]["pump_fee_sol"], 0)
        self.assertGreater(bought["costs"]["jito_tip_sol"], 0)
        self.assertFalse(bought["costs"]["jito_tip_sent"])
        self.assertEqual(bought["mode"], "paper")
        journal = Path(bought["journal"])
        self.assertIn("Size SOL: 0.005", journal.read_text(encoding="utf-8"))

        position = self.db.get_position(bought["position_id"])
        entry = float(position["entry_price"])
        partial = self.engine.exit(
            bought["position_id"],
            {"price_sol": entry * 1.3, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
        )
        self.assertTrue(partial["partial"])
        self.assertEqual(partial["family"], "partial_profit")
        self.assertAlmostEqual(partial["remaining_fraction"], 0.5, places=6)

        stopped = self.engine.exit(
            bought["position_id"],
            {"price_sol": entry * 0.7, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
        )
        self.assertTrue(stopped["full"])
        self.assertEqual(stopped["family"], "fixed_stop")
        self.assertEqual(self.db.get_position(bought["position_id"])["status"], "closed")

    def test_full_exit_and_not_blind_2x(self) -> None:
        bought = self.engine.buy(candidate(candidate_id="cand-full"), now=NOW)
        entry = float(self.db.get_position(bought["position_id"])["entry_price"])
        wide = {
            **DEFAULT_EXIT_PARAMS,
            "partial_r": 10,
            "target_r": 10,
            "fixed_stop_pct": 0.25,
            "trail_pct": 0.2,
        }
        position = self.db.get_position(bought["position_id"])
        position["state"]["exit_params"] = wide
        self.db.upsert_position(
            {
                **{key: position[key] for key in (
                    "position_id", "candidate_id", "mint", "size_sol", "tokens", "entry_price",
                    "remaining_fraction", "status", "opened_at", "high_water_price", "strategy_id",
                )},
                "updated_at": position["updated_at"],
                "state": position["state"],
            }
        )
        doubled = entry * 2
        signals = evaluate_exits(
            self.db.get_position(bought["position_id"]),
            {"price_sol": doubled, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
            wide,
        )
        self.assertFalse(any(item["family"] in {"blind_2x", "price_2x"} for item in signals))
        held = self.engine.exit(
            bought["position_id"],
            {"price_sol": doubled, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
        )
        self.assertFalse(held["exited"])

        r_hit = evaluate_exits(
            self.db.get_position(bought["position_id"]),
            {"price_sol": entry * 1.5, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
            {**DEFAULT_EXIT_PARAMS, "partial_r": 3, "target_r": 2, "trail_pct": 0.5},
        )
        self.assertIn("fixed_r_multiple", [item["family"] for item in r_hit])

    def test_unknown_inputs_do_not_fire_exits(self) -> None:
        bought = self.engine.buy(candidate(candidate_id="cand-unk-exit"), now=NOW)
        position = self.db.get_position(bought["position_id"])
        signals = evaluate_exits(position, {}, DEFAULT_EXIT_PARAMS)
        self.assertEqual(signals, [])
        momentum = evaluate_exits(
            position,
            {"price_sol": float(position["entry_price"]) * 0.95, "structure_valid": True, "liquidity_sol": 20},
            DEFAULT_EXIT_PARAMS,
        )
        self.assertNotIn("momentum_loss", [item["family"] for item in momentum])
        self.assertNotIn("volume_breakdown", [item["family"] for item in momentum])

    def test_other_exit_families_and_ceiling(self) -> None:
        position = {
            "entry_price": 1.0,
            "high_water_price": 1.2,
            "remaining_fraction": 1.0,
            "state": {"entry_liquidity_sol": 20.0, "entry_volume_sol_5m": 10.0, "tokens_initial": 1.0},
        }
        liq = evaluate_exits(
            position,
            {"price_sol": 1.05, "liquidity_sol": 5, "volume_sol_5m": 10, "structure_valid": True},
            {**DEFAULT_EXIT_PARAMS, "partial_r": 99, "target_r": 99},
        )
        self.assertIn("liquidity_deterioration", [item["family"] for item in liq])
        volume = evaluate_exits(
            position,
            {"price_sol": 1.05, "liquidity_sol": 20, "volume_sol_5m": 0.2, "structure_valid": True},
            {**DEFAULT_EXIT_PARAMS, "partial_r": 99, "target_r": 99, "trail_pct": 0.5},
        )
        families = [item["family"] for item in volume]
        self.assertIn("volume_breakdown", families)
        self.assertIn("momentum_loss", families)
        structure = evaluate_exits(
            position,
            {"price_sol": 1.05, "liquidity_sol": 20, "volume_sol_5m": 10, "structure_valid": False},
            DEFAULT_EXIT_PARAMS,
        )
        self.assertIn("structure_invalidation", [item["family"] for item in structure])

        params = {
            **DEFAULT_EXIT_PARAMS,
            "trail_pct": 0.18,
            "ceiling_adaptive": True,
            "market_cap_ceiling": 50000,
            "partial_r": 99,
            "target_r": 99,
        }
        snapshot = {"price_sol": 1.08, "market_cap_usd": 48000, "liquidity_sol": 20, "volume_sol_5m": 10, "structure_valid": True}
        tightened = _maybe_tighten_ceiling(params, snapshot, position)
        fired = [item["family"] for item in evaluate_exits(position, snapshot, tightened)]
        quiet = [item["family"] for item in evaluate_exits(position, snapshot, params | {"ceiling_adaptive": False})]
        self.assertIn("trailing_stop", fired)
        self.assertNotIn("trailing_stop", quiet)

    def test_receipt_duplicate_and_restart_recovery(self) -> None:
        self.db.add_receipt(
            {
                "receipt_id": "paper:once",
                "candidate_id": "cand-1",
                "mode": "paper",
                "status": "simulated",
                "payload": {"sent": False},
            }
        )
        with self.assertRaises(DuplicateReceipt):
            self.db.add_receipt(
                {
                    "receipt_id": "paper:once",
                    "candidate_id": "cand-1",
                    "mode": "paper",
                    "status": "simulated",
                    "payload": {"sent": False},
                }
            )
        bought = self.engine.buy(candidate(candidate_id="cand-restart"), now=NOW)
        path = self.db.path
        self.db.close()
        restored = ResearchDB(path)
        self.db = restored
        position = restored.get_position(bought["position_id"])
        self.assertEqual(position["status"], "open")
        self.assertAlmostEqual(position["remaining_fraction"], 1.0)
        engine = PaperEngine(restored, thresholds())
        entry = float(position["entry_price"])
        closed = engine.exit(
            bought["position_id"],
            {"price_sol": entry * 0.5, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
        )
        self.assertTrue(closed["full"])

    def test_hard_caps_and_strategy_cannot_weaken_or_go_live(self) -> None:
        loosened = load_thresholds(
            {
                "MAX_BUY_SOL": "1",
                "MAX_TOTAL_EXPOSURE_SOL": "5",
                "MIN_SOL_RESERVE": "0",
                "PAPER_BUY_SOL": "1",
                "MAX_TOP_HOLDER_PCT": "0.99",
            }
        )
        self.assertEqual(loosened.max_buy_sol, HARD_MAX_BUY_SOL)
        self.assertEqual(loosened.max_total_exposure_sol, 0.03)
        self.assertGreaterEqual(loosened.min_sol_reserve, HARD_MIN_SOL_RESERVE)
        self.assertEqual(loosened.paper_buy_sol, 0.005)
        self.assertLessEqual(loosened.max_top_holder_pct, 0.25)
        self.assertTrue(loosened.clamped)

        raw = {
            "strategy_id": "weaken",
            "version": "9",
            "status": "live",
            "MAX_BUY_SOL": 1,
            "entry": {},
            "exits": {"blind_2x": True},
        }
        cleaned = sanitize_strategy(raw, Path("weaken.md"), "A hypothesis only.")
        self.assertEqual(cleaned["status"], "paper-only")
        self.assertTrue(cleaned["attempted_live_promotion"])
        self.assertTrue(cleaned["attempted_limit_changes"])
        self.assertNotIn("MAX_BUY_SOL", json.dumps(cleaned["params"]))
        self.assertFalse(cleaned["exits"]["blind_2x"])

        strategies = Path(self.tmp.name) / "strategies"
        strategies.mkdir()
        (strategies / "bad.md").write_text(
            "# Bad\n\nTrying to loosen limits.\n\n```json\n"
            + json.dumps(raw)
            + "\n```\n",
            encoding="utf-8",
        )
        summary = run_daily(self.db, thresholds(), strategies, day="2026-09-26")
        self.assertFalse(summary["promote_to_live"])
        self.assertFalse(summary["hard_limits_changed"])
        self.assertFalse(summary["key_material_present"])
        self.assertEqual(summary["hard_caps"]["MAX_BUY_SOL"], 0.005)
        self.assertTrue(summary["attempted_live_promotion"])
        self.assertEqual(summary["mode"], "paper")

    def test_shipped_hypotheses_are_paper_only(self) -> None:
        loaded = load_strategies(REPO_ROOT / "research" / "strategies")
        ids = {item["strategy_id"] for item in loaded}
        self.assertIn("prebond-volume-regime", ids)
        self.assertIn("narrative-ceiling-exits", ids)
        self.assertIn("video3-scalping-community-filters", ids)
        for item in loaded:
            self.assertEqual(item["status"], "paper-only")
            self.assertFalse(item["attempted_live_promotion"])
            self.assertEqual(item["attempted_limit_changes"], [])
            self.assertEqual(item["chain"], "solana")

    def test_secret_material_refused(self) -> None:
        payload = candidate(candidate_id="cand-secret")
        payload["blob"] = [1] * 64
        result = self.engine.buy(payload, now=NOW)
        self.assertEqual(result["error"], "secret_material_refused")
        self.assertEqual(self.db.counts()["candidates"], 0)
        self.assertNotIn("1, 1, 1", json.dumps(result))

    def test_cli_status_scan_and_research(self) -> None:
        db_path = Path(self.tmp.name) / "cli.sqlite"
        env = os.environ.copy()
        env["TRADING_MODE"] = "live"
        env["PYTHONPATH"] = str(REPO_ROOT)
        status = subprocess.run(
            ["python3", "-m", "layer", "--db", str(db_path), "status"],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        body = json.loads(status.stdout)
        self.assertEqual(body["mode"], "paper")
        self.assertTrue(body["live_refused"])
        self.assertFalse(body["real_trades"])
        self.assertFalse(body["private_key_exposed"])
        self.assertEqual(body["caps"]["MAX_BUY_SOL"], 0.005)

        src = Path(self.tmp.name) / "one.json"
        fresh = candidate(
            candidate_id="cand-cli",
            observed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        src.write_text(json.dumps(fresh), encoding="utf-8")
        scanned = subprocess.run(
            ["python3", "-m", "layer", "--db", str(db_path), "scan", "--input", str(src)],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        scan_body = json.loads(scanned.stdout)
        self.assertEqual(scan_body["results"][0]["decision"], "PASS")
        self.assertFalse(scan_body["results"][0]["traded"])

        out = Path(self.tmp.name) / "daily.json"
        researched = subprocess.run(
            [
                "python3", "-m", "layer", "--db", str(db_path), "research",
                "--day", "2026-09-26", "--out", str(out),
            ],
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(researched.returncode, 0, researched.stderr)
        daily = json.loads(out.read_text(encoding="utf-8"))
        self.assertFalse(daily["promote_to_live"])
        self.assertFalse(daily["key_material_present"])
        self.assertGreaterEqual(len(daily["hypotheses"]), 2)

    def test_video3_filters_unknown_and_hard_caps(self) -> None:
        loaded = {
            item["strategy_id"]: item
            for item in load_strategies(REPO_ROOT / "research" / "strategies")
        }
        strategy = loaded["video3-scalping-community-filters"]
        self.assertEqual(strategy["claim_status"], "hypothesis")
        self.assertEqual(strategy["evidence_status"], "hypothesis_not_evidence")
        self.assertEqual(strategy["attempted_limit_changes"], [])
        self.assertFalse(strategy["non_binding_research_note"]["binding"])
        nulls = [item["null_hypothesis"] for item in strategy["hypotheses"]]
        self.assertGreaterEqual(len(nulls), 7)
        self.assertTrue(all(isinstance(item, str) and item for item in nulls))
        opinion = json.dumps(strategy["opinion_vs_measurable"])
        self.assertIn("100 percent", opinion)
        self.assertNotIn("MAX_BUY_SOL", json.dumps(strategy["params"]))

        base = {
            "launchpad": "pump",
            "migration_status": "new_pair",
            "market_cap_usd": 9000,
            "pullback_from_ath_pct": 0.45,
        }
        matched = match_strategy(strategy, base, "pump")
        self.assertTrue(matched["matched"])
        self.assertIn("token_type", matched["unobserved"])
        self.assertIn("ca_in_bio", matched["unobserved"])
        self.assertIn("pinned_post_has_ca", matched["unobserved"])

        self.assertIn("launchpad", match_strategy(strategy, {**base, "launchpad": "meteora"}, "pump")["failed"])
        self.assertFalse(match_strategy(strategy, {**base, "migration_status": "migrated"}, "pump")["matched"])
        self.assertIn(
            "market_cap_usd",
            match_strategy(strategy, {**base, "market_cap_usd": 5000}, "pump")["failed"],
        )
        self.assertIn(
            "pullback_from_ath_pct",
            match_strategy(strategy, {**base, "pullback_from_ath_pct": 0.10}, "pump")["failed"],
        )
        self.assertIn(
            "token_type",
            match_strategy(strategy, {**base, "token_type": "tweet_speculation"}, "pump")["failed"],
        )
        self.assertIn(
            "discussion_quality",
            match_strategy(strategy, {**base, "discussion_quality": "link_spam"}, "pump")["failed"],
        )
        self.assertIn(
            "ca_in_bio",
            match_strategy(strategy, {**base, "ca_in_bio": False}, "pump")["failed"],
        )

        stretch = {
            "launchpad": "pump",
            "migration_status": "final_stretch",
            "market_cap_usd": 9000,
            "age_minutes": 40,
            "dev_holding_pct": 0.05,
            "insiders_pct": 0.20,
            "prot_traders": 10,
            "pullback_from_ath_pct": 0.42,
        }
        self.assertTrue(match_strategy(strategy, stretch, "pump")["matched"])
        self.assertIn("age_minutes", match_strategy(strategy, {**stretch, "age_minutes": 50}, "pump")["failed"])
        self.assertIn("dev_holding_pct", match_strategy(strategy, {**stretch, "dev_holding_pct": 0.08}, "pump")["failed"])
        self.assertIn("insiders_pct", match_strategy(strategy, {**stretch, "insiders_pct": 0.25}, "pump")["failed"])
        self.assertIn("prot_traders", match_strategy(strategy, {**stretch, "prot_traders": 9}, "pump")["failed"])
        missing_traders = dict(stretch)
        del missing_traders["prot_traders"]
        unknown_traders = match_strategy(strategy, missing_traders, "pump")
        self.assertIn("prot_traders", unknown_traders["unknown"])
        self.assertFalse(unknown_traders["matched"])

        payload = candidate(candidate_id="cand-video3")
        payload["features"].update(base)
        stored, error = normalize_candidate(payload)
        self.assertIsNone(error)
        self.assertEqual(stored["features"]["ca_in_bio"], "UNKNOWN")
        self.assertEqual(stored["features"]["pinned_post_has_ca"], "UNKNOWN")
        self.assertEqual(stored["features"]["discussion_quality"], "UNKNOWN")
        self.assertEqual(stored["features"]["token_type"], "UNKNOWN")
        bought = self.engine.buy(payload, now=NOW, strategy=strategy)
        self.assertTrue(bought["traded"])
        self.assertEqual(bought["size_sol"], 0.005)
        self.assertFalse(bought["sent"])
        entry = float(self.db.get_position(bought["position_id"])["entry_price"])
        jeet = self.engine.exit(
            bought["position_id"],
            {"price_sol": entry * 0.99, "liquidity_sol": 20, "volume_sol_5m": 8, "structure_valid": True},
        )
        self.assertTrue(jeet["full"])
        self.assertEqual(jeet["family"], "fixed_stop")

    def _risk(self, **overrides):
        payload = candidate(**overrides)
        normalized, error = normalize_candidate(payload)
        self.assertIsNone(error)
        return evaluate(normalized, thresholds(), now=NOW, open_exposure_sol=0, wallet_sol=0.1)


if __name__ == "__main__":
    unittest.main()
