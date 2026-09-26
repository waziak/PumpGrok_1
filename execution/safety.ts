/** Pre-send checks. An expired blockhash or an uncertain signature is not resent. */

export const BLOCKHASH_TTL_MS = 60_000;
export const MAX_SLIPPAGE_BPS = 300;
export const MAX_PRICE_IMPACT_BPS = 800;

export function blockhashExpired(fetchedAtMs: number, nowMs: number, ttlMs = BLOCKHASH_TTL_MS): boolean {
  if (!Number.isFinite(fetchedAtMs) || !Number.isFinite(nowMs)) return true;
  return nowMs - fetchedAtMs > ttlMs || fetchedAtMs - nowMs > 5_000;
}
