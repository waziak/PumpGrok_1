/** Jupiter lite quote for observed price impact. A missing quote stays UNKNOWN. */

import { HttpProvider } from "./provider.ts";

export const JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote";
export const WSOL = "So11111111111111111111111111111111111111112";

export async function jupiterImpact(
  provider: HttpProvider,
  mint: string,
  amountLamports = 5_000_000,
): Promise<{ priceImpactPct?: string; outAmount?: string; error?: string }> {
  const url =
    `${JUPITER_QUOTE}?inputMint=${WSOL}&outputMint=${mint}&amount=${amountLamports}&slippageBps=150`;
  const response = await provider.getJson(url);
  if (!response.ok) return { error: response.error };
  const data = response.data as { priceImpactPct?: string; outAmount?: string; error?: string };
  if (!data || data.error) return { error: data?.error || "quote_error" };
  return { priceImpactPct: data.priceImpactPct, outAmount: data.outAmount };
}
