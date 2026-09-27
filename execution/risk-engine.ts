/**
 * Execution-side risk check. Independent of agent text.
 * Hard SOL caps are clamped to config/hard-caps.json and cannot be raised here.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const fileCaps = JSON.parse(readFileSync(join(root, "config", "hard-caps.json"), "utf8")) as Record<string, number>;

export const HARD_CAPS = {
  MAX_BUY_SOL: Math.min(0.005, Number(fileCaps.MAX_BUY_SOL)),
  MAX_TOTAL_EXPOSURE_SOL: Math.min(0.03, Number(fileCaps.MAX_TOTAL_EXPOSURE_SOL)),
  MIN_SOL_RESERVE: Math.max(0.02, Number(fileCaps.MIN_SOL_RESERVE)),
  PAPER_BUY_SOL: Math.min(0.005, Number(fileCaps.PAPER_BUY_SOL)),
} as const;

const BASE58 = new Set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz");
const SUPPORTED = new Set(["pump", "pumpswap", "jupiter", "pump.fun", "jup"]);
const OVERRIDE_KEYS = ["risk_override", "override_risk", "ignore_risk", "chief_override", "veto_override", "force_clear", "bypass_risk"];

export type ExecRisk = {
  decision: "PASS" | "REJECT";
  reasons: string[];
  sizeSol: number | null;
};

function unknownMint(mint: unknown): boolean {
  if (typeof mint !== "string") return true;
  const token = mint.trim();
  if (!token || token.toUpperCase() === "UNKNOWN") return true;
  if (token.length < 32 || token.length > 44) return true;
  return [...token].some((char) => !BASE58.has(char));
}

export function evaluateExecutionRisk(input: {
  candidate: Record<string, unknown>;
  nowMs: number;
  openExposureSol: number;
  walletSol: number | null;
  maxAgeMs?: number;
}): ExecRisk {
  const reasons: string[] = [];
  const candidate = input.candidate;
  const features = (candidate.features && typeof candidate.features === "object"
    ? candidate.features
    : null) as Record<string, unknown> | null;
  if (!features) reasons.push("malformed_output");

  for (const key of OVERRIDE_KEYS) {
    if (candidate[key] === true) reasons.push("ai_risk_override");
  }
  const reviews = Array.isArray(candidate.agent_reviews) ? candidate.agent_reviews : [];
  for (const review of reviews) {
    if (!review || typeof review !== "object") continue;
    const verdict = String((review as { verdict?: unknown }).verdict || "").toUpperCase();
    const agent = String((review as { agent?: unknown }).agent || "").toUpperCase();
    if (verdict.includes("OVERRIDE") || (agent === "CHIEF" && (verdict === "CLEAR" || verdict === "PASS"))) {
      reasons.push("ai_risk_override");
    }
  }

  if (unknownMint(candidate.mint)) reasons.push("unknown_mint");
  const program = String(candidate.program || "").toLowerCase();
  if (!SUPPORTED.has(program)) reasons.push("unsupported_program");

  const freeze = features?.freeze_authority;
  const mintAuth = features?.mint_authority;
  if (freeze !== "revoked" && freeze !== null) reasons.push(freeze === undefined || freeze === "UNKNOWN" ? "freeze_authority_unknown" : "freeze_authority_risk");
  if (mintAuth !== "revoked" && mintAuth !== null) reasons.push(mintAuth === undefined || mintAuth === "UNKNOWN" ? "mint_authority_unknown" : "mint_authority_risk");

  const liquidity = features?.liquidity_sol;
  if (typeof liquidity !== "number") reasons.push("liquidity_unknown");
  else if (liquidity < 5) reasons.push("low_liquidity");

  const top = features?.top_holder_pct;
  if (typeof top !== "number") reasons.push("holder_concentration_unknown");
  else if (top > 0.15) reasons.push("high_holder_concentration");

  const observed = typeof candidate.observed_at === "string" ? Date.parse(candidate.observed_at) : NaN;
  const maxAge = input.maxAgeMs ?? 180_000;
  if (!Number.isFinite(observed)) reasons.push("observed_at_unknown");
  else if (input.nowMs - observed > maxAge) reasons.push("stale_candidate");

  if (features?.route_ok !== true) reasons.push("bad_routing");
  const impact = features?.price_impact_bps;
  if (typeof impact === "number" && impact > 300) reasons.push("bad_routing");

  const requested = typeof candidate.proposed_size_sol === "number" ? candidate.proposed_size_sol : HARD_CAPS.PAPER_BUY_SOL;
  let size: number | null = requested;
  if (!(requested > 0) || requested - HARD_CAPS.MAX_BUY_SOL > 1e-12) {
    reasons.push("excess_size");
    size = null;
  }
  if (size !== null && input.openExposureSol + size - HARD_CAPS.MAX_TOTAL_EXPOSURE_SOL > 1e-12) {
    reasons.push("excess_exposure");
  }
  // Signature, priority, one ATA rent, and the pump user-volume account rent.
  // The hard caps above are unchanged. This buffer only keeps the reserve intact.
  const fee = (5_000 + 20_000 + 2_039_280 + 1_844_400) / 1_000_000_000;
  if (input.walletSol === null) reasons.push("sol_reserve");
  else if (size !== null && input.walletSol - size - fee < HARD_CAPS.MIN_SOL_RESERVE) reasons.push("sol_reserve");

  const deduped = [...new Set(reasons)];
  return { decision: deduped.length ? "REJECT" : "PASS", reasons: deduped, sizeSol: deduped.length ? null : size };
}
