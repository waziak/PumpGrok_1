/** Pump.fun public coin list and an optional PumpPortal websocket with reconnect. */

import { HttpProvider } from "./provider.ts";
import { normalizePumpCoin, type NormalizedToken } from "./token-state.ts";

export const PUMP_COINS = "https://frontend-api-v3.pump.fun/coins";
export const PUMP_PORTAL = "wss://pumpportal.fun/api/data";

export async function discoverPumpCoins(
  provider: HttpProvider,
  opts: { limit?: number; sort?: string; nowMs?: number } = {},
): Promise<{ ok: boolean; tokens: NormalizedToken[]; error?: string }> {
  const limit = opts.limit ?? 30;
  const sort = opts.sort ?? "created_timestamp";
  const url = `${PUMP_COINS}?offset=0&limit=${limit}&sort=${encodeURIComponent(sort)}&order=DESC&includeNsfw=false`;
  const response = await provider.getJson(url);
  if (!response.ok) return { ok: false, tokens: [], error: response.error };
  if (!Array.isArray(response.data)) return { ok: false, tokens: [], error: "unexpected_payload" };
  const nowMs = opts.nowMs ?? Date.now();
  const tokens: NormalizedToken[] = [];
  for (const row of response.data) {
    const token = normalizePumpCoin(row, nowMs);
    if (token) tokens.push(token);
  }
  return { ok: true, tokens };
}

export type SocketLike = {
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  onclose: (() => void) | null;
};

export function connectPumpPortal(opts: {
  url?: string;
  WebSocketImpl: new (url: string) => SocketLike;
  onToken: (message: unknown) => void;
  sleep?: (ms: number) => Promise<void>;
  maxAttempts?: number;
}): { stop: () => void; attempts: () => number } {
  let stopped = false;
  let attempts = 0;
  let reconnecting = false;
  let socket: SocketLike | null = null;
  const sleep = opts.sleep || ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  const maxAttempts = opts.maxAttempts ?? 5;

  const open = async (): Promise<void> => {
    if (stopped || attempts >= maxAttempts) return;
    attempts += 1;
    const current = new opts.WebSocketImpl(opts.url || PUMP_PORTAL);
    socket = current;
    current.onopen = () => {
      try {
        current.send(JSON.stringify({ method: "subscribeNewToken" }));
      } catch {
        // The REST poll remains the source of discovery.
      }
    };
    current.onmessage = (event) => {
      let parsed: unknown = event.data;
      if (typeof event.data === "string") {
        try {
          parsed = JSON.parse(event.data);
        } catch {
          return;
        }
      }
      opts.onToken(parsed);
    };
    current.onerror = () => {
      try {
        current.close();
      } catch {
        // already closed
      }
    };
    current.onclose = () => {
      if (stopped || reconnecting) return;
      reconnecting = true;
      const delay = 200 * attempts;
      void sleep(delay).then(() => {
        reconnecting = false;
        void open();
      });
    };
  };
  void open();
  return {
    stop() {
      stopped = true;
      try {
        socket?.close();
      } catch {
        // ignore
      }
    },
    attempts: () => attempts,
  };
}
