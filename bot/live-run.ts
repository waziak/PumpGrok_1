/**
 * Live scan, qualify, and gated broadcast loop.
 * Sends only when TRADING_MODE=live and LIVE_NETWORK_SEND=true.
 * The paper bot does not import this file.
 */

import { spawn } from "node:child_process";
import { createServer, type Server } from "node:http";
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { resolveTrade, type Resolved } from "./resolve-trade.ts";
import { runControlled, type TradeRequest } from "../execution/controlled.ts";
import { liveGate } from "../execution/gate.ts";
import { refusingJito } from "../execution/jito.ts";
import { createSolanaRpc, type LiveRpc } from "../execution/rpc.ts";
import { ReceiptLog } from "../execution/receipts.ts";
import { scanOnce, type ScanResult } from "../scanner/scanner-loop.ts";
import { PUBLIC_WALLET, type NormalizedToken } from "../scanner/token-state.ts";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const VIEW_HTML = readFileSync(new URL("./live-view.html", import.meta.url), "utf8");

export type ScopeRow = {
  mint: string;
  symbol: string | null;
  program: string;
  strategy_id: string | null;
  execution_class: string | null;
  matched: boolean;
  decision: string;
  reasons: string[];
  qualified: boolean;
};

export type LivePosition = {
  mint: string;
  venue: string;
  size_sol: number;
  signature: string | null;
  status: string;
  opened_at: string;
};

export type LiveTrade = {
  at: string;
  mint: string;
  side: string;
  sent: boolean;
  signed: boolean;
  signature: string | null;
  error: string | null;
};

type Qualified = {
  mint?: string;
  program?: string;
  strategy_id?: string;
  execution_class?: string;
  size_sol?: number;
  slippage_bps?: number;
  price_sol?: number;
  symbol?: string | null;
  qualified?: boolean;
};

type QualifyResult = {
  ok?: boolean;
  qualified_live?: Qualified[];
  scope?: ScopeRow[];
  sent?: boolean;
  real_trades?: boolean;
};

export type LiveStatus = {
  cycle: number;
  mode: string;
  live_send_enabled: boolean;
  real_trades: boolean;
  private_key_exposed: false;
  reads_keypair: boolean;
  bot_wallet: string;
  balance_lamports: number | null;
  open_exposure_sol: number;
  signal: string;
  scope: ScopeRow[];
  positions: LivePosition[];
  trades: LiveTrade[];
  errors: string[];
  view: string | null;
  updated_at: string;
  shutting_down: boolean;
};

function publicText(value: unknown): string {
  return JSON.stringify(value, (key, item) => {
    if (key !== "private_key_exposed" && /secret|mnemonic|seed_phrase|private_key/i.test(key)) return undefined;
    return item;
  });
}

function readPositions(statusPath: string): LivePosition[] {
  try {
    const parsed = JSON.parse(readFileSync(statusPath, "utf8")) as { positions?: LivePosition[] };
    return Array.isArray(parsed.positions) ? parsed.positions : [];
  } catch {
    return [];
  }
}

export function startLiveView(opts: { port: number; statusPath: string; eventsPath: string }): Promise<{ port: number; close(): Promise<void> }> {
  const server: Server = createServer((req, res) => {
    const url = req.url || "/";
    if (url === "/api/status") {
      let body = "{}";
      try {
        body = readFileSync(opts.statusPath, "utf8");
      } catch {
        body = "{}";
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(body);
      return;
    }
    if (url === "/api/events") {
      let body = "";
      try {
        body = readFileSync(opts.eventsPath, "utf8");
      } catch {
        body = "";
      }
      res.writeHead(200, { "content-type": "text/plain" });
      res.end(body);
      return;
    }
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(VIEW_HTML);
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(opts.port, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : opts.port;
      resolve({
        port,
        close: () => new Promise((done) => server.close(() => done())),
      });
    });
  });
}

function qualifyWithPython(db: string, payload: unknown): Promise<QualifyResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", ["-m", "layer", "--db", db, "cycle"], {
      cwd: ROOT,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk);
    });
    child.on("close", () => {
      try {
        resolve(JSON.parse(stdout) as QualifyResult);
      } catch {
        resolve({ ok: false, qualified_live: [], scope: [] });
      }
    });
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

function scopeFromCandidates(candidates: NormalizedToken[], qualify: QualifyResult): ScopeRow[] {
  if (Array.isArray(qualify.scope) && qualify.scope.length) return qualify.scope;
  const qualified = new Set((qualify.qualified_live || []).map((row) => row.mint));
  return candidates.slice(0, 40).map((token) => ({
    mint: token.mint,
    symbol: token.symbol,
    program: token.program,
    strategy_id: qualified.has(token.mint) ? "prebond-volume-regime" : null,
    execution_class: qualified.has(token.mint) ? "FAST" : null,
    matched: qualified.has(token.mint),
    decision: qualified.has(token.mint) ? "PASS" : "REJECT",
    reasons: qualified.has(token.mint) ? [] : ["not_qualified"],
    qualified: qualified.has(token.mint),
  }));
}

export async function runLiveBot(opts: {
  env?: Record<string, string | undefined>;
  maxCycles?: number;
  intervalMs?: number;
  db?: string;
  statusPath?: string;
  eventsPath?: string;
  sleep?: (ms: number) => Promise<void>;
  scan?: typeof scanOnce;
  qualify?: (db: string, payload: unknown) => Promise<QualifyResult>;
  rpc?: LiveRpc;
  resolve?: typeof resolveTrade;
  nowMs?: number;
  serve?: boolean;
  viewPort?: number;
  shouldStop?: () => boolean;
} = {}): Promise<Record<string, unknown>> {
  const env = opts.env || process.env;
  const maxCycles = opts.maxCycles ?? Number(env.BOT_MAX_CYCLES || 0);
  const intervalMs = opts.intervalMs ?? Number(env.BOT_INTERVAL_MS || 20_000);
  const db = opts.db || join(ROOT, "data", "pumpgrok-live.sqlite");
  const statusPath = opts.statusPath || join(ROOT, "data", "live-status.json");
  const eventsPath = opts.eventsPath || join(ROOT, "data", "live-events.jsonl");
  const sleep = opts.sleep || ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const scan = opts.scan || scanOnce;
  const qualify = opts.qualify || qualifyWithPython;
  const rpc = opts.rpc || createSolanaRpc(env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com");
  const resolve = opts.resolve || resolveTrade;
  mkdirSync(dirname(db), { recursive: true });
  mkdirSync(dirname(statusPath), { recursive: true });
  const receipts = new ReceiptLog();
  let positions = readPositions(statusPath);
  let trades: LiveTrade[] = [];
  let view: { port: number; close(): Promise<void> } | null = null;
  if (opts.serve !== false && env.LIVE_VIEW !== "0") {
    try {
      view = await startLiveView({ port: opts.viewPort ?? Number(env.LIVE_VIEW_PORT || 8787), statusPath, eventsPath });
    } catch {
      view = null;
    }
  }
  let cycle = 0;
  let realTrades = false;
  const shouldStop = opts.shouldStop || (() => false);
  while (!shouldStop()) {
    cycle += 1;
    const nowMs = opts.nowMs ?? Date.now();
    const gate = liveGate(env);
    const discovered: ScanResult = await scan({ env, enrichLimit: Number(env.SCAN_ENRICH_LIMIT || 4), nowMs });
    let balance = discovered.wallet.balanceLamports;
    try {
      balance = await rpc.getBalance(PUBLIC_WALLET);
    } catch {
      balance = discovered.wallet.balanceLamports;
    }
    const exposure = positions.reduce((sum, row) => sum + (row.status === "open" ? row.size_sol : 0), 0);
    const qualified = await qualify(db, {
      candidates: discovered.candidates,
      marks: discovered.marks,
      qualify_only: true,
      wallet: { pubkey: PUBLIC_WALLET, balanceLamports: balance },
      open_exposure_sol: exposure,
      open_mints: positions.map((row) => row.mint),
      grok: { available: false, reason: "GROK_API_KEY_REQUIRED" },
      scanner: discovered.scanner,
    });
    let scope = scopeFromCandidates(discovered.candidates, qualified);
    const retryMints = scope
      .filter((row) => row.matched && !row.qualified && row.reasons.some((reason) => /unknown|holder|authority/.test(reason)))
      .slice(0, 6)
      .map((row) => row.mint);
    if (retryMints.length) {
      const enriched = await scan({
        env,
        watchMints: retryMints,
        enrichLimit: retryMints.length,
        nowMs,
      });
      discovered.candidates = enriched.candidates.length ? enriched.candidates : discovered.candidates;
      discovered.errors = [...discovered.errors, ...enriched.errors];
      const again = await qualify(db, {
        candidates: discovered.candidates,
        marks: enriched.marks,
        qualify_only: true,
        wallet: { pubkey: PUBLIC_WALLET, balanceLamports: balance },
        open_exposure_sol: exposure,
        open_mints: positions.map((row) => row.mint),
        grok: { available: false, reason: "GROK_API_KEY_REQUIRED" },
        scanner: enriched.scanner,
      });
      qualified.qualified_live = again.qualified_live;
      qualified.scope = again.scope;
      scope = scopeFromCandidates(discovered.candidates, qualified);
    }
    const events: Record<string, unknown>[] = [{ type: "scope", cycle, count: scope.length, qualified: scope.filter((row) => row.qualified).length }];
    const fast = (qualified.qualified_live || []).filter((row) => row.qualified && row.execution_class === "FAST" && row.mint);
    if (!gate.open) {
      events.push({ type: "send_refused", cycle, reasons: gate.reasons, sent: false, signed: false });
    } else {
      const next = fast[0];
      if (!next?.mint) {
        events.push({ type: "no_fast_candidate", cycle, sent: false });
      } else {
        const token = discovered.candidates.find((item) => item.mint === next.mint);
        const price = typeof next.price_sol === "number"
          ? next.price_sol
          : typeof token?.features.price_sol === "number"
            ? token.features.price_sol
            : Number.NaN;
        const resolved: Resolved = await resolve({
          mint: next.mint,
          program: String(next.program || token?.program || "pump"),
          priceSol: price,
          sizeSol: Math.min(typeof next.size_sol === "number" ? next.size_sol : 0.005, 0.005),
          slippageBps: Math.min(typeof next.slippage_bps === "number" ? next.slippage_bps : 100, 300),
          user: PUBLIC_WALLET,
          getAccountInfo: (pubkey) => rpc.getAccountInfo(pubkey),
        });
        if (!resolved.ok) {
          events.push({ type: "missing_accounts", cycle, mint: next.mint, missing: resolved.missing, sent: false, signed: false });
        } else {
          const outcome = await executeQualified({
            env,
            rpc,
            receipts,
            nowMs,
            balance,
            exposure,
            token,
            qualified: next,
            trade: resolved.trade,
          });
          events.push(outcome.event);
          trades = [outcome.trade, ...trades].slice(0, 50);
          if (outcome.position) positions = [outcome.position, ...positions.filter((row) => row.mint !== outcome.position?.mint)];
          if (outcome.trade.sent) realTrades = true;
        }
      }
    }
    for (const event of events) {
      appendFileSync(eventsPath, `${publicText(event)}\n`);
    }
    const status: LiveStatus = {
      cycle,
      mode: gate.open ? "live" : "paper",
      live_send_enabled: gate.open,
      real_trades: realTrades,
      private_key_exposed: false,
      reads_keypair: gate.open && Boolean(env.SOLANA_KEYPAIR_PATH),
      bot_wallet: PUBLIC_WALLET,
      balance_lamports: balance,
      open_exposure_sol: positions.reduce((sum, row) => sum + (row.status === "open" ? row.size_sol : 0), 0),
      signal: fast.length ? "FAST_CANDIDATE" : "LIVE SCANNER ACTIVE — NO QUALIFIED SIGNAL YET",
      scope,
      positions,
      trades,
      errors: discovered.errors,
      view: view ? `http://127.0.0.1:${view.port}/` : null,
      updated_at: new Date(nowMs).toISOString(),
      shutting_down: false,
    };
    writeFileSync(statusPath, `${publicText(status)}\n`);
    const sentNow = events.some((event) => event.sent === true);
    process.stdout.write(`${publicText({ cycle, mode: status.mode, sent: sentNow, live_send_enabled: gate.open, signal: status.signal, real_trades: realTrades })}\n`);
    if (maxCycles > 0 && cycle >= maxCycles) break;
    const waitUntil = Date.now() + intervalMs;
    while (Date.now() < waitUntil) await sleep(Math.min(250, waitUntil - Date.now()));
  }
  if (view) await view.close();
  return { cycles: cycle, real_trades: realTrades, private_key_exposed: false, statusPath, eventsPath };
}

async function executeQualified(input: {
  env: Record<string, string | undefined>;
  rpc: LiveRpc;
  receipts: ReceiptLog;
  nowMs: number;
  balance: number | null;
  exposure: number;
  token: NormalizedToken | undefined;
  qualified: Qualified;
  trade: TradeRequest;
}): Promise<{ event: Record<string, unknown>; trade: LiveTrade; position: LivePosition | null }> {
  const observedAt = input.token?.observed_at || new Date(input.nowMs).toISOString().replace(/\.\d{3}Z$/, "Z");
  const candidate: Record<string, unknown> = {
    ...(input.token || {}),
    candidate_id: input.qualified.mint,
    mint: input.qualified.mint,
    program: input.qualified.program || input.token?.program || "pump",
    observed_at: observedAt,
    proposed_size_sol: Math.min(typeof input.qualified.size_sol === "number" ? input.qualified.size_sol : 0.005, 0.005),
    slippage_bps: Math.min(typeof input.qualified.slippage_bps === "number" ? input.qualified.slippage_bps : 100, 300),
    features: input.token?.features || {},
  };
  let blockhash: Uint8Array;
  let fetchedAt = input.nowMs;
  try {
    const latest = await input.rpc.getLatestBlockhash();
    blockhash = latest.blockhash;
    fetchedAt = latest.fetchedAtMs;
  } catch {
    const trade: LiveTrade = {
      at: new Date(input.nowMs).toISOString(),
      mint: String(input.qualified.mint),
      side: "buy",
      sent: false,
      signed: false,
      signature: null,
      error: "blockhash_unavailable",
    };
    return { event: { type: "blockhash_unavailable", ...trade }, trade, position: null };
  }
  const walletSol = input.balance === null ? null : input.balance / 1_000_000_000;
  const result = await runControlled({
    env: input.env,
    candidate,
    trade: input.trade,
    rpc: input.rpc,
    jito: refusingJito,
    receipts: input.receipts,
    receiptId: `live:${input.qualified.mint}:${input.nowMs}`,
    nowMs: input.nowMs,
    openExposureSol: input.exposure,
    walletSol,
    blockhash,
    blockhashFetchedAtMs: fetchedAt,
    keypairPath: input.env.SOLANA_KEYPAIR_PATH,
  });
  const sent = result.sent === true;
  const signed = result.signed === true;
  const signature = typeof result.signature === "string" ? result.signature : null;
  const error = typeof result.error === "string" ? result.error : null;
  const trade: LiveTrade = {
    at: new Date(input.nowMs).toISOString(),
    mint: String(input.qualified.mint),
    side: "buy",
    sent,
    signed,
    signature,
    error,
  };
  const position: LivePosition | null = sent ? {
    mint: trade.mint,
    venue: input.trade.venue,
    size_sol: Number(candidate.proposed_size_sol),
    signature,
    status: "open",
    opened_at: trade.at,
  } : null;
  return {
    event: { type: sent ? "sent" : "not_sent", mint: trade.mint, sent, signed, signature, error, real_trades: sent },
    trade,
    position,
  };
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) {
  const stop = { requested: false };
  process.on("SIGINT", () => {
    stop.requested = true;
  });
  process.on("SIGTERM", () => {
    stop.requested = true;
  });
  runLiveBot({
    maxCycles: Number(process.env.BOT_MAX_CYCLES || 0),
    shouldStop: () => stop.requested,
    sleep: async (ms: number) => {
      const end = Date.now() + ms;
      while (!stop.requested && Date.now() < end) {
        await new Promise((resolve) => setTimeout(resolve, Math.min(250, end - Date.now())));
      }
    },
  })
    .then(() => process.exit(0))
    .catch((error) => {
      process.stderr.write(`${error instanceof Error ? error.message : "live_bot_failed"}\n`);
      process.exit(1);
    });
}
