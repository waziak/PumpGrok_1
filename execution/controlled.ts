/**
 * Build, simulate, sign, and optionally submit.
 * Broadcast happens only when TRADING_MODE=live and LIVE_NETWORK_SEND=true.
 * The paper bot does not import this module.
 */

import { compileLegacy } from "./message.ts";
import { liveGate, signGate } from "./gate.ts";
import type { JitoTransport } from "./jito.ts";
import { submitBundle } from "./jito.ts";
import type { RpcTransport } from "./live.ts";
import { ReceiptLog, reconcileSignature } from "./receipts.ts";
import { evaluateExecutionRisk } from "./risk-engine.ts";
import { blockhashExpired } from "./safety.ts";
import { buildPlan } from "./transaction-builder.ts";
import { signTransaction } from "./wallet.ts";
import { associatedTokenAddress } from "./pda.ts";
import {
  WSOL,
  TOKEN_PROGRAM,
  buildPumpInstruction,
  buildPumpSwapInstruction,
  closeTokenAccount,
  createAtaIdempotent,
  syncNative,
  systemTransfer,
  type Wire,
  type WireInstruction,
} from "./venues.ts";

export type TradeRequest = {
  venue: "pump" | "pumpswap";
  side: "buy" | "sell";
  user: string;
  baseMint: string;
  creator?: string;
  coinCreator?: string;
  baseTokenProgram?: string;
  quoteMint?: string;
  quoteTokenProgram?: string;
  feeRecipient?: string;
  buybackFeeRecipient?: string;
  protocolFeeRecipient?: string;
  pool?: string;
  mayhem?: boolean;
  cashback?: boolean;
  amount: bigint;
  limitLamports: bigint;
};

function assemble(trade: TradeRequest): Wire {
  if (trade.venue === "pump") {
    if (!trade.creator || !trade.baseTokenProgram) {
      return { ok: false, wireReady: false, error: "missing_accounts", missing: [
        ...(!trade.creator ? ["creator"] : []),
        ...(!trade.baseTokenProgram ? ["baseTokenProgram"] : []),
      ], sent: false };
    }
    return buildPumpInstruction({
      side: trade.side,
      user: trade.user,
      baseMint: trade.baseMint,
      creator: trade.creator,
      baseTokenProgram: trade.baseTokenProgram,
      quoteMint: trade.quoteMint,
      quoteTokenProgram: trade.quoteTokenProgram,
      feeRecipient: trade.feeRecipient,
      buybackFeeRecipient: trade.buybackFeeRecipient,
      mayhem: trade.mayhem,
      amount: trade.amount,
      limitLamports: trade.limitLamports,
    });
  }
  if (!trade.coinCreator || !trade.baseTokenProgram || !trade.protocolFeeRecipient) {
    return { ok: false, wireReady: false, error: "missing_accounts", missing: [
      ...(!trade.coinCreator ? ["coinCreator"] : []),
      ...(!trade.baseTokenProgram ? ["baseTokenProgram"] : []),
      ...(!trade.protocolFeeRecipient ? ["protocolFeeRecipient"] : []),
    ], sent: false };
  }
  return buildPumpSwapInstruction({
    side: trade.side,
    user: trade.user,
    baseMint: trade.baseMint,
    quoteMint: trade.quoteMint,
    coinCreator: trade.coinCreator,
    baseTokenProgram: trade.baseTokenProgram,
    quoteTokenProgram: trade.quoteTokenProgram,
    protocolFeeRecipient: trade.protocolFeeRecipient,
    buybackFeeRecipient: trade.buybackFeeRecipient,
    pool: trade.pool,
    cashback: trade.cashback,
    amount: trade.amount,
    limitLamports: trade.limitLamports,
  });
}

export function prepareInstructions(trade: TradeRequest, wire: WireInstruction): WireInstruction[] {
  const instructions: WireInstruction[] = [];
  const quoteMint = trade.quoteMint || WSOL;
  const quoteProgram = trade.quoteTokenProgram || TOKEN_PROGRAM;
  if (trade.baseTokenProgram && (trade.side === "buy" || quoteMint === WSOL)) {
    if (trade.side === "buy") {
      instructions.push(createAtaIdempotent(trade.user, trade.user, trade.baseMint, trade.baseTokenProgram));
    }
    instructions.push(createAtaIdempotent(trade.user, trade.user, quoteMint, quoteProgram));
  }
  if (trade.venue === "pumpswap" && trade.side === "buy" && quoteMint === WSOL) {
    const wsolAta = associatedTokenAddress(trade.user, WSOL, TOKEN_PROGRAM);
    instructions.push(systemTransfer(trade.user, wsolAta, trade.limitLamports));
    instructions.push(syncNative(wsolAta));
  }
  instructions.push(wire);
  if (trade.venue === "pumpswap" && trade.side === "sell" && quoteMint === WSOL) {
    const wsolAta = associatedTokenAddress(trade.user, WSOL, TOKEN_PROGRAM);
    instructions.push(closeTokenAccount(wsolAta, trade.user, trade.user));
  }
  return instructions;
}

export async function runControlled(input: {
  env: Record<string, string | undefined>;
  candidate: Record<string, unknown>;
  trade: TradeRequest;
  rpc: RpcTransport & { simulateWire?: (txB64: string) => Promise<{ ok: boolean; error?: string }> };
  jito: JitoTransport;
  receipts: ReceiptLog;
  receiptId: string;
  nowMs: number;
  openExposureSol: number;
  walletSol: number | null;
  blockhash: Uint8Array;
  blockhashFetchedAtMs: number;
  keypairPath?: string;
  uncertainSignature?: string;
}): Promise<Record<string, unknown>> {
  const sendGate = liveGate(input.env);
  const signing = signGate(input.env);
  const risk = evaluateExecutionRisk({
    candidate: input.candidate,
    nowMs: input.nowMs,
    openExposureSol: input.openExposureSol,
    walletSol: input.walletSol,
  });
  if (risk.decision !== "PASS" || risk.sizeSol === null) {
    return { ok: false, decision: "REJECT", reasons: risk.reasons, sent: false, signed: false, retry: false, realTrades: false, wireReady: false };
  }
  const slippage = typeof input.candidate.slippage_bps === "number" ? input.candidate.slippage_bps : 100;
  const plan = buildPlan({
    program: input.trade.venue,
    sizeSol: risk.sizeSol,
    slippageBps: slippage,
    observedAt: String(input.candidate.observed_at || ""),
    nowMs: input.nowMs,
  });
  if (!plan.ok) {
    return { ok: false, error: plan.error, sent: false, signed: false, retry: false, realTrades: false, wireReady: false };
  }
  const wire = assemble(input.trade);
  if (!wire.ok) {
    return { ok: false, error: wire.error, missing: wire.missing, sent: false, signed: false, retry: false, realTrades: false, wireReady: false };
  }
  const instructions = prepareInstructions(input.trade, wire);
  let compiled;
  try {
    compiled = compileLegacy(instructions, input.trade.user, input.blockhash);
  } catch {
    return { ok: false, error: "compile_failed", sent: false, signed: false, retry: false, realTrades: false, wireReady: false };
  }
  const unsigned = Buffer.from(compiled.message).toString("base64");
  const simulate = input.rpc.simulateWire || (async () => input.rpc.simulate({ wire }));
  let simulation: { ok: boolean; error?: string };
  try {
    simulation = await simulate(unsigned);
  } catch {
    return { ok: false, error: "rpc_failure", sent: false, signed: false, retry: false, realTrades: false, wireReady: true };
  }
  if (!simulation.ok) {
    return { ok: false, error: simulation.error || "simulation_failed", sent: false, signed: false, retry: false, realTrades: false, wireReady: true };
  }
  if (!signing.open || !input.keypairPath) {
    input.receipts.record({
      receiptId: input.receiptId,
      status: "simulated",
      signature: null,
      retry: false,
      payload: { sent: false, simulated: true, wireReady: true },
    });
    return {
      ok: true,
      simulated: true,
      sent: false,
      signed: false,
      retry: false,
      realTrades: false,
      wireReady: true,
      instruction: wire.instruction,
      accounts: wire.accounts.length,
    };
  }
  const signed = signTransaction(input.keypairPath, compiled.message, input.env);
  if (!signed.ok) {
    return { ok: false, error: signed.error, sent: false, signed: false, retry: false, realTrades: false, wireReady: true, secretMaterialExposed: false };
  }
  if (blockhashExpired(input.blockhashFetchedAtMs, input.nowMs)) {
    return { ok: false, error: "blockhash_expired", sent: false, signed: true, retry: false, realTrades: false, wireReady: true };
  }
  if (input.uncertainSignature && input.receipts.findUncertainSignature(input.uncertainSignature)) {
    return { ok: false, error: "uncertain_no_resend", sent: false, signed: true, retry: false, realTrades: false, wireReady: true };
  }
  if (!sendGate.open) {
    input.receipts.record({
      receiptId: input.receiptId,
      status: "simulated",
      signature: null,
      retry: false,
      payload: { sent: false, signed: true, broadcast: false },
    });
    return {
      ok: true,
      simulated: true,
      sent: false,
      signed: true,
      retry: false,
      realTrades: false,
      wireReady: true,
      instruction: wire.instruction,
      secretMaterialExposed: false,
    };
  }
  if (input.receipts.get(input.receiptId)) {
    return { ok: false, error: "duplicate_receipt", sent: false, signed: true, retry: false, realTrades: false };
  }
  let sent: { signature?: string };
  try {
    sent = await input.rpc.send(signed.transactionB64);
  } catch {
    input.receipts.record({
      receiptId: input.receiptId,
      status: "failed",
      signature: null,
      retry: false,
      payload: { error: "rpc_failure", sent: false },
    });
    return { ok: false, error: "rpc_failure", sent: false, signed: true, retry: false, realTrades: false, wireReady: true };
  }
  if (!sent.signature) {
    const bundle = await submitBundle(input.jito, [signed.transactionB64]);
    input.receipts.record({
      receiptId: input.receiptId,
      status: bundle.status === "submitted" ? "submitted" : bundle.status === "uncertain" ? "uncertain" : "failed",
      signature: null,
      retry: false,
      payload: { bundleId: bundle.bundleId, error: bundle.error || null },
    });
    return { ok: bundle.ok, error: bundle.error, status: bundle.status, sent: false, signed: true, retry: false, realTrades: false, wireReady: true };
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
    sent: true,
    signed: true,
    retry: false,
    realTrades: false,
    wireReady: true,
  };
}
