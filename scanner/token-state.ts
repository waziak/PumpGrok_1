/**
 * Normalize observed Pump and DexScreener fields.
 * Missing values stay UNKNOWN. Reserve integers become SOL only when the
 * 30 SOL virtual offset confirms lamport units.
 */

export const UNKNOWN = "UNKNOWN";
export const PUBLIC_WALLET = "2LmzxcxCfijZANvbiDrf8DcVRgUqY6PFwfB7wPVbsXCp";

const LAMPORTS = 1_000_000_000;
const VIRTUAL_OFFSET = 30 * LAMPORTS;
const BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export type NormalizedToken = {
  candidate_id: string;
  mint: string;
  symbol: string | null;
  name: string | null;
  source: string;
  program: string;
  observed_at: string;
  proposed_size_sol: number;
  slippage_bps: number;
  enriched: boolean;
  discovery_class: string;
  features: Record<string, unknown>;
  observed: Record<string, unknown>;
};

export function isoNow(nowMs: number): string {
  return new Date(nowMs).toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function validMint(mint: unknown): mint is string {
  if (typeof mint !== "string") return false;
  if (mint.length < 32 || mint.length > 44) return false;
  for (const char of mint) {
    if (!BASE58.includes(char)) return false;
  }
  return true;
}

export function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) return Number(value);
  return null;
}

export function solPriceUsd(usdMcap: unknown, solMcap: unknown): number | null {
  const usd = asNumber(usdMcap);
  const sol = asNumber(solMcap);
  if (usd === null || sol === null || sol <= 0) return null;
  const ratio = usd / sol;
  if (ratio < 10 || ratio > 10_000) return null;
  return ratio;
}

/** Real SOL in the bonding curve when virtual-real matches the 30 SOL offset. */
export function curveLiquiditySol(raw: Record<string, unknown>): number | typeof UNKNOWN {
  if (raw.complete === true) return UNKNOWN;
  const real = asNumber(raw.real_sol_reserves);
  const virt = asNumber(raw.virtual_sol_reserves);
  if (real === null || virt === null || real < 0) return UNKNOWN;
  if (Math.abs(virt - real - VIRTUAL_OFFSET) > 1_000_000) return UNKNOWN;
  return real / LAMPORTS;
}

export function priceSolFromPump(raw: Record<string, unknown>): number | typeof UNKNOWN {
  const mcap = asNumber(raw.market_cap);
  const supply = asNumber(raw.total_supply);
  const decimals = asNumber(raw.base_decimals);
  if (mcap === null || supply === null || decimals === null) return UNKNOWN;
  if (mcap <= 0 || supply <= 0 || decimals < 0 || decimals > 18) return UNKNOWN;
  const ui = supply / 10 ** decimals;
  if (!Number.isFinite(ui) || ui <= 0) return UNKNOWN;
  const price = mcap / ui;
  if (!Number.isFinite(price) || price <= 0) return UNKNOWN;
  return price;
}

export function pullbackFromAth(ath: unknown, usd: unknown): number | typeof UNKNOWN {
  const high = asNumber(ath);
  const current = asNumber(usd);
  if (high === null || current === null || high <= 0) return UNKNOWN;
  const value = (high - current) / high;
  if (value < 0 || value > 1) return UNKNOWN;
  return value;
}

export function migrationFromPump(raw: Record<string, unknown>, ageMinutes: number | typeof UNKNOWN): string {
  if (raw.complete === true) return "migrated";
  if (raw.complete === false && typeof ageMinutes === "number") {
    return ageMinutes < 15 ? "new_pair" : "non_migrated";
  }
  return UNKNOWN;
}

export function ageMinutes(createdMs: unknown, nowMs: number): number | typeof UNKNOWN {
  const created = asNumber(createdMs);
  if (created === null || created <= 0) return UNKNOWN;
  const minutes = (nowMs - created) / 60_000;
  if (!Number.isFinite(minutes) || minutes < -1) return UNKNOWN;
  return minutes < 0 ? 0 : minutes;
}

export function normalizePumpCoin(raw: unknown, nowMs: number): NormalizedToken | null {
  if (!raw || typeof raw !== "object") return null;
  const row = raw as Record<string, unknown>;
  if (!validMint(row.mint)) return null;
  const created = asNumber(row.created_timestamp);
  const age = ageMinutes(created, nowMs);
  const migration = migrationFromPump(row, age);
  const program = migration === "migrated" ? "pumpswap" : "pump";
  const usd = asNumber(row.usd_market_cap);
  const liquidity = curveLiquiditySol(row);
  const price = priceSolFromPump(row);
  const holderReward = typeof row.is_holder_reward === "boolean" ? row.is_holder_reward : UNKNOWN;
  const symbol = typeof row.symbol === "string" && row.symbol.trim() ? row.symbol.trim() : null;
  const name = typeof row.name === "string" && row.name.trim() ? row.name.trim() : null;
  return {
    candidate_id: row.mint,
    mint: row.mint,
    symbol,
    name,
    source: "pump.fun",
    program,
    observed_at: isoNow(nowMs),
    proposed_size_sol: 0.005,
    slippage_bps: 150,
    enriched: false,
    discovery_class: migration,
    features: {
      launchpad: "pump",
      migration_status: migration,
      market_cap_usd: usd === null ? UNKNOWN : usd,
      age_minutes: age,
      price_sol: price,
      liquidity_sol: liquidity,
      holder_reward_flag: holderReward,
      pullback_from_ath_pct: pullbackFromAth(row.ath_market_cap, row.usd_market_cap),
    },
    observed: {
      creation_time: created === null ? UNKNOWN : new Date(created).toISOString(),
      creator: typeof row.creator === "string" ? row.creator : UNKNOWN,
      complete: typeof row.complete === "boolean" ? row.complete : UNKNOWN,
      ath_market_cap: asNumber(row.ath_market_cap) === null ? UNKNOWN : row.ath_market_cap,
      reply_count: asNumber(row.reply_count) === null ? UNKNOWN : row.reply_count,
      last_trade_timestamp: asNumber(row.last_trade_timestamp),
      sol_price_usd: solPriceUsd(row.usd_market_cap, row.market_cap),
      final_stretch: UNKNOWN,
      buys: UNKNOWN,
      sells: UNKNOWN,
      unique_buyers: UNKNOWN,
      unique_sellers: UNKNOWN,
    },
  };
}

export function quoteIsSol(pair: Record<string, unknown>): boolean {
  const quote = pair.quoteToken as { symbol?: string } | undefined;
  const symbol = String(quote?.symbol || "").toUpperCase();
  return symbol === "SOL" || symbol === "WSOL";
}

export function normalizeDexPair(pair: unknown, nowMs: number): NormalizedToken | null {
  if (!pair || typeof pair !== "object") return null;
  const row = pair as Record<string, unknown>;
  if (row.chainId !== "solana") return null;
  const dex = String(row.dexId || "").toLowerCase();
  if (dex !== "pumpfun" && dex !== "pumpswap" && dex !== "pump") return null;
  const base = row.baseToken as { address?: string; symbol?: string; name?: string } | undefined;
  if (!base || !validMint(base.address)) return null;
  const program = dex === "pumpswap" ? "pumpswap" : "pump";
  const migration = dex === "pumpswap" ? "migrated" : UNKNOWN;
  const price = quoteIsSol(row) ? asNumber(row.priceNative) : null;
  const liquidity = row.liquidity as { quote?: unknown; usd?: unknown } | undefined;
  const liq = quoteIsSol(row) ? asNumber(liquidity?.quote) : null;
  const volume = row.volume as { m5?: unknown; h1?: unknown } | undefined;
  const txns = row.txns as { m5?: { buys?: unknown; sells?: unknown } } | undefined;
  const created = asNumber(row.pairCreatedAt);
  const usd = asNumber(row.marketCap);
  const symbol = typeof base.symbol === "string" ? base.symbol : null;
  const name = typeof base.name === "string" ? base.name : null;
  const volM5 = asNumber(volume?.m5);
  const volH1 = asNumber(volume?.h1);
  return {
    candidate_id: base.address,
    mint: base.address,
    symbol,
    name,
    source: "dexscreener",
    program,
    observed_at: isoNow(nowMs),
    proposed_size_sol: 0.005,
    slippage_bps: 150,
    enriched: false,
    discovery_class: migration,
    features: {
      launchpad: "pump",
      migration_status: migration,
      market_cap_usd: usd === null ? UNKNOWN : usd,
      age_minutes: ageMinutes(created, nowMs),
      price_sol: price === null ? UNKNOWN : price,
      liquidity_sol: liq === null ? UNKNOWN : liq,
    },
    observed: {
      creation_time: created === null ? UNKNOWN : new Date(created).toISOString(),
      dex: dex,
      volume_m5_usd: volM5 === null ? UNKNOWN : volM5,
      volume_h1_usd: volH1 === null ? UNKNOWN : volH1,
      buys: asNumber(txns?.m5?.buys) === null ? UNKNOWN : txns?.m5?.buys,
      sells: asNumber(txns?.m5?.sells) === null ? UNKNOWN : txns?.m5?.sells,
      unique_buyers: UNKNOWN,
      unique_sellers: UNKNOWN,
      final_stretch: UNKNOWN,
      activity: unusuallyActive(volM5, volH1) ? "unusually_active" : UNKNOWN,
    },
  };
}

export function unusuallyActive(volumeM5: number | null, volumeH1: number | null): boolean {
  if (volumeM5 !== null && volumeM5 >= 10_000) return true;
  if (volumeH1 !== null && volumeH1 >= 50_000) return true;
  return false;
}

export function cheapFilter(token: NormalizedToken): { pass: boolean; reason?: string } {
  if (!validMint(token.mint)) return { pass: false, reason: "mint" };
  const pad = String(token.features.launchpad || "");
  if (pad === "meteora") return { pass: false, reason: "launchpad" };
  if (token.program !== "pump" && token.program !== "pumpswap") return { pass: false, reason: "program" };
  return { pass: true };
}

export function mergeToken(base: NormalizedToken, extra: NormalizedToken): NormalizedToken {
  const features = { ...base.features };
  for (const [key, value] of Object.entries(extra.features)) {
    if (value === undefined || value === UNKNOWN) continue;
    if (features[key] === undefined || features[key] === UNKNOWN) features[key] = value;
  }
  const observed = { ...base.observed };
  for (const [key, value] of Object.entries(extra.observed)) {
    if (value === undefined || value === UNKNOWN || value === null) continue;
    if (observed[key] === undefined || observed[key] === UNKNOWN || observed[key] === null) observed[key] = value;
  }
  const migration = features.migration_status === UNKNOWN ? extra.features.migration_status : features.migration_status;
  features.migration_status = migration;
  const program = base.program === "pump" && extra.program === "pumpswap" && features.migration_status === "migrated"
    ? "pumpswap"
    : base.program;
  return {
    ...base,
    symbol: base.symbol || extra.symbol,
    name: base.name || extra.name,
    program,
    discovery_class: String(features.migration_status || base.discovery_class),
    features,
    observed,
  };
}

export function applyVolumeSol(token: NormalizedToken): NormalizedToken {
  if (token.features.volume_sol_5m !== undefined && token.features.volume_sol_5m !== UNKNOWN) return token;
  const usd = asNumber(token.observed.volume_m5_usd);
  const solUsd = asNumber(token.observed.sol_price_usd);
  if (usd === null || solUsd === null || solUsd <= 0) return token;
  const volume = usd / solUsd;
  if (!Number.isFinite(volume) || volume < 0) return token;
  return { ...token, features: { ...token.features, volume_sol_5m: volume } };
}

export function applyQuote(token: NormalizedToken, quote: { priceImpactPct?: unknown; outAmount?: unknown; error?: unknown }): NormalizedToken {
  if (quote.error) {
    return { ...token, features: { ...token.features, route_ok: false } };
  }
  const impact = asNumber(quote.priceImpactPct);
  const outAmount = asNumber(quote.outAmount);
  if (impact === null || outAmount === null || outAmount <= 0) return token;
  // Jupiter documents 0.01 as 1 percent. Convert that fraction to basis points.
  const bps = impact * 10_000;
  if (!Number.isFinite(bps) || bps < 0) return token;
  return { ...token, features: { ...token.features, route_ok: true, price_impact_bps: bps } };
}

export function applyHolders(
  token: NormalizedToken,
  info: { mintAuthority: unknown; freezeAuthority: unknown; supply: unknown; decimals: unknown } | null,
  largest: { amount: string }[] | null,
): NormalizedToken {
  if (!info && !largest) return token;
  const features = { ...token.features };
  if (info) {
    if (info.mintAuthority === null) features.mint_authority = null;
    else if (typeof info.mintAuthority === "string" && info.mintAuthority) features.mint_authority = "present";
    if (info.freezeAuthority === null) features.freeze_authority = null;
    else if (typeof info.freezeAuthority === "string" && info.freezeAuthority) features.freeze_authority = "present";
  }
  const supply = info ? asNumber(info.supply) : null;
  if (largest && supply !== null && supply > 0) {
    const amounts = largest
      .map((row) => asNumber(row.amount))
      .filter((value): value is number => value !== null && value >= 0);
    if (amounts.length) {
      const top = amounts[0] / supply;
      const top10 = amounts.slice(0, 10).reduce((sum, value) => sum + value, 0) / supply;
      if (top >= 0 && top <= 1) features.top_holder_pct = top;
      if (top10 >= 0 && top10 <= 1) features.top10_holder_pct = top10;
    }
  }
  return { ...token, features };
}
