import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { describeLive, LIVE_CONFIRM, NETWORK_CONFIRM } from "../gate.ts";
import { submitBundle } from "../jito.ts";
import { executePlan, refusingRpc } from "../live.ts";
import { PositionBook } from "../positions.ts";
import { ReceiptLog, reconcileSignature } from "../receipts.ts";
import { HARD_CAPS, evaluateExecutionRisk } from "../risk-engine.ts";
import { buildPlan } from "../transaction-builder.ts";
import { loadPublicPreview, signMessage } from "../wallet.ts";

const NOW = Date.parse("2026-09-26T18:00:00Z");
const MINT = "9PaperMint1111111111111111111111111111111";

function candidate(extra: Record<string, unknown> = {}) {
  return {
    candidate_id: "cand-exec",
    mint: MINT,
    program: "pump",
    observed_at: "2026-09-26T18:00:00Z",
    proposed_size_sol: 0.005,
    slippage_bps: 100,
    features: {
      mint_authority: "revoked",
      freeze_authority: "revoked",
      liquidity_sol: 20,
      top_holder_pct: 0.05,
      route_ok: true,
      price_impact_bps: 20,
      price_sol: 0.00002,
    },
    ...extra,
  };
}

function openEnv(): Record<string, string> {
  return {
    TRADING_MODE: "live",
    LIVE_TRADING_CONFIRM: LIVE_CONFIRM,
    LIVE_NETWORK_SEND: NETWORK_CONFIRM,
  };
}

test("live refuses by default", () => {
  const report = describeLive({});
  assert.equal(report.refused, true);
  assert.equal(report.sent, false);
  assert.equal(report.signed, false);
  assert.equal(report.realTrades, false);
  assert.equal(report.privateKeyExposed, false);
  const partial = describeLive({ TRADING_MODE: "live" });
  assert.equal(partial.refused, true);
  assert.equal(partial.sent, false);
});

test("hard caps stay at the ceiling", () => {
  assert.equal(HARD_CAPS.MAX_BUY_SOL, 0.005);
  assert.equal(HARD_CAPS.MAX_TOTAL_EXPOSURE_SOL, 0.03);
  assert.equal(HARD_CAPS.MIN_SOL_RESERVE, 0.02);
  assert.equal(HARD_CAPS.PAPER_BUY_SOL, 0.005);
});

test("stale signal is refused before send", () => {
  const plan = buildPlan({
    program: "pump",
    sizeSol: 0.005,
    slippageBps: 100,
    observedAt: "2026-09-26T17:00:00Z",
    nowMs: NOW,
  });
  assert.equal(plan.ok, false);
  if (!plan.ok) assert.equal(plan.error, "stale_candidate");
});

test("rpc failure does not sign or send", async () => {
  const receipts = new ReceiptLog();
  let sends = 0;
  const result = await executePlan({
    env: openEnv(),
    candidate: candidate(),
    rpc: {
      async getBalance() {
        throw new Error("down");
      },
      async simulate() {
        throw new Error("down");
      },
      async send() {
        sends += 1;
        return { signature: "should-not-send" };
      },
      async confirm() {
        return "confirmed";
      },
    },
    jito: { async sendBundle() { throw new Error("jito_disabled"); } },
    receipts,
    receiptId: "r-rpc",
    nowMs: NOW,
    openExposureSol: 0,
    walletSol: 0.1,
    owner: "owner",
  });
  assert.equal(result.error, "rpc_failure");
  assert.equal(result.sent, false);
  assert.equal(result.retry, false);
  assert.equal(sends, 0);
  assert.equal(JSON.stringify(result).includes("should-not-send"), false);
});

test("jito failure is not retried", async () => {
  let calls = 0;
  const result = await submitBundle(
    {
      async sendBundle() {
        calls += 1;
        throw new Error("block engine down");
      },
    },
    ["dGhpcy1pcy1ub3QtYS1yZWFsLXR4"],
  );
  assert.equal(result.error, "jito_failure");
  assert.equal(result.retry, false);
  assert.equal(calls, 1);
  const again = await submitBundle(
    {
      async sendBundle() {
        return { bundleId: "bundle-1" };
      },
      async bundleStatus() {
        return "Pending";
      },
    },
    ["dGhpcy1pcy1ub3QtYS1yZWFsLXR4"],
  );
  assert.equal(again.status, "uncertain");
  assert.equal(again.retry, false);
});

test("uncertain transaction is not treated as a fill", () => {
  assert.deepEqual(reconcileSignature(null, false), { status: "uncertain", retry: false });
  assert.deepEqual(reconcileSignature(undefined, true), { status: "uncertain", retry: false });
  assert.equal(reconcileSignature("confirmed", false).retry, false);
});

test("duplicate receipt and restart recovery", () => {
  const log = new ReceiptLog();
  const first = log.record({
    receiptId: "r1",
    status: "uncertain",
    signature: null,
    retry: false,
    payload: { sent: false },
  });
  assert.equal(first.ok, true);
  const second = log.record({
    receiptId: "r1",
    status: "submitted",
    signature: "sig",
    retry: false,
    payload: {},
  });
  assert.equal(second.ok, false);
  if (!second.ok) assert.equal(second.error, "duplicate_receipt");
  const restored = ReceiptLog.load(log.save());
  assert.equal(restored.get("r1")?.status, "uncertain");
  const book = new PositionBook();
  book.upsert({ positionId: "p1", mint: MINT, sizeSol: 0.005, remainingFraction: 1, status: "open" });
  const reloaded = PositionBook.load(book.save());
  assert.equal(reloaded.exposure(), 0.005);
});

test("sol reserve and excess size reject", () => {
  const reserve = evaluateExecutionRisk({
    candidate: candidate(),
    nowMs: NOW,
    openExposureSol: 0,
    walletSol: 0.024,
  });
  assert.equal(reserve.decision, "REJECT");
  assert.equal(reserve.reasons.includes("sol_reserve"), true);
  const size = evaluateExecutionRisk({
    candidate: candidate({ proposed_size_sol: 0.02 }),
    nowMs: NOW,
    openExposureSol: 0,
    walletSol: 1,
  });
  assert.equal(size.reasons.includes("excess_size"), true);
});

test("inaccessible keypair path does not throw secret material", () => {
  const missing = loadPublicPreview(join(tmpdir(), "does-not-exist-pumpgrok-wallet.json"));
  assert.equal(missing.ok, false);
  if (!missing.ok) {
    assert.equal(missing.error, "inaccessible_keypair");
    assert.equal(missing.secretMaterialExposed, false);
  }
  const closed = signMessage(join(tmpdir(), "missing.json"), Buffer.from("paper"), {});
  assert.equal(closed.ok, false);
  if (!closed.ok) assert.equal(closed.error, "sign_refused");

  const dir = mkdtempSync(join(tmpdir(), "pumpgrok-wallet-"));
  const file = join(dir, "wallet.json");
  const bytes = [...randomBytes(64)];
  writeFileSync(file, JSON.stringify(bytes));
  try {
    const signed = signMessage(file, Buffer.from("paper-only"), openEnv());
    assert.equal(signed.ok, true);
    const encoded = JSON.stringify(signed);
    assert.equal(encoded.includes(JSON.stringify(bytes)), false);
    assert.equal(encoded.includes(bytes.slice(0, 8).join(",")), false);
    if (signed.ok) assert.equal(signed.secretMaterialExposed, false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("default rpc transport is disabled", async () => {
  const rpc = refusingRpc();
  await assert.rejects(() => rpc.simulate({}), /rpc_disabled/);
});
