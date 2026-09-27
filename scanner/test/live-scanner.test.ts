import assert from "node:assert/strict";
import { test } from "node:test";

import { scanOnce } from "../scanner-loop.ts";

test("live scanner observes mainnet pump tokens", { timeout: 120_000 }, async () => {
  const result = await scanOnce({ enrichLimit: 1, attempts: 2, timeoutMs: 12_000 });
  assert.equal(result.mocked, false);
  assert.equal(result.live, true);
  assert.equal(result.ok, true);
  assert.ok(result.tokensObserved > 0, "expected at least one live token");
  const mint = result.candidates.find((token) => token.mint.length >= 32);
  assert.ok(mint, "expected a real mint");
  assert.ok(mint.source === "pump.fun" || mint.source === "dexscreener");
  assert.equal(JSON.stringify(result).includes("PRIVATE"), false);
});
