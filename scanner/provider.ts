/**
 * HTTP and JSON-RPC helper with timeout, retry, and endpoint fallback.
 * Callers inject fetch so unit tests do not need the network.
 * Pump.fun currently rejects Node's fetch client; a curl GET is the fallback.
 */

import { spawn } from "node:child_process";

export type FetchImpl = (url: string, init?: RequestInit) => Promise<Response>;

export type ProviderOptions = {
  fetchImpl?: FetchImpl;
  timeoutMs?: number;
  attempts?: number;
  sleep?: (ms: number) => Promise<void>;
};

const DEFAULT_RPC = ["https://api.mainnet-beta.solana.com"];

export function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function withBackoff<T>(
  fn: () => Promise<T>,
  opts: { attempts: number; baseMs: number; sleep?: (ms: number) => Promise<void> },
): Promise<T> {
  const sleep = opts.sleep || defaultSleep;
  let last: unknown;
  for (let attempt = 1; attempt <= opts.attempts; attempt += 1) {
    try {
      return await fn();
    } catch (error) {
      last = error;
      if (attempt === opts.attempts) break;
      await sleep(opts.baseMs * attempt);
    }
  }
  throw last instanceof Error ? last : new Error("request_failed");
}

export class HttpProvider {
  fetchImpl: FetchImpl;
  timeoutMs: number;
  attempts: number;
  sleep: (ms: number) => Promise<void>;

  constructor(opts: ProviderOptions = {}) {
    this.fetchImpl = opts.fetchImpl || fetch;
    this.timeoutMs = opts.timeoutMs ?? 12_000;
    this.attempts = opts.attempts ?? 3;
    this.sleep = opts.sleep || defaultSleep;
  }

  async getJson(url: string): Promise<{ ok: true; status: number; data: unknown } | { ok: false; error: string; status?: number }> {
    try {
      const response = await withBackoff(
        () => this.fetchImpl(url, { method: "GET", signal: AbortSignal.timeout(this.timeoutMs) }),
        { attempts: this.attempts, baseMs: 200, sleep: this.sleep },
      );
      if (response.ok) {
        const data = await response.json();
        return { ok: true, status: response.status, data };
      }
      if (response.status === 403 && this.fetchImpl === fetch) {
        const curled = await curlGetJson(url, this.timeoutMs);
        if (curled.ok) return curled;
      }
      return { ok: false, error: `http_${response.status}`, status: response.status };
    } catch {
      if (this.fetchImpl === fetch) {
        const curled = await curlGetJson(url, this.timeoutMs);
        if (curled.ok) return curled;
      }
      return { ok: false, error: "request_failed" };
    }
  }

  async postJson(url: string, body: unknown): Promise<{ ok: true; data: unknown } | { ok: false; error: string }> {
    try {
      const response = await withBackoff(
        () =>
          this.fetchImpl(url, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(this.timeoutMs),
          }),
        { attempts: this.attempts, baseMs: 200, sleep: this.sleep },
      );
      if (!response.ok) return { ok: false, error: `http_${response.status}` };
      return { ok: true, data: await response.json() };
    } catch {
      return { ok: false, error: "request_failed" };
    }
  }

  async rpc(method: string, params: unknown[], urls: string[] = DEFAULT_RPC): Promise<{ ok: true; result: unknown } | { ok: false; error: string }> {
    const endpoints = urls.length ? urls : DEFAULT_RPC;
    let last = "rpc_failed";
    for (const url of endpoints) {
      const response = await this.postJson(url, { jsonrpc: "2.0", id: 1, method, params });
      if (!response.ok) {
        last = response.error;
        continue;
      }
      const data = response.data as { result?: unknown; error?: { message?: string } };
      if (data && data.error) {
        last = "rpc_error";
        continue;
      }
      if (!data || !("result" in data)) {
        last = "rpc_error";
        continue;
      }
      return { ok: true, result: data.result };
    }
    return { ok: false, error: last };
  }
}

export function curlGetJson(
  url: string,
  timeoutMs: number,
): Promise<{ ok: true; status: number; data: unknown } | { ok: false; error: string; status?: number }> {
  return new Promise((resolve) => {
    const child = spawn(
      "curl",
      ["-sS", "-m", String(Math.max(1, Math.ceil(timeoutMs / 1000))), "-w", "\n%{http_code}", "-H", "accept: application/json", url],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let out = "";
    child.stdout.on("data", (chunk) => {
      out += String(chunk);
    });
    child.on("error", () => resolve({ ok: false, error: "curl_failed" }));
    child.on("close", (code) => {
      if (code !== 0) {
        resolve({ ok: false, error: "curl_failed" });
        return;
      }
      const split = out.lastIndexOf("\n");
      const body = split >= 0 ? out.slice(0, split) : out;
      const status = Number(split >= 0 ? out.slice(split + 1).trim() : "0");
      if (status < 200 || status >= 300) {
        resolve({ ok: false, error: `http_${status || "curl"}`, status });
        return;
      }
      try {
        resolve({ ok: true, status, data: JSON.parse(body) });
      } catch {
        resolve({ ok: false, error: "unexpected_payload", status });
      }
    });
  });
}

export function rpcUrls(env: Record<string, string | undefined>): string[] {
  const urls = [];
  const primary = (env.SOLANA_RPC_URL || "").trim();
  if (primary) urls.push(primary);
  if (!urls.includes(DEFAULT_RPC[0])) urls.push(DEFAULT_RPC[0]);
  return urls;
}
