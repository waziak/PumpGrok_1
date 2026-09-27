/**
 * Jito bundle client.
 *
 * Docs: POST https://mainnet.block-engine.jito.wtf/api/v1/bundles
 * method sendBundle, base58 or base64 signed txs, max 5.
 * A tip is a SOL transfer to a tip account from getTipAccounts.
 * Minimum documented tip is 1_000 lamports. A bundle id is not a landing.
 * getInflightBundleStatuses reports Invalid, Pending, Landed, or Failed.
 *
 * This module never retries and the default transport does not call the network.
 */

export type JitoStatus = "submitted" | "failed" | "uncertain" | "refused";

export type JitoTransport = {
  sendBundle: (body: unknown) => Promise<{ bundleId?: string; error?: string }>;
  bundleStatus?: (bundleId: string) => Promise<"Invalid" | "Pending" | "Landed" | "Failed" | "unknown" | null>;
};

export const refusingJito: JitoTransport = {
  async sendBundle() {
    throw new Error("jito_disabled");
  },
};

export async function submitBundle(
  transport: JitoTransport,
  signedTxs: string[],
): Promise<{ ok: boolean; status: JitoStatus; bundleId: string | null; retry: false; error?: string }> {
  if (signedTxs.length < 1 || signedTxs.length > 5) {
    return { ok: false, status: "refused", bundleId: null, retry: false, error: "bundle_size" };
  }
  let response: { bundleId?: string; error?: string };
  try {
    response = await transport.sendBundle({
      jsonrpc: "2.0",
      id: 1,
      method: "sendBundle",
      params: [signedTxs, { encoding: "base64" }],
    });
  } catch {
    return { ok: false, status: "failed", bundleId: null, retry: false, error: "jito_failure" };
  }
  if (!response.bundleId) {
    return { ok: false, status: "failed", bundleId: null, retry: false, error: response.error || "jito_failure" };
  }
  if (!transport.bundleStatus) {
    return { ok: false, status: "uncertain", bundleId: response.bundleId, retry: false, error: "uncertain_tx" };
  }
  try {
    const status = await transport.bundleStatus(response.bundleId);
    if (status === "Landed") {
      return { ok: true, status: "submitted", bundleId: response.bundleId, retry: false };
    }
    if (status === "Failed" || status === "Invalid") {
      return { ok: false, status: "failed", bundleId: response.bundleId, retry: false, error: "jito_failure" };
    }
    return { ok: false, status: "uncertain", bundleId: response.bundleId, retry: false, error: "uncertain_tx" };
  } catch {
    return { ok: false, status: "uncertain", bundleId: response.bundleId, retry: false, error: "uncertain_tx" };
  }
}
