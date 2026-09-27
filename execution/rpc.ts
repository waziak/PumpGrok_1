/**
 * Solana JSON-RPC transport for the live bot.
 * simulateTransaction and sendRawTransaction are separate calls.
 * send uses maxRetries 0. Confirmation polling does not submit again.
 */

import { b58decode } from "./codec.ts";
import type { RpcTransport } from "./live.ts";

export type AccountInfo = { data: Uint8Array; owner: string };

export type LiveRpc = RpcTransport & {
  simulateWire(messageB64: string): Promise<{ ok: boolean; error?: string }>;
  getLatestBlockhash(): Promise<{ blockhash: Uint8Array; fetchedAtMs: number }>;
  getAccountInfo(pubkey: string): Promise<AccountInfo | null>;
};

type FetchImpl = (url: string, init?: RequestInit) => Promise<Response>;

export function createSolanaRpc(
  rpcUrl: string,
  opts: { fetchImpl?: FetchImpl; sleep?: (ms: number) => Promise<void>; confirmMs?: number } = {},
): LiveRpc {
  const fetchImpl = opts.fetchImpl || fetch;
  const sleep = opts.sleep || ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const confirmMs = opts.confirmMs ?? 20_000;

  async function call(method: string, params: unknown[]): Promise<unknown> {
    const response = await fetchImpl(rpcUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    const body = await response.json() as { result?: unknown; error?: { message?: string } };
    if (!response.ok || body.error) {
      throw new Error(body.error?.message || `rpc_${response.status}`);
    }
    return body.result;
  }

  return {
    async getBalance(owner: string) {
      const result = await call("getBalance", [owner]) as { value?: number };
      if (typeof result?.value !== "number") throw new Error("balance_missing");
      return result.value;
    },
    async getAccountInfo(pubkey: string) {
      const result = await call("getAccountInfo", [pubkey, { encoding: "base64" }]) as {
        value?: { owner?: string; data?: [string, string] } | null;
      };
      const value = result?.value;
      if (!value || !value.data || typeof value.owner !== "string") return null;
      return { owner: value.owner, data: Buffer.from(value.data[0], "base64") };
    },
    async getLatestBlockhash() {
      const fetchedAtMs = Date.now();
      const result = await call("getLatestBlockhash", [{ commitment: "confirmed" }]) as {
        value?: { blockhash?: string };
      };
      const blockhash = result?.value?.blockhash;
      if (typeof blockhash !== "string") throw new Error("blockhash_missing");
      const bytes = b58decode(blockhash);
      if (bytes.length !== 32) throw new Error("blockhash");
      return { blockhash: bytes, fetchedAtMs };
    },
    async simulate() {
      throw new Error("use_simulate_wire");
    },
    async simulateWire(messageB64: string) {
      const message = Buffer.from(messageB64, "base64");
      if (message.length < 3) return { ok: false, error: "empty_message" };
      const signatures = message[0];
      const tx = Buffer.concat([Buffer.from([signatures]), Buffer.alloc(64 * signatures), message]);
      const result = await call("simulateTransaction", [
        tx.toString("base64"),
        { encoding: "base64", sigVerify: false, replaceRecentBlockhash: false, commitment: "processed" },
      ]) as { value?: { err?: unknown } };
      if (result?.value?.err) return { ok: false, error: JSON.stringify(result.value.err) };
      return { ok: true };
    },
    async send(signed: string) {
      const signature = await call("sendRawTransaction", [
        signed,
        { encoding: "base64", skipPreflight: false, maxRetries: 0 },
      ]);
      if (typeof signature !== "string" || signature.length < 32) throw new Error("send_missing_signature");
      return { signature };
    },
    async confirm(signature: string) {
      const deadline = Date.now() + confirmMs;
      while (Date.now() <= deadline) {
        const result = await call("getSignatureStatuses", [[signature], { searchTransactionHistory: true }]) as {
          value?: Array<{ err?: unknown; confirmationStatus?: string } | null>;
        };
        const row = result?.value?.[0];
        if (row?.err) return "failed";
        if (row?.confirmationStatus === "confirmed" || row?.confirmationStatus === "finalized") return "confirmed";
        if (Date.now() + 500 > deadline) break;
        await sleep(500);
      }
      return null;
    },
  };
}
