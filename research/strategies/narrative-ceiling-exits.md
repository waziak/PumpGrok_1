# Narrative and ceiling-adaptive exits

Hypothesis drawn from https://www.youtube.com/watch?v=ZntWs_fr3ic . This is not proof and not a return claim.

The video discusses launchpad and narrative awareness, holder-reward flags when that meta is active, entries around a 30k to 50k market cap, and exits that adapt to a ceiling. Multi-chain readiness is out of scope for this Solana-only v1 layer.

```json
{
  "strategy_id": "narrative-ceiling-exits",
  "version": "0.1.0",
  "status": "paper-only",
  "source_url": "https://www.youtube.com/watch?v=ZntWs_fr3ic",
  "claim_status": "hypothesis",
  "chain": "solana",
  "multi_chain": "out_of_scope_v1",
  "entry": {
    "market_cap_usd_min": 30000,
    "market_cap_usd_max": 50000,
    "require_holder_reward_flag": true
  },
  "exits": {
    "fixed_stop_pct": 0.2,
    "trail_pct": 0.18,
    "partial_r": 1.0,
    "partial_fraction": 0.33,
    "target_r": 2.5,
    "momentum_volume_ratio": 0.45,
    "liquidity_ratio": 0.55,
    "volume_floor_sol_5m": 1.0,
    "ceiling_adaptive": true,
    "blind_2x": false
  },
  "notes": "Holder-reward must be observed true. UNKNOWN does not qualify. Near the market-cap ceiling the paper engine tightens the trail. Other chains are not traded."
}
```
