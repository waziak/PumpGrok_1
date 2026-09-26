/**
 * Live entrypoint. Refuses unless the gate is explicitly open, and even then
 * this CLI does not attach a network transport or sign.
 */

import { pathToFileURL } from "node:url";

import { describeLive, liveGate } from "./gate.ts";
import { submitBundle } from "./jito.ts";
import type { JitoTransport } from "./jito.ts";
import { ReceiptLog, reconcileSignature } from "./receipts.ts";
import { evaluateExecutionRisk } from "./risk-engine.ts";
import { buildPlan } from "./transaction-builder.ts";

export type RpcTransport = {
  getBalance: (owner: string) => Promise<number>;
  simulate: (plan: unknown) => Promise<{ ok: boolean; error?: string }>;
  send: (signed: string) => Promise<{ signature?: string }>;
  confirm: (signature: string) => Promise<"confirmed" | "failed" | null>;
};

export function refusingRpc(): RpcTransport {
  return {
    async getBalance() {
      throw new Error("rpc_disabled");
    },
    async simulate() {
      throw new Error("rpc_disabled");
    },
    async send() {
      throw new Error("rpc_disabled");
    },
    async confirm() {
      throw new Error("rpc_disabled");
    },
  };
}

export async function executePlan(input: {
  env: Record<string, string | undefined>;
  candidate: Record<string, unknown>;
  rpc: RpcTransport;
  jito: JitoTransport;
  receipts: ReceiptLog;
  receiptId: string;
  signedTransaction?: string;
  nowMs: number;
  openExposureSol: number;
  walletSol: number | null;
  owner?: string;
}): Promise<Record<string, unknown>> {
  const gate = liveGate(input.env);
  if (!gate.open) {
    return { ok: false, refused: true, reasons: gate.reasons, signed: false, sent: false, retry: false, realTrades: false };
  }
  if (input.receipts.get(input.receiptId)) {
    return { ok: false, error: "duplicate_receipt", sent: false, retry: false, realTrades: false };
  }
  const risk = evaluateExecutionRisk({
    candidate: input.candidate,
    nowMs: input.nowMs,
    openExposureSol: input.openExposureSol,
    walletSol: input.walletSol,
  });
  if (risk.decision !== "PASS" || risk.sizeSol === null) {
    input.receipts.record({
      receiptId: input.receiptId,
      status: "refused",
      signature: null,
      retry: false,
      payload: { reasons: risk.reasons, sent: false },
    });
    return { ok: false, decision: "REJECT", reasons: risk.reasons, sent: false, signed: false, retry: false, realTrades: false };
  }
  const observedAt = String(input.candidate.observed_at || "");
  const slippage = typeof input.candidate.slippage_bps === "number" ? input.candidate.slippage_bps : 100;
  const plan = buildPlan({
    program: String(input.candidate.program || ""),
    sizeSol: risk.sizeSol,
    slippageBps: slippage,
    observedAt,
    nowMs: input.nowMs,
  });
  if (!plan.ok) {
    return { ok: false, error: plan.error, sent: false, signed: false, retry: false, realTrades: false };
  }
  let balance = input.walletSol;
  try {
    if (input.owner) balance = await input.rpc.getBalance(input.owner);
    const simulation = await input.rpc.simulate(plan);
    if (!simulation.ok) {
      return { ok: false, error: simulation.error || "rpc_failure", sent: false, signed: false, retry: false, realTrades: false };
    }
  } catch {
    return { ok: false, error: "rpc_failure", sent: false, signed: false, retry: false, realTrades: false, balance };
  }
  const reserve = evaluateExecutionRisk({
    candidate: input.candidate,
    nowMs: input.nowMs,
    openExposureSol: input.openExposureSol,
    walletSol: balance,
  });
  if (reserve.decision !== "PASS") {
    return { ok: false, decision: "REJECT", reasons: reserve.reasons, sent: false, signed: false, retry: false, realTrades: false };
  }
  const signedTransaction = input.signedTransaction;
  if (!signedTransaction) {
    input.receipts.record({
      receiptId: input.receiptId,
      status: "simulated",
      signature: null,
      retry: false,
      payload: { sent: false, simulated: true },
    });
    return {
      ok: true,
      simulated: true,
      sent: false,
      signed: false,
      retry: false,
      realTrades: false,
      note: "simulation stopped before broadcast; this package does not invent a signed transaction",
    };
  }
  let sent: { signature?: string };
  try {
    sent = await input.rpc.send(signedTransaction);
  } catch {
    input.receipts.record({
      receiptId: input.receiptId,
      status: "failed",
      signature: null,
      retry: false,
      payload: { error: "rpc_failure", sent: false },
    });
    return { ok: false, error: "rpc_failure", sent: false, signed: true, retry: false, realTrades: false };
  }
  if (!sent.signature) {
    const bundle = await submitBundle(input.jito, [signedTransaction]);
    input.receipts.record({
      receiptId: input.receiptId,
      status: bundle.status === "submitted" ? "submitted" : bundle.status === "uncertain" ? "uncertain" : "failed",
      signature: null,
      retry: false,
      payload: { bundleId: bundle.bundleId, error: bundle.error || null },
    });
    return {
      ok: bundle.ok,
      error: bundle.error,
      status: bundle.status,
      bundleId: bundle.bundleId,
      sent: false,
      retry: false,
      realTrades: false,
    };
  }
  let confirmation: "confirmed" | "failed" | null = null;
  let rpcThrew = false;
  try {
    confirmation = await input.rpc.confirm(sent.signature);
  } catch {
    rpcThrew = true;
  }
  const reconciled = reconcileSignature(confirmation, rpcThrew);
  input.receipts.record({
    receiptId: input.receiptId,
    status: reconciled.status,
    signature: sent.signature,
    retry: false,
    payload: { sent: true },
  });
  return {
    ok: reconciled.status === "submitted",
    status: reconciled.status,
    signature: sent.signature,
    retry: false,
    sent: true,
    realTrades: false,
  };
}

function main(): number {
  const report = describeLive(process.env);
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  return report.refused ? 2 : 0;
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) {
  process.exit(main());
}
