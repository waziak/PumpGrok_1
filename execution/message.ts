/** Legacy Solana message compiler. It does not broadcast. */

import { concatBytes, pubkeyBytes } from "./codec.ts";
import type { AccountMeta, WireInstruction } from "./venues.ts";

export type Compiled = {
  message: Uint8Array;
  accountKeys: string[];
  signaturesRequired: number;
};

function compact(value: number): Uint8Array {
  if (value < 0x80) return new Uint8Array([value]);
  if (value < 0x4000) return new Uint8Array([(value & 0x7f) | 0x80, value >> 7]);
  throw new Error("compact_too_large");
}

export function compileLegacy(instructions: WireInstruction[], feePayer: string, blockhash: Uint8Array): Compiled {
  if (blockhash.length !== 32) throw new Error("blockhash");
  const metas: AccountMeta[] = [{ name: "fee_payer", pubkey: feePayer, writable: true, signer: true }];
  for (const ix of instructions) {
    for (const account of ix.accounts) metas.push(account);
    metas.push({ name: "program", pubkey: ix.programId, writable: false, signer: false });
  }
  const order: string[] = [];
  const flags = new Map<string, { writable: boolean; signer: boolean }>();
  for (const account of metas) {
    const current = flags.get(account.pubkey);
    if (!current) {
      order.push(account.pubkey);
      flags.set(account.pubkey, { writable: account.writable, signer: account.signer });
    } else {
      current.writable = current.writable || account.writable;
      current.signer = current.signer || account.signer;
    }
  }
  const signers = order.filter((key) => flags.get(key)?.signer);
  const writableSigners = signers.filter((key) => flags.get(key)?.writable);
  const readonlySigners = signers.filter((key) => !flags.get(key)?.writable);
  const writable = order.filter((key) => !flags.get(key)?.signer && flags.get(key)?.writable);
  const readonly = order.filter((key) => !flags.get(key)?.signer && !flags.get(key)?.writable);
  const keys = [...writableSigners, ...readonlySigners, ...writable, ...readonly];
  const index = new Map(keys.map((key, position) => [key, position]));
  const keyBytes = concatBytes(keys.map((key) => pubkeyBytes(key)));
  const compiled = instructions.map((ix) => {
    const programIndex = index.get(ix.programId);
    if (programIndex === undefined) throw new Error("missing_program");
    const indexes = ix.accounts.map((account) => {
      const position = index.get(account.pubkey);
      if (position === undefined) throw new Error("missing_account");
      return position;
    });
    return concatBytes([
      new Uint8Array([programIndex]),
      compact(indexes.length),
      new Uint8Array(indexes),
      compact(ix.data.length),
      ix.data,
    ]);
  });
  const message = concatBytes([
    new Uint8Array([signers.length, readonlySigners.length, readonly.length]),
    compact(keys.length),
    keyBytes,
    blockhash,
    compact(instructions.length),
    ...compiled,
  ]);
  return { message, accountKeys: keys, signaturesRequired: signers.length };
}
