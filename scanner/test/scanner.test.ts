import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { qualitativeNotes } from "../../bot/grok.ts";
import { PAPER_BOT_READS_KEYPAIR } from "../../bot/run.ts";
import { HttpProvider, withBackoff } from "../provider.ts";
import { connectPumpPortal, discoverPumpCoins } from "../pump.ts";
import { cheapFilter, curveLiquiditySol, normalizeDexPair, normalizePumpCoin } from "../token-state.ts";

const NOW = Date.parse("2026-09-26T18:00:00Z");
const MINT = "DszJTd7XZn4rc4t6Cbm5rzrWw5stbD7G3T19Kgjipump";

test("pump coin keeps unknown reserves unknown and does not invent final stretch", () => {
  const good = normalizePumpCoin(
    {
      mint: MINT,
      symbol: "INUX",
      name: "Example",
      created_timestamp: NOW - 60_000,
      complete: false,
      usd_market_cap: 3410,
      market_cap: 28.14,
      ath_market_cap: 4000,
      is_holder_reward: false,
      real_sol_reserves: 97_774_557,
      virtual_sol_reserves: 30_097_774_557,
      total_supply: 1_000_000_000_000_000,
      base_decimals: 6,
      creator: "418FJkS2vDyVkuRCEpR6JHfgaofSjBhhsBEDFf5k7oWg",
    },
    NOW,
  );
  assert.ok(good);
  assert.equal(good?.features.migration_status, "new_pair");
  assert.equal(good?.observed.final_stretch, "UNKNOWN");
  assert.ok(Math.abs(Number(good?.features.liquidity_sol) - 0.097774557) < 1e-9);
  assert.equal(good?.features.unique_buyers, undefined);
  assert.equal(good?.observed.unique_buyers, "UNKNOWN");
  const bad = curveLiquiditySol({ complete: false, real_sol_reserves: 0, virtual_sol_reserves: 239_733_729_941 });
  assert.equal(bad, "UNKNOWN");
});

test("dex screener drops non pump venues", () => {
  const meteora = normalizeDexPair(
    {
      chainId: "solana",
      dexId: "meteora",
      baseToken: { address: MINT, symbol: "NO" },
      quoteToken: { symbol: "SOL" },
    },
    NOW,
  );
  assert.equal(meteora, null);
  const pair = normalizeDexPair(
    {
      chainId: "solana",
      dexId: "pumpswap",
      baseToken: { address: MINT, symbol: "YES" },
      quoteToken: { symbol: "SOL" },
      priceNative: "0.0001",
      liquidity: { quote: 12.5 },
      marketCap: 40000,
      volume: { m5: 20000, h1: 80000 },
      txns: { m5: { buys: 10, sells: 4 } },
    },
    NOW,
  );
  assert.equal(pair?.program, "pumpswap");
  assert.equal(pair?.features.migration_status, "migrated");
  assert.equal(pair?.observed.activity, "unusually_active");
  assert.equal(pair?.features.liquidity_sol, 12.5);
  assert.equal(cheapFilter(pair!).pass, true);
});

test("provider retries then succeeds", async () => {
  let calls = 0;
  const slept: number[] = [];
  const value = await withBackoff(
    async () => {
      calls += 1;
      if (calls < 3) throw new Error("down");
      return "ok";
    },
    { attempts: 3, baseMs: 5, sleep: async (ms) => { slept.push(ms); } },
  );
  assert.equal(value, "ok");
  assert.equal(calls, 3);
  assert.deepEqual(slept, [5, 10]);
  const provider = new HttpProvider({
    attempts: 2,
    sleep: async () => {},
    fetchImpl: async () => {
      throw new Error("down");
    },
  });
  const failed = await provider.getJson("https://example.invalid/coins");
  assert.equal(failed.ok, false);
});

test("pump portal reconnects after a socket error", async () => {
  let made = 0;
  class FakeSocket {
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: unknown }) => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    onclose: (() => void) | null = null;
    url: string;
    constructor(url: string) {
      this.url = url;
      made += 1;
      const self = this;
      queueMicrotask(() => {
        if (made === 1) self.onerror?.(new Error("down"));
        else self.onopen?.();
      });
    }
    send(data: string) {
      assert.equal(JSON.parse(data).method, "subscribeNewToken");
    }
    close() {
      this.onclose?.();
    }
  }
  const handle = connectPumpPortal({
    WebSocketImpl: FakeSocket,
    onToken() {},
    sleep: async () => {},
    maxAttempts: 3,
  });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.ok(handle.attempts() >= 2);
  handle.stop();
});

test("mocked discovery is not a live pass", async () => {
  const provider = new HttpProvider({
    attempts: 1,
    fetchImpl: async () =>
      new Response(
        JSON.stringify([
          {
            mint: MINT,
            symbol: "INUX",
            created_timestamp: NOW - 60_000,
            complete: false,
            usd_market_cap: 3410,
            market_cap: 28,
            real_sol_reserves: 97_774_557,
            virtual_sol_reserves: 30_097_774_557,
            total_supply: 1_000_000_000_000_000,
            base_decimals: 6,
          },
        ]),
        { status: 200 },
      ),
  });
  const found = await discoverPumpCoins(provider, { nowMs: NOW });
  assert.equal(found.ok, true);
  assert.equal(found.tokens[0]?.mint, MINT);
  assert.equal(found.tokens[0]?.source, "pump.fun");
});

test("grok missing key and timeout do not leak the key", async () => {
  const missing = await qualitativeNotes({}, [{ mint: MINT }]);
  assert.equal(missing.reason, "GROK_API_KEY_REQUIRED");
  assert.equal(missing.available, false);
  const secret = "sk-test-value-do-not-leak-0001";
  const timed = await qualitativeNotes({ XAI_API_KEY: secret }, [{ mint: MINT }], {
    timeoutMs: 20,
    fetchImpl: (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          const error = new Error("aborted");
          error.name = "AbortError";
          reject(error);
        });
      }),
  });
  assert.equal(timed.reason, "GROK_TIMEOUT");
  assert.equal(JSON.stringify(timed).includes(secret), false);
  assert.equal(PAPER_BOT_READS_KEYPAIR, false);
  const source = readFileSync(new URL("../../bot/run.ts", import.meta.url), "utf8");
  assert.equal(source.includes("wallet.ts"), false);
  assert.equal(source.includes("readFileSync"), false);
});
