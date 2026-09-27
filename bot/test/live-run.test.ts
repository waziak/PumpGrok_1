import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { runLiveBot } from "../live-run.ts";
import { resolveTrade } from "../resolve-trade.ts";
import type { LiveRpc } from "../../execution/rpc.ts";
import { PUBLIC_WALLET } from "../../scanner/token-state.ts";
import type { NormalizedToken } from "../../scanner/token-state.ts";

const NOW = Date.parse("2026-09-26T18:00:00Z");
const MINT = "6MwwrxeHWTwwSqWQp1huASBYF1F1azGQjva6iEWkpump";

function token(): NormalizedToken {
  return {
    candidate_id: MINT,
    mint: MINT,
    symbol: "PAPER",
    name: "Paper",
    source: "pump.fun",
    program: "pump",
    observed_at: "2026-09-26T18:00:00Z",
    proposed_size_sol: 0.005,
    slippage_bps: 100,
    enriched: true,
    discovery_class: "new_pair",
    features: {
      mint_authority: "revoked",
      freeze_authority: "revoked",
      liquidity_sol: 20,
      top_holder_pct: 0.05,
      route_ok: true,
      price_impact_bps: 20,
      price_sol: 0.00002,
      market_cap_usd: 12000,
      volume_sol_5m: 8,
    },
    observed: {},
  };
}

function qualified() {
  return {
    ok: true,
    qualified_live: [{
      mint: MINT,
      program: "pump",
      strategy_id: "prebond-volume-regime",
      execution_class: "FAST",
      size_sol: 0.005,
      slippage_bps: 100,
      price_sol: 0.00002,
      symbol: "PAPER",
      qualified: true,
    }],
    scope: [{
      mint: MINT,
      symbol: "PAPER",
      program: "pump",
      strategy_id: "prebond-volume-regime",
      execution_class: "FAST",
      matched: true,
      decision: "PASS",
      reasons: [],
      qualified: true,
    }],
  };
}

function rpc(sends: { n: number }): LiveRpc {
  return {
    async getBalance() {
      return 1_000_000_000;
    },
    async getAccountInfo() {
      return null;
    },
    async getLatestBlockhash() {
      return { blockhash: randomBytes(32), fetchedAtMs: NOW };
    },
    async simulate() {
      return { ok: true };
    },
    async simulateWire() {
      return { ok: true };
    },
    async send() {
      sends.n += 1;
      return { signature: "live-test-signature" };
    },
    async confirm() {
      return "confirmed";
    },
  };
}

const trade = {
  venue: "pump" as const,
  side: "buy" as const,
  user: PUBLIC_WALLET,
  baseMint: MINT,
  creator: "AYjX7q4qoAxF5UqpFW75RCVWAeZjvac4gqkEPuyhAYi4",
  baseTokenProgram: "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
  amount: 1000n,
  limitLamports: 5_000_000n,
};

test("closed gate writes scope and does not send", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pumpgrok-live-"));
  const sends = { n: 0 };
  try {
    await runLiveBot({
      env: {},
      maxCycles: 1,
      serve: false,
      nowMs: NOW,
      db: join(dir, "live.sqlite"),
      statusPath: join(dir, "live-status.json"),
      eventsPath: join(dir, "live-events.jsonl"),
      sleep: async () => {},
      scan: async () => ({
        ok: true,
        live: false,
        mocked: true,
        tokensObserved: 1,
        candidates: [token()],
        marks: [],
        errors: [],
        samples: [],
        wallet: { pubkey: PUBLIC_WALLET, balanceLamports: 0 },
        scanner: {},
      }),
      qualify: async () => qualified(),
      rpc: rpc(sends),
      resolve: async () => ({ ok: true, trade }),
    });
    assert.equal(sends.n, 0);
    const status = JSON.parse(readFileSync(join(dir, "live-status.json"), "utf8"));
    assert.equal(status.live_send_enabled, false);
    assert.equal(status.mode, "paper");
    assert.equal(status.private_key_exposed, false);
    assert.equal(status.real_trades, false);
    assert.equal(status.scope[0].mint, MINT);
    assert.equal(Array.isArray(status.positions), true);
    assert.equal(Array.isArray(status.trades), true);
    const events = readFileSync(join(dir, "live-events.jsonl"), "utf8");
    assert.equal(events.includes("send_refused"), true);
    assert.equal(events.includes("live-test-signature"), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("open gate sends once and records the trade", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pumpgrok-live-"));
  const keyDir = mkdtempSync(join(tmpdir(), "pumpgrok-key-"));
  const keyFile = join(keyDir, "wallet.json");
  const secret = [...randomBytes(64)];
  writeFileSync(keyFile, JSON.stringify(secret));
  const sends = { n: 0 };
  try {
    await runLiveBot({
      env: { TRADING_MODE: "live", LIVE_NETWORK_SEND: "true", SOLANA_KEYPAIR_PATH: keyFile },
      maxCycles: 1,
      serve: false,
      nowMs: NOW,
      db: join(dir, "live.sqlite"),
      statusPath: join(dir, "live-status.json"),
      eventsPath: join(dir, "live-events.jsonl"),
      sleep: async () => {},
      scan: async () => ({
        ok: true,
        live: false,
        mocked: true,
        tokensObserved: 1,
        candidates: [token()],
        marks: [],
        errors: [],
        samples: [],
        wallet: { pubkey: PUBLIC_WALLET, balanceLamports: 1_000_000_000 },
        scanner: {},
      }),
      qualify: async () => qualified(),
      rpc: rpc(sends),
      resolve: async () => ({ ok: true, trade }),
    });
    assert.equal(sends.n, 1);
    const status = JSON.parse(readFileSync(join(dir, "live-status.json"), "utf8"));
    assert.equal(status.live_send_enabled, true);
    assert.equal(status.trades[0].sent, true);
    assert.equal(status.trades[0].signature, "live-test-signature");
    assert.equal(status.positions[0].mint, MINT);
    assert.equal(status.private_key_exposed, false);
    const encoded = JSON.stringify(status) + readFileSync(join(dir, "live-events.jsonl"), "utf8");
    assert.equal(encoded.includes(JSON.stringify(secret)), false);
    assert.equal(encoded.includes(secret.slice(0, 8).join(",")), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    rmSync(keyDir, { recursive: true, force: true });
  }
});

test("holder unknown triggers one enrich retry and a closed gate still does not send", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pumpgrok-live-"));
  const sends = { n: 0 };
  let scans = 0;
  let qualifies = 0;
  try {
    await runLiveBot({
      env: {},
      maxCycles: 1,
      serve: false,
      nowMs: NOW,
      db: join(dir, "live.sqlite"),
      statusPath: join(dir, "live-status.json"),
      eventsPath: join(dir, "live-events.jsonl"),
      sleep: async () => {},
      scan: async (opts) => {
        scans += 1;
        assert.equal(opts?.enrichLimit, scans === 1 ? 4 : 1);
        return {
          ok: true,
          live: false,
          mocked: true,
          tokensObserved: 1,
          candidates: [token()],
          marks: [],
          errors: [],
          samples: [],
          wallet: { pubkey: PUBLIC_WALLET, balanceLamports: 0 },
          scanner: {},
        };
      },
      qualify: async () => {
        qualifies += 1;
        if (qualifies === 1) {
          return {
            ok: true,
            qualified_live: [],
            scope: [{
              mint: MINT,
              symbol: "PAPER",
              program: "pump",
              strategy_id: "prebond-volume-regime",
              execution_class: "FAST",
              matched: true,
              decision: "REJECT",
              reasons: ["holder_concentration_unknown"],
              qualified: false,
            }],
          };
        }
        return qualified();
      },
      rpc: rpc(sends),
      resolve: async () => ({ ok: true, trade }),
    });
    assert.equal(scans, 2);
    assert.equal(qualifies, 2);
    assert.equal(sends.n, 0);
    const status = JSON.parse(readFileSync(join(dir, "live-status.json"), "utf8"));
    assert.equal(status.scope[0].qualified, true);
    assert.equal(status.live_send_enabled, false);
    assert.equal(status.trades.length, 0);
    const events = readFileSync(join(dir, "live-events.jsonl"), "utf8");
    assert.equal(events.includes("send_refused"), true);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("missing accounts do not send", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pumpgrok-live-"));
  const sends = { n: 0 };
  const transport = rpc(sends);
  try {
    await runLiveBot({
      env: { TRADING_MODE: "live", LIVE_NETWORK_SEND: "true" },
      maxCycles: 1,
      serve: false,
      nowMs: NOW,
      db: join(dir, "live.sqlite"),
      statusPath: join(dir, "live-status.json"),
      eventsPath: join(dir, "live-events.jsonl"),
      sleep: async () => {},
      scan: async () => ({
        ok: true,
        live: false,
        mocked: true,
        tokensObserved: 1,
        candidates: [token()],
        marks: [],
        errors: [],
        samples: [],
        wallet: { pubkey: PUBLIC_WALLET, balanceLamports: 1_000_000_000 },
        scanner: {},
      }),
      qualify: async () => qualified(),
      rpc: transport,
      resolve: (input) => resolveTrade({ ...input, getAccountInfo: transport.getAccountInfo }),
    });
    assert.equal(sends.n, 0);
    const events = readFileSync(join(dir, "live-events.jsonl"), "utf8");
    assert.equal(events.includes("missing_accounts"), true);
    const status = JSON.parse(readFileSync(join(dir, "live-status.json"), "utf8"));
    assert.equal(status.trades.length, 0);
    assert.equal(status.scope.length, 1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
