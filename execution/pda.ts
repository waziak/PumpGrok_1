/** Program-derived addresses. Off-curve check uses ed25519, not a secret key. */

import { createHash } from "node:crypto";

import { ed25519 } from "@noble/curves/ed25519";

import { b58encode, concatBytes, pubkeyBytes } from "./codec.ts";

function onCurve(bytes: Uint8Array): boolean {
  try {
    ed25519.ExtendedPoint.fromHex(bytes);
    return true;
  } catch {
    return false;
  }
}

export function findProgramAddress(seeds: Uint8Array[], program: Uint8Array): { bytes: Uint8Array; bump: number } {
  for (let bump = 255; bump >= 0; bump -= 1) {
    const hash = createHash("sha256")
      .update(concatBytes([...seeds, new Uint8Array([bump]), program, Buffer.from("ProgramDerivedAddress")]))
      .digest();
    if (!onCurve(hash)) return { bytes: hash, bump };
  }
  throw new Error("pda_not_found");
}

export function pda(seeds: Uint8Array[], program: string): string {
  return b58encode(findProgramAddress(seeds, pubkeyBytes(program)).bytes);
}

export function associatedTokenAddress(owner: string, mint: string, tokenProgram: string): string {
  return pda(
    [pubkeyBytes(owner), pubkeyBytes(tokenProgram), pubkeyBytes(mint)],
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
  );
}
