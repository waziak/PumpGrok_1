---
name: solana-rpc-and-wallet
description: Safe patterns for reading balances, token accounts, metadata, Token-2022 extensions, and managing the throwaway wallet connection without ever handling private keys.
---

# Solana RPC and Wallet

## Wallet Rules (non-negotiable)
- Only a dedicated throwaway wallet ≤ $200 USDC + SOL for fees
- Private keys and seed phrases never enter chat, git, SQLite, research files, or agent prompts
- Desk agents connect by human screen hand-off only and record the public address in desk.md
- `execution/wallet.ts` may read `SOLANA_KEYPAIR_PATH` only after the live gate opens, and it must not print or store the bytes. Default `npm run live` does not read a wallet. `.env.example` documents the path only

## Priority Fees & Compute
Prefer the output of `tools/priority_fee.py`. Cap maximum fee so a spike cannot drain the wallet.
