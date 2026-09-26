/**
 * Swap plan builder. It does not broadcast.
 *
 * Jupiter Swap V2 (docs, 2026): GET https://api.jup.ag/swap/v2/build returns
 * quote plus instructions. POST /swap/v2/execute and the landing endpoint are
 * live paths and are not called here.
 *
 * Pump bonding curve: 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
 * PumpSwap AMM: pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA
 * Pump fees: pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ
 * buy_v2 takes amount and max quote cost. Account lists change; this file
 * records the plan and does not assemble a claim of a valid on-chain ix.
 */

import { HARD_CAPS } from "./risk-engine.ts";

export const PROGRAM_IDS = {
  pump: "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
  pumpswap: "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
  pumpFees: "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
} as const;

export const JUPITER = {
  build: "https://api.jup.ag/swap/v2/build",
  executeCalled: false,
} as const;

const MAX_SLIPPAGE_BPS = 300;
const MAX_AGE_MS = 180_000;

export type SwapPlan =
  | {
      ok: true;
      liveSend: false;
      program: string;
      programId: string | null;
      instruction: string;
      amountLamports: number;
      maxSolCostLamports: number;
      slippageBps: number;
      jupiter: typeof JUPITER;
      wireReady: false;
      notes: string[];
    }
  | { ok: false; error: string; liveSend: false; sent: false; wireReady: false };

export function buildPlan(input: {
  program: string;
  sizeSol: number;
  slippageBps: number;
  observedAt: string;
  nowMs: number;
}): SwapPlan {
  const observed = Date.parse(input.observedAt);
  if (!Number.isFinite(observed) || input.nowMs - observed > MAX_AGE_MS) {
    return { ok: false, error: "stale_candidate", liveSend: false, sent: false, wireReady: false };
  }
  if (input.slippageBps > MAX_SLIPPAGE_BPS || input.slippageBps < 0) {
    return { ok: false, error: "slippage_exceeds_max", liveSend: false, sent: false, wireReady: false };
  }
  if (!(input.sizeSol > 0) || input.sizeSol - HARD_CAPS.MAX_BUY_SOL > 1e-12) {
    return { ok: false, error: "excess_size", liveSend: false, sent: false, wireReady: false };
  }
  const program = input.program.toLowerCase();
  const programId = program === "pump" || program === "pump.fun"
    ? PROGRAM_IDS.pump
    : program === "pumpswap"
      ? PROGRAM_IDS.pumpswap
      : program === "jupiter" || program === "jup"
        ? null
        : undefined;
  if (programId === undefined) {
    return { ok: false, error: "unsupported_program", liveSend: false, sent: false, wireReady: false };
  }
  const lamports = Math.round(input.sizeSol * 1_000_000_000);
  const maxSolCostLamports = Math.round(lamports * (1 + input.slippageBps / 10_000));
  return {
    ok: true,
    liveSend: false,
    program,
    programId,
    instruction: programId === PROGRAM_IDS.pumpswap ? "buy" : programId === null ? "jupiter-build" : "buy_v2",
    amountLamports: lamports,
    maxSolCostLamports,
    slippageBps: input.slippageBps,
    jupiter: JUPITER,
    wireReady: false,
    notes: [
      "Plan only. Account metas for Pump buy_v2 and PumpSwap buy are not assembled here.",
      "Jupiter /swap/v2/execute is not called. Jito sendBundle is not called by the builder.",
    ],
  };
}
