/** Mint authorities and largest accounts from Solana JSON-RPC. Failures stay UNKNOWN. */

import { HttpProvider } from "./provider.ts";

export type HolderRead = {
  info: { mintAuthority: unknown; freezeAuthority: unknown; supply: unknown; decimals: unknown } | null;
  largest: { amount: string }[] | null;
};

export async function readHolders(provider: HttpProvider, mint: string, rpcUrls: string[]): Promise<HolderRead> {
  const account = await provider.rpc("getAccountInfo", [mint, { encoding: "jsonParsed" }], rpcUrls);
  let info: HolderRead["info"] = null;
  if (account.ok) {
    const result = account.result as {
      value?: { data?: { parsed?: { info?: Record<string, unknown> } } };
    } | null;
    const parsed = result?.value?.data?.parsed?.info;
    if (parsed && ("mintAuthority" in parsed || "supply" in parsed)) {
      info = {
        mintAuthority: parsed.mintAuthority ?? null,
        freezeAuthority: parsed.freezeAuthority ?? null,
        supply: parsed.supply ?? null,
        decimals: parsed.decimals ?? null,
      };
    }
  }
  const largestCall = await provider.rpc("getTokenLargestAccounts", [mint], rpcUrls);
  let largest: { amount: string }[] | null = null;
  if (largestCall.ok) {
    const result = largestCall.result as { value?: { amount?: string }[] } | null;
    if (result && Array.isArray(result.value)) {
      largest = result.value
        .filter((row) => typeof row.amount === "string")
        .map((row) => ({ amount: row.amount as string }));
    }
  }
  return { info, largest };
}

export async function readBalance(provider: HttpProvider, owner: string, rpcUrls: string[]): Promise<number | null> {
  const response = await provider.rpc("getBalance", [owner], rpcUrls);
  if (!response.ok) return null;
  const result = response.result as { value?: unknown } | number | null;
  if (typeof result === "number") return result;
  if (result && typeof result === "object" && typeof result.value === "number") return result.value;
  return null;
}
