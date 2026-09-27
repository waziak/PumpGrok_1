---
name: research-paper-layer
description: Paper-only research loop for Solana memecoin candidates. Stores SQLite features without inventing UNKNOWN values, runs the deterministic risk gate, and simulates fills. Agents emit JSON proposals only. Live execution stays disabled.
---

# Research and paper layer

Use this skill when recording candidates, running paper fills, or writing the daily research note. It does not authorize a live send.

## Hard caps (immutable)

- MAX_BUY_SOL = 0.005
- MAX_TOTAL_EXPOSURE_SOL = 0.03
- MIN_SOL_RESERVE = 0.02
- PAPER_BUY_SOL = 0.005

Strategy files, CHIEF, and daily research cannot raise these or lower the reserve. TRADING_MODE defaults to paper. The Python process refuses a live request.

## Commands

```bash
python -m layer scan --input candidates.json
python -m layer paper --input candidate.json
python -m layer paper --exit --position-id <id> --snapshot snapshot.json
python -m layer research --out research/daily/YYYY-MM-DD.json
python -m layer status
npm test
npm run live
```

`npm run live` refuses unless explicit out-of-repo confirmation variables are set, and the CLI still does not broadcast.

## Rules

- Agents emit structured trade-candidate JSON only. They do not sign, send, read wallet files, or override RISK.
- Missing features stay UNKNOWN. Do not fill them with zeros or guesses.
- CHIEF cannot override a RISK veto or a deterministic REJECT.
- Hypotheses under `research/strategies/` are paper tests, not proof and not return claims.
- `video3-scalping-community-filters` is a parsed transcript. Creator P&L and certainty claims are hypotheses, not evidence. Missing social fields stay UNKNOWN.
- Do not put key material in proposals, SQLite, logs, or git.
- `execution/` may sign only after its own gate opens. Default is closed.
- Exit families are structure invalidation, fixed stop, trailing stop, partial profit, momentum loss, liquidity deterioration, volume breakdown, and fixed R-multiple. Do not add a blind 2x rule.

## Evidence

Reuse `tools/authority_check.py`, `tools/holder_check.py`, `tools/jupiter_quote.py`, and `tools/pipeline_evidence.py`. Copy only fields those tools actually returned. Pipeline verdicts are not RISK clearance.
