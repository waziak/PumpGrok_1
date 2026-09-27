/** DexScreener search for PumpSwap and pump.fun pairs. No API key. */

import { HttpProvider } from "./provider.ts";
import { normalizeDexPair, type NormalizedToken } from "./token-state.ts";

export const DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search";
export const DEX_PAIRS = "https://api.dexscreener.com/token-pairs/v1/solana";

export async function discoverPumpSwap(
  provider: HttpProvider,
  opts: { query?: string; nowMs?: number } = {},
): Promise<{ ok: boolean; tokens: NormalizedToken[]; error?: string }> {
  const query = opts.query ?? "pumpswap";
  const response = await provider.getJson(`${DEX_SEARCH}?q=${encodeURIComponent(query)}`);
  if (!response.ok) return { ok: false, tokens: [], error: response.error };
  const body = response.data as { pairs?: unknown };
  if (!body || !Array.isArray(body.pairs)) return { ok: false, tokens: [], error: "unexpected_payload" };
  const nowMs = opts.nowMs ?? Date.now();
  const tokens: NormalizedToken[] = [];
  for (const pair of body.pairs) {
    const token = normalizeDexPair(pair, nowMs);
    if (token) tokens.push(token);
  }
  return { ok: true, tokens };
}

export async function pairsForMint(
  provider: HttpProvider,
  mint: string,
  nowMs: number,
): Promise<NormalizedToken | null> {
  const response = await provider.getJson(`${DEX_PAIRS}/${mint}`);
  if (!response.ok || !Array.isArray(response.data)) return null;
  let best: NormalizedToken | null = null;
  let bestLiq = -1;
  for (const pair of response.data) {
    const token = normalizeDexPair(pair, nowMs);
    if (!token) continue;
    const liq = typeof token.features.liquidity_sol === "number" ? token.features.liquidity_sol : 0;
    if (!best || liq > bestLiq) {
      best = token;
      bestLiq = liq;
    }
  }
  return best;
}
