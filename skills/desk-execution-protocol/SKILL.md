---
name: desk-execution-protocol
description: Hard rules that SNIPER and EXIT must obey before, during, and after every transaction. Enforces single-send, exact-parameter matching, and no-auto-retry.
---

# Desk Execution Protocol

## Pre-Send Checklist (ALL must pass)
1. Ticket status = APPROVED
2. RISK Verdict = CLEAR (or CONDITIONAL conditions met)
3. Human message contains exact ticket ID + mint + size + max slippage
4. Daily-loss limit not breached
5. Live quote obtained within last 30–60 s
6. Returned mint and expected size match the approved ticket

## Preferred Tools
```bash
python /workspace/pumpgrok/tools/jupiter_quote.py ...
python /workspace/pumpgrok/tools/priority_fee.py ...
```

## Rules
- Submit once only
- On any error or timeout → stop and report “UNKNOWN RESULT – reconcile by signature”
- Never increase size, change mint, or re-quote and send automatically

## Paper layer and disabled execution package
Agents do not sign and do not read wallet files. Paper fills go through `python -m layer paper`. The `execution/` package repeats the hard caps, simulates only when a caller injects a transport, and refuses network send unless explicitly configured. `npm run live` refuses by default. Unknown, timed-out, and failed results are not retried. Duplicate receipts are rejected.
