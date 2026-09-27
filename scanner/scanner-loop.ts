/**
 * One discovery pass: Pump.fun coins, PumpSwap pairs, cheap filter, small enrichment.
 * Fields that were not observed stay UNKNOWN.
 */

import { readBalance, readHolders } from "./holders.ts";
import { jupiterImpact } from "./market-data.ts";
import { HttpProvider, rpcUrls, type FetchImpl } from "./provider.ts";
import { discoverPumpCoins } from "./pump.ts";
import { discoverPumpSwap, pairsForMint } from "./pumpswap.ts";
import {
  PUBLIC_WALLET,
  applyHolders,
  applyQuote,
  applyVolumeSol,
  cheapFilter,
  mergeToken,
  type NormalizedToken,
} from "./token-state.ts";

export type ScanOptions = {
  fetchImpl?: FetchImpl;
  nowMs?: number;
  enrichLimit?: number;
  watchMints?: string[];
  env?: Record<string, string | undefined>;
  sleep?: (ms: number) => Promise<void>;
  attempts?: number;
  timeoutMs?: number;
};

export type ScanResult = {
  ok: boolean;
  live: boolean;
  mocked: boolean;
  tokensObserved: number;
  candidates: NormalizedToken[];
  marks: Record<string, unknown>[];
  errors: string[];
  samples: { stage: string; duration_ms: number }[];
  wallet: { pubkey: string; balanceLamports: number | null };
  scanner: Record<string, unknown>;
};

export async function scanOnce(opts: ScanOptions = {}): Promise<ScanResult> {
  const nowMs = opts.nowMs ?? Date.now();
  const provider = new HttpProvider({
    fetchImpl: opts.fetchImpl,
    timeoutMs: opts.timeoutMs,
    attempts: opts.attempts,
    sleep: opts.sleep,
  });
  const enrichLimit = opts.enrichLimit ?? 4;
  const errors: string[] = [];
  const samples: { stage: string; duration_ms: number }[] = [];
  const started = Date.now();

  const pumpNew = await discoverPumpCoins(provider, { limit: 30, sort: "created_timestamp", nowMs });
  const pumpActive = await discoverPumpCoins(provider, { limit: 20, sort: "last_trade_timestamp", nowMs });
  const migrated = await discoverPumpSwap(provider, { query: "pumpswap", nowMs });
  const pumpDex = await discoverPumpSwap(provider, { query: "pumpfun", nowMs });
  if (!pumpNew.ok) errors.push(`pump:${pumpNew.error}`);
  if (!migrated.ok) errors.push(`pumpswap:${migrated.error}`);

  const byMint = new Map<string, NormalizedToken>();
  for (const token of [...pumpNew.tokens, ...pumpActive.tokens, ...migrated.tokens, ...pumpDex.tokens]) {
    const existing = byMint.get(token.mint);
    byMint.set(token.mint, existing ? mergeToken(existing, token) : token);
  }
  for (const mint of opts.watchMints || []) {
    if (byMint.has(mint)) continue;
    const pair = await pairsForMint(provider, mint, nowMs);
    if (pair && cheapFilter(pair).pass) byMint.set(mint, pair);
  }
  const filtered = [...byMint.values()].filter((token) => cheapFilter(token).pass);
  samples.push({ stage: "discover", duration_ms: Date.now() - started });

  const enrichStarted = Date.now();
  const selected = selectEnrich(filtered, opts.watchMints || [], enrichLimit);
  const urls = rpcUrls(opts.env || process.env);
  const enriched: NormalizedToken[] = [];
  for (const token of selected) {
    let next = token;
    const pair = await pairsForMint(provider, token.mint, nowMs);
    if (pair) next = mergeToken(next, pair);
    const holders = await readHolders(provider, token.mint, urls);
    next = applyHolders(next, holders.info, holders.largest);
    const quote = await jupiterImpact(provider, token.mint);
    next = applyQuote(next, quote);
    next = applyVolumeSol({ ...next, enriched: true });
    enriched.push(next);
    byMint.set(next.mint, next);
  }
  samples.push({ stage: "enrich", duration_ms: Date.now() - enrichStarted });

  const balance = await readBalance(provider, PUBLIC_WALLET, urls);
  const candidates = [...byMint.values()].filter((token) => cheapFilter(token).pass).slice(0, 40);
  const marks = candidates
    .filter((token) => typeof token.features.price_sol === "number")
    .map((token) => ({
      mint: token.mint,
      price_sol: token.features.price_sol,
      liquidity_sol: typeof token.features.liquidity_sol === "number" ? token.features.liquidity_sol : undefined,
      volume_sol_5m: typeof token.features.volume_sol_5m === "number" ? token.features.volume_sol_5m : undefined,
      market_cap_usd: typeof token.features.market_cap_usd === "number" ? token.features.market_cap_usd : undefined,
    }));

  const live = opts.fetchImpl === undefined;
  return {
    ok: pumpNew.ok || migrated.ok || candidates.length > 0,
    live,
    mocked: !live,
    tokensObserved: candidates.length,
    candidates,
    marks,
    errors,
    samples,
    wallet: { pubkey: PUBLIC_WALLET, balanceLamports: balance },
    scanner: {
      pump_new: pumpNew.tokens.length,
      pump_active: pumpActive.tokens.length,
      pumpswap: migrated.tokens.length,
      pumpfun_dex: pumpDex.tokens.length,
      enriched: enriched.length,
      errors,
      live,
    },
  };
}

export function selectEnrich(tokens: NormalizedToken[], watch: string[], limit: number): NormalizedToken[] {
  const byMint = new Map(tokens.map((token) => [token.mint, token]));
  const chosen: NormalizedToken[] = [];
  const seen = new Set<string>();
  for (const mint of watch) {
    const token = byMint.get(mint);
    if (!token || seen.has(mint)) continue;
    chosen.push(token);
    seen.add(mint);
  }
  const rest = tokens.filter((token) => !seen.has(token.mint));
  rest.sort((a, b) => rank(b) - rank(a));
  for (const token of rest) {
    if (chosen.length >= limit) break;
    chosen.push(token);
    seen.add(token.mint);
  }
  return chosen.slice(0, Math.max(limit, watch.length));
}

function rank(token: NormalizedToken): number {
  let score = 0;
  if (token.discovery_class === "new_pair") score += 50;
  if (token.observed.activity === "unusually_active") score += 40;
  if (token.discovery_class === "migrated") score += 10;
  const age = token.features.age_minutes;
  if (typeof age === "number") score += Math.max(0, 30 - age);
  const volume = token.observed.volume_m5_usd;
  if (typeof volume === "number") score += Math.min(30, volume / 1000);
  return score;
}
