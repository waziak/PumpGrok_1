"""Path setup and the stop-loss plus profit checklists printed after the suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GROUPS = {
    "STOP LOSS ENGINE": [
        "test_reject_trade_without_stop",
        "test_fixed_20_percent_stop_uses_actual_fill_not_signal",
        "test_full_exit_on_fixed_stop_breach",
        "test_strategy_stop_clamped_never_wider_than_max",
        "test_llm_cannot_disable_widen_or_remove_stop",
        "test_structure_stop",
        "test_partial_profit_retains_stop_and_moves_to_breakeven",
        "test_exit_priority_over_new_entry",
        "test_stop_during_grok_and_scanner_outage",
    ],
    "HARD EMERGENCY STOP": [
        "test_hard_25_percent_emergency_stop",
        "test_hard_max_loss_ignores_override_attempts",
        "test_overnight_caps_tighten_hard_loss_and_block_oversized_buys",
    ],
    "TRAILING STOP": [
        "test_trailing_activates_moves_up_and_never_down",
    ],
    "RESTART RECOVERY": [
        "test_crash_after_trigger_restart_executes_below_stop",
        "test_restart_does_not_reset_stops",
    ],
    "STOP RETRY": [
        "test_failed_stop_tx_safe_retry_and_duplicate_event",
    ],
    "PAPER STOP TEST": [
        "test_paper_stop_cycle_and_receipt",
        "test_liquidity_collapse_exit",
        "test_no_route_pool_gone_and_unsellable",
        "test_stale_price_fallback_and_not_safe",
        "test_severe_slippage_worse_than_configured_stop",
        "test_status_shows_open_position",
        "test_live_sends_disabled",
        "test_private_key_not_exposed",
        "test_paper_sim_rejects_buy_without_stop",
    ],
    "STAGED PROFIT ENGINE": [
        "test_profit_stages_leave_a_runner",
        "test_profit_no_sell_all_at_plus_50_or_mcap_ceiling",
        "test_profit_partials_are_debounced",
        "test_profit_peak_drawdown_partial_not_flatten",
        "test_profit_hard_stop_beats_partial",
    ],
    "TREND STATE ENGINE": [
        "test_trend_components_are_visible",
        "test_trend_unknown_without_measurements",
        "test_grok_narrative_is_not_authority",
        "test_trend_breakdown_exits_runner_after_debounce",
    ],
    "MARKET CAP REGIME ENGINE": [
        "test_regime_context_does_not_exit",
    ],
    "STALL DETECTION": [
        "test_stall_at_50k",
    ],
    "BREAKOUT DETECTION": [
        "test_breakout_50k_to_200k",
    ],
    "RUNNER MODE": [
        "test_runner_to_1m",
    ],
    "ADAPTIVE TRAILING STOP": [
        "test_adaptive_trail_never_loosens",
    ],
    "PRINCIPAL RECOVERY": [
        "test_principal_recovery",
    ],
    "COUNTERFACTUAL TRACKING": [
        "test_counterfactual_tracking",
    ],
}

NEW_TESTS = [
    "test_profit_stages_leave_a_runner",
    "test_profit_no_sell_all_at_plus_50_or_mcap_ceiling",
    "test_profit_partials_are_debounced",
    "test_profit_peak_drawdown_partial_not_flatten",
    "test_profit_hard_stop_beats_partial",
    "test_trend_components_are_visible",
    "test_trend_unknown_without_measurements",
    "test_grok_narrative_is_not_authority",
    "test_trend_breakdown_exits_runner_after_debounce",
    "test_regime_context_does_not_exit",
    "test_stall_at_50k",
    "test_breakout_50k_to_200k",
    "test_runner_to_1m",
    "test_adaptive_trail_never_loosens",
    "test_principal_recovery",
    "test_counterfactual_tracking",
    "test_profit_status_shows_research",
    "test_profit_paper_mode_only",
]

_results: dict[str, bool] = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        _results[item.name] = bool(report.passed)
    elif report.when == "setup" and report.failed:
        _results[item.name] = False


STOP_GROUP_NAMES = (
    "STOP LOSS ENGINE",
    "HARD EMERGENCY STOP",
    "TRAILING STOP",
    "RESTART RECOVERY",
    "STOP RETRY",
    "PAPER STOP TEST",
)


def _group_line(name: str, tests: list[str], results: dict[str, bool]) -> str:
    ok = all(results.get(test) is True for test in tests)
    return f"{name}: {'PASS' if ok else 'FAIL'}"


def checklist_lines(results: dict[str, bool]) -> list[str]:
    lines = [_group_line(name, GROUPS[name], results) for name in STOP_GROUP_NAMES]
    private_key = "NO" if results.get("test_private_key_not_exposed") is True else "YES"
    real_trades = "NO" if results.get("test_live_sends_disabled") is True else "YES"
    lines.append(f"PRIVATE KEY EXPOSED={private_key}")
    lines.append(f"REAL TRADES={real_trades}")
    engine_ok = all(line.endswith("PASS") for line in lines if line.startswith((
        "STOP LOSS ENGINE",
        "HARD EMERGENCY STOP",
        "TRAILING STOP",
        "RESTART RECOVERY",
        "STOP RETRY",
        "PAPER STOP TEST",
    )))
    # Real trading stays disabled. Live-ready cannot pass while that is true.
    live_ready = "PASS" if engine_ok and real_trades == "YES" else "FAIL"
    lines.append(f"LIVE READY: {live_ready}")
    return lines


def profit_checklist_lines(results: dict[str, bool]) -> list[str]:
    lines = [
        _group_line(name, tests, results)
        for name, tests in GROUPS.items()
        if name not in STOP_GROUP_NAMES
    ]
    passed = sum(1 for name in NEW_TESTS if results.get(name) is True)
    lines.append(f"NEW TESTS {passed}/{len(NEW_TESTS)}")
    paper_ok = results.get("test_profit_paper_mode_only") is True
    live_blocked = results.get("test_live_sends_disabled") is True
    lines.append(f"REAL TRADES {'NO' if paper_ok and live_blocked else 'YES'}")
    lines.append(f"CURRENT MODE {'PAPER' if paper_ok else 'FAIL'}")
    return lines


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    del exitstatus, config
    terminalreporter.write_sep("-", "stop-loss checklist")
    for line in checklist_lines(_results):
        terminalreporter.write_line(line)
    terminalreporter.write_sep("-", "profit checklist")
    for line in profit_checklist_lines(_results):
        terminalreporter.write_line(line)
