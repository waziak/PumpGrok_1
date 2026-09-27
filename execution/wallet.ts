/**
 * Local keypair file loader. Agents never call this.
 * Secret bytes stay in a closure and are not returned, logged, or written.
 */

import { createPrivateKey, sign as cryptoSign } from "node:crypto";
import { readFileSync, statSync } from "node:fs";

import { signGate } from "./gate.ts";

const PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export type WalletError = {
  ok: false;
  error: "inaccessible_keypair" | "malformed_keypair" | "sign_refused";
  signed: false;
  secretMaterialExposed: false;
};

export type SignResult =
  | { ok: true; signatureB64: string; signed: true; secretMaterialExposed: false }
  | WalletError;

function refuse(error: WalletError["error"]): WalletError {
  return { ok: false, error, signed: false, secretMaterialExposed: false };
}

export function loadPublicPreview(keypairPath: string | undefined): { ok: true; bytes: number } | WalletError {
  if (!keypairPath) return refuse("inaccessible_keypair");
  let raw: string;
  try {
    const info = statSync(keypairPath);
    if (!info.isFile()) return refuse("inaccessible_keypair");
    raw = readFileSync(keypairPath, "utf8");
  } catch {
    return refuse("inaccessible_keypair");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return refuse("malformed_keypair");
  }
  if (!Array.isArray(parsed) || parsed.length !== 64) return refuse("malformed_keypair");
  if (!parsed.every((item) => Number.isInteger(item) && item >= 0 && item <= 255)) {
    return refuse("malformed_keypair");
  }
  return { ok: true, bytes: 64 };
}

export type SignedTransaction =
  | { ok: true; transactionB64: string; signed: true; secretMaterialExposed: false }
  | WalletError;

export function signMessage(
  keypairPath: string | undefined,
  message: Uint8Array,
  env: Record<string, string | undefined>,
): SignResult {
  const gate = signGate(env);
  if (!gate.open) return refuse("sign_refused");
  const preview = loadPublicPreview(keypairPath);
  if (!preview.ok) return preview;
  let parsed: number[];
  try {
    parsed = JSON.parse(readFileSync(keypairPath as string, "utf8")) as number[];
  } catch {
    return refuse("inaccessible_keypair");
  }
  const secret = Buffer.from(parsed);
  const seed = secret.subarray(0, 32);
  try {
    const key = createPrivateKey({
      key: Buffer.concat([PKCS8_PREFIX, seed]),
      format: "der",
      type: "pkcs8",
    });
    const signature = cryptoSign(null, Buffer.from(message), key);
    return {
      ok: true,
      signatureB64: signature.toString("base64"),
      signed: true,
      secretMaterialExposed: false,
    };
  } catch {
    return refuse("malformed_keypair");
  } finally {
    secret.fill(0);
    seed.fill(0);
  }
}

export function signTransaction(
  keypairPath: string | undefined,
  message: Uint8Array,
  env: Record<string, string | undefined>,
): SignedTransaction {
  const signed = signMessage(keypairPath, message, env);
  if (!signed.ok) return signed;
  const signature = Buffer.from(signed.signatureB64, "base64");
  if (signature.length !== 64) return refuse("malformed_keypair");
  const tx = Buffer.concat([Buffer.from([1]), signature, Buffer.from(message)]);
  return {
    ok: true,
    transactionB64: tx.toString("base64"),
    signed: true,
    secretMaterialExposed: false,
  };
}
