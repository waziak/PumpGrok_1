/**
 * Paper bot. Discovers tokens, asks Python to score and paper-trade, then sleeps.
 * This process never reads a keypair and never broadcasts.
 */

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { qualitativeNotes } from "./grok.ts";
import { scanOnce } from "../scanner/scanner-loop.ts";
import { PUBLIC_WALLET } from "../scanner/token-state.ts";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

export const PAPER_BOT_READS_KEYPAIR = false;

type CycleResult = {
  ok?: boolean;
  open_mints?: string[];
  qualified_entries?: number;
  stored?: number;
  paper_trades?: number;
  paper_exits?: number;
  grok_reason?: string | null;
  entries?: unknown[];
  exits?: unknown[];
};

function runPython(db: string, payload: unknown): Promise<CycleResult> {
  return new Promise((resolve) => {
    const child = spawn("python3", ["-m", "layer", "--db", db, "cycle"], {
      cwd: ROOT,
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk);
    });
    child.on("close", () => {
      try {
        resolve(JSON.parse(stdout) as CycleResult);
      } catch {
        resolve({ ok: false, entries: [{ error: "cycle_output", detail: stderr.slice(0, 400) }] });
      }
    });
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  });
}

export async function runBot(opts: {
  env?: Record<string, string | undefined>;
  maxCycles?: number;
  intervalMs?: number;
  db?: string;
  sleep?: (ms: number) => Promise<void>;
  scan?: typeof scanOnce;
} = {}): Promise<Record<string, unknown>> {
  const env = opts.env || process.env;
  const maxCycles = opts.maxCycles ?? Number(env.BOT_MAX_CYCLES || 0);
  const intervalMs = opts.intervalMs ?? Number(env.BOT_INTERVAL_MS || 20_000);
  const db = opts.db || join(ROOT, "data", "pumpgrok-research.sqlite");
  const sleep = opts.sleep || ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const scan = opts.scan || scanOnce;
  mkdirSync(dirname(db), { recursive: true });
  let stop = false;
  const requestStop = () => {
    stop = true;
  };
  process.on("SIGINT", requestStop);
  process.on("SIGTERM", requestStop);
  let watch: string[] = [];
  let cycle = 0;
  let last: CycleResult = {};
  while (!stop) {
    cycle += 1;
    const started = Date.now();
    const discovered = await scan({
      env,
      watchMints: watch,
      enrichLimit: Number(env.SCAN_ENRICH_LIMIT || 4),
    });
    const qualitative = discovered.candidates.filter((token) => token.enriched).slice(0, 3);
    const grok = await qualitativeNotes(env, qualitative);
    last = await runPython(db, {
      candidates: discovered.candidates,
      marks: discovered.marks,
      grok,
      samples: discovered.samples,
      wallet: discovered.wallet,
      scanner: discovered.scanner,
    });
    watch = last.open_mints || [];
    const status = {
      cycle,
      mode: "paper",
      real_trades: false,
      private_key_exposed: false,
      live_send_disabled: true,
      reads_keypair: PAPER_BOT_READS_KEYPAIR,
      bot_wallet: PUBLIC_WALLET,
      wallet: discovered.wallet,
      tokens_observed: discovered.tokensObserved,
      qualified_entries: last.qualified_entries || 0,
      grok_reason: grok.reason || last.grok_reason || null,
      scanner: discovered.scanner,
      errors: discovered.errors,
      signal: (last.qualified_entries || 0) > 0 ? "QUALIFIED" : "LIVE SCANNER ACTIVE — NO QUALIFIED SIGNAL YET",
      duration_ms: Date.now() - started,
      shutting_down: false,
    };
    writeFileSync(join(ROOT, "data", "bot-status.json"), JSON.stringify(status, null, 2));
    process.stdout.write(`${JSON.stringify(status)}\n`);
    if (maxCycles > 0 && cycle >= maxCycles) break;
    const waitUntil = Date.now() + intervalMs;
    while (!stop && Date.now() < waitUntil) {
      await sleep(Math.min(250, waitUntil - Date.now()));
    }
  }
  const shutdown = { cycle, shutting_down: true, real_trades: false, live_send_disabled: true };
  writeFileSync(join(ROOT, "data", "bot-shutdown.json"), JSON.stringify(shutdown));
  return { cycles: cycle, last, shutting_down: true, real_trades: false };
}

const invoked = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (invoked) {
  runBot()
    .then(() => process.exit(0))
    .catch((error) => {
      process.stderr.write(`${error instanceof Error ? error.message : "bot_failed"}\n`);
      process.exit(1);
    });
}
