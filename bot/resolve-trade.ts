/**
 * Build a TradeRequest from a qualified mint plus on-chain account data.
 * Missing creator, token program, or a quote leaves the trade unwired.
 */

import { b58encode } from "../execution/codec.ts";
import type { TradeRequest } from "../execution/controlled.ts";
import type { AccountInfo } from "../execution/rpc.ts";
import {
  BUYBACK_FEE_RECIPIENTS,
  MAYHEM_FEE_RECIPIENTS,
  NORMAL_FEE_RECIPIENTS,
  PUMP,
  TOKEN_2022,
  TOKEN_PROGRAM,
  WSOL,
  bondingCurvePda,
  canonicalPumpSwapPool,
  decodeBondingCurve,
} from "../execution/venues.ts";
import { PUBLIC_WALLET } from "../scanner/token-state.ts";

export type Resolved =
  | { ok: true; trade: TradeRequest }
  | { ok: false; missing: string[] };

export function quoteMinTokens(priceSol: number, decimals: number, sizeSol: number, slippageBps: number): bigint | null {
  if (!(priceSol > 0) || !(sizeSol > 0) || decimals < 0 || decimals > 18) return null;
  if (slippageBps < 0 || slippageBps > 10_000) return null;
  const tokens = (sizeSol / priceSol) * 10 ** decimals * (1 - slippageBps / 10_000);
  if (!Number.isFinite(tokens) || tokens < 1) return null;
  return BigInt(Math.floor(tokens));
}

function mintDecimals(data: Uint8Array): number | null {
  if (data.length < 45) return null;
  const decimals = data[44];
  if (decimals > 18) return null;
  return decimals;
}

export async function resolveTrade(input: {
  mint: string;
  program: string;
  priceSol: number;
  sizeSol: number;
  slippageBps: number;
  user?: string;
  getAccountInfo(pubkey: string): Promise<AccountInfo | null>;
}): Promise<Resolved> {
  const missing: string[] = [];
  const limitLamports = BigInt(Math.round(Math.min(input.sizeSol, 0.005) * 1_000_000_000));
  if (limitLamports <= 0n || limitLamports > 5_000_000n) missing.push("size");
  const mintInfo = await input.getAccountInfo(input.mint);
  const baseTokenProgram = mintInfo && (mintInfo.owner === TOKEN_PROGRAM || mintInfo.owner === TOKEN_2022)
    ? mintInfo.owner
    : "";
  if (!baseTokenProgram) missing.push("baseTokenProgram");
  const decimals = mintInfo ? mintDecimals(mintInfo.data) : null;
  const amount = decimals === null ? null : quoteMinTokens(input.priceSol, decimals, Number(limitLamports) / 1_000_000_000, input.slippageBps);
  if (amount === null) missing.push("min_tokens_out");
  const user = input.user || PUBLIC_WALLET;
  const venue = input.program === "pumpswap" ? "pumpswap" : "pump";

  if (venue === "pump") {
    const curve = bondingCurvePda(input.mint);
    const curveInfo = await input.getAccountInfo(curve);
    const decoded = curveInfo && curveInfo.owner === PUMP ? decodeBondingCurve(curveInfo.data) : null;
    if (!decoded) missing.push("creator");
    else if (decoded.complete) missing.push("bonding_curve_open");
    if (missing.length || !decoded || amount === null) {
      return { ok: false, missing: [...new Set(missing)] };
    }
    return {
      ok: true,
      trade: {
        venue: "pump",
        side: "buy",
        user,
        baseMint: input.mint,
        creator: decoded.creator,
        baseTokenProgram,
        quoteMint: WSOL,
        quoteTokenProgram: TOKEN_PROGRAM,
        feeRecipient: decoded.mayhem ? MAYHEM_FEE_RECIPIENTS[0] : NORMAL_FEE_RECIPIENTS[0],
        buybackFeeRecipient: BUYBACK_FEE_RECIPIENTS[0],
        mayhem: decoded.mayhem,
        amount,
        limitLamports,
      },
    };
  }

  const pool = canonicalPumpSwapPool(input.mint);
  const poolInfo = await input.getAccountInfo(pool);
  let coinCreator = "";
  let mayhem = false;
  if (!poolInfo || poolInfo.data.length < 243) missing.push("coinCreator");
  else {
    const base = b58encode(poolInfo.data.subarray(43, 75));
    if (base !== input.mint) missing.push("pool_base_mint");
    coinCreator = b58encode(poolInfo.data.subarray(211, 243));
    if (poolInfo.data.length > 243) mayhem = poolInfo.data[243] === 1;
    if (!coinCreator || coinCreator === "11111111111111111111111111111111") missing.push("coinCreator");
  }
  if (!baseTokenProgram) missing.push("baseTokenProgram");
  if (missing.length || amount === null || !coinCreator) return { ok: false, missing: [...new Set(missing)] };
  return {
    ok: true,
    trade: {
      venue: "pumpswap",
      side: "buy",
      user,
      baseMint: input.mint,
      coinCreator,
      baseTokenProgram,
      quoteMint: WSOL,
      quoteTokenProgram: TOKEN_PROGRAM,
      protocolFeeRecipient: mayhem ? MAYHEM_FEE_RECIPIENTS[0] : NORMAL_FEE_RECIPIENTS[0],
      buybackFeeRecipient: BUYBACK_FEE_RECIPIENTS[0],
      pool,
      mayhem,
      amount,
      limitLamports,
    },
  };
}
