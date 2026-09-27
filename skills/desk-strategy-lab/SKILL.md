---
name: desk-strategy-lab
description: Lightweight rules and paper-trading support for entry filters, position sizing, and exit logic. Allows the desk to experiment without touching live capital until rules are proven.
---

# Desk Strategy Lab

## Paper Mode
When engagement = paper, never call SNIPER for a real send.
Log the simulated fill with:
```bash
python /workspace/pumpgrok/tools/paper_sim.py --action buy|sell --ticket <ID> \
  --mint <mint> --size-usd <usd> --price <price> ...
```

## Paper layer
Prefer the deterministic paper engine for simulated buys and exits:

```bash
python -m layer paper --input candidate.json
python -m layer paper --exit --position-id <id> --snapshot snapshot.json
```

Hypotheses under `research/strategies/` are paper tests, not proof. They cannot raise MAX_BUY_SOL, raise MAX_TOTAL_EXPOSURE_SOL, lower MIN_SOL_RESERVE, or set status to live. Exit logic uses the named families (structure, fixed stop, trail, partial at R, momentum, liquidity, volume, fixed R). Do not add a blind 2x rule.

## Changing Rules
Any change must be written into a dated file under research/ and requires explicit human approval before promotion to micro-live. Daily research must not perform that promotion.
