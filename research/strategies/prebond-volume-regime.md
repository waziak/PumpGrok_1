# Pre-bond volume regime

Hypothesis drawn from https://www.youtube.com/watch?v=vaB08fflrYc (Cupsy / Incent / Flipping Profits). This is not proof and not a return claim.

The video discusses pre-bond entries around a 5k to 20k market cap, small size, consistent take-profit, a volume-regime filter, and matching an edge instead of chasing. v1 tests that shape on Solana paper fills only. Size cannot exceed the desk hard caps.

```json
{
  "strategy_id": "prebond-volume-regime",
  "version": "0.1.0",
  "status": "paper-only",
  "source_url": "https://www.youtube.com/watch?v=vaB08fflrYc",
  "claim_status": "hypothesis",
  "chain": "solana",
  "multi_chain": "out_of_scope_v1",
  "entry": {
    "program": "pump",
    "market_cap_usd_min": 5000,
    "market_cap_usd_max": 20000,
    "min_volume_sol_5m": 1.0
  },
  "exits": {
    "fixed_stop_pct": 0.25,
    "trail_pct": 0.2,
    "partial_r": 1.0,
    "partial_fraction": 0.5,
    "target_r": 2.0,
    "momentum_volume_ratio": 0.4,
    "liquidity_ratio": 0.5,
    "volume_floor_sol_5m": 0.5,
    "blind_2x": false
  },
  "notes": "Small size means the hard cap PAPER_BUY_SOL. Consistent take-profit is partial_r then target_r, not a blind 2x. UNKNOWN volume does not pass the filter."
}
```
