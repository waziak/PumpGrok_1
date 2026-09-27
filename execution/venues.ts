/**
 * Pump.fun and PumpSwap instruction builders.
 *
 * Account order follows the public IDL at
 * https://github.com/pump-fun/pump-public-docs (idl/pump.json, idl/pump_amm.json)
 * and docs/instructions/BUY.md. Fee recipient lists are the published sets in
 * docs/FEE_RECIPIENTS.md. A missing creator, mint, or token program stays
 * unwired instead of being invented.
 */

import { b58encode, concatBytes, pubkeyBytes, u64le } from "./codec.ts";
import { associatedTokenAddress, pda } from "./pda.ts";

export const PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P";
export const PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";
export const FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ";
export const TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA";
export const TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
export const ASSOCIATED_TOKEN = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL";
export const SYSTEM = "11111111111111111111111111111111";
export const WSOL = "So11111111111111111111111111111111111111112";

export const NORMAL_FEE_RECIPIENTS = [
  "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
  "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
  "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
  "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
  "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
  "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",
  "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
  "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
] as const;

export const MAYHEM_FEE_RECIPIENTS = [
  "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS",
  "4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6",
  "8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR",
  "4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH",
  "8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6",
  "Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk",
  "463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq",
  "6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA",
] as const;

export const BUYBACK_FEE_RECIPIENTS = [
  "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
  "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
  "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
  "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
  "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
  "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
  "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
  "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
] as const;

const BUY_EXACT_QUOTE_V2 = new Uint8Array([194, 171, 28, 70, 104, 77, 91, 47]);
const SELL_V2 = new Uint8Array([93, 246, 130, 60, 231, 233, 64, 178]);
const AMM_BUY_EXACT_QUOTE = new Uint8Array([198, 46, 21, 82, 180, 217, 232, 112]);
const AMM_SELL = new Uint8Array([51, 230, 133, 164, 1, 127, 131, 173]);

export type AccountMeta = {
  name: string;
  pubkey: string;
  writable: boolean;
  signer: boolean;
};

export type WireInstruction = {
  ok: true;
  wireReady: true;
  venue: "pump" | "pumpswap";
  instruction: string;
  programId: string;
  accounts: AccountMeta[];
  data: Uint8Array;
  missing: [];
};

export type WireFailure = {
  ok: false;
  wireReady: false;
  error: string;
  missing: string[];
  sent: false;
};

export type Wire = WireInstruction | WireFailure;

export type PumpTrade = {
  side: "buy" | "sell";
  user: string;
  baseMint: string;
  creator: string;
  baseTokenProgram: string;
  quoteMint?: string;
  quoteTokenProgram?: string;
  feeRecipient?: string;
  buybackFeeRecipient?: string;
  mayhem?: boolean;
  amount: bigint;
  limitLamports: bigint;
};

export type PumpSwapTrade = {
  side: "buy" | "sell";
  user: string;
  baseMint: string;
  quoteMint?: string;
  coinCreator: string;
  baseTokenProgram: string;
  quoteTokenProgram?: string;
  protocolFeeRecipient: string;
  buybackFeeRecipient?: string;
  pool?: string;
  cashback?: boolean;
  amount: bigint;
  limitLamports: bigint;
};

function meta(name: string, pubkey: string, writable: boolean, signer = false): AccountMeta {
  pubkeyBytes(pubkey);
  return { name, pubkey, writable, signer };
}

function fail(missing: string[]): WireFailure {
  return { ok: false, wireReady: false, error: "missing_accounts", missing, sent: false };
}

export function bondingCurvePda(mint: string): string {
  return pda([Buffer.from("bonding-curve"), pubkeyBytes(mint)], PUMP);
}

export function pumpPoolAuthority(mint: string): string {
  return pda([Buffer.from("pool-authority"), pubkeyBytes(mint)], PUMP);
}

export function poolV2Pda(mint: string): string {
  return pda([Buffer.from("pool-v2"), pubkeyBytes(mint)], PUMP_AMM);
}

export function canonicalPumpSwapPool(mint: string): string {
  const authority = pumpPoolAuthority(mint);
  return pda(
    [Buffer.from("pool"), new Uint8Array([0, 0]), pubkeyBytes(authority), pubkeyBytes(mint), pubkeyBytes(WSOL)],
    PUMP_AMM,
  );
}

function feeConfig(programId: string): string {
  return pda([Buffer.from("fee_config"), pubkeyBytes(programId)], FEE_PROGRAM);
}

function pumpCommon(input: PumpTrade): { accounts: AccountMeta[] } | WireFailure {
  const missing: string[] = [];
  for (const key of ["user", "baseMint", "creator", "baseTokenProgram"] as const) {
    if (!input[key]) missing.push(key);
  }
  if (input.amount <= 0n) missing.push("amount");
  if (input.limitLamports < 0n) missing.push("limitLamports");
  if (missing.length) return fail(missing);
  const quoteMint = input.quoteMint || WSOL;
  const quoteProgram = input.quoteTokenProgram || TOKEN_PROGRAM;
  const feeRecipient = input.feeRecipient || (input.mayhem ? MAYHEM_FEE_RECIPIENTS[0] : NORMAL_FEE_RECIPIENTS[0]);
  const buyback = input.buybackFeeRecipient || BUYBACK_FEE_RECIPIENTS[0];
  const curve = bondingCurvePda(input.baseMint);
  const creatorVault = pda([Buffer.from("creator-vault"), pubkeyBytes(input.creator)], PUMP);
  const userVolume = pda([Buffer.from("user_volume_accumulator"), pubkeyBytes(input.user)], PUMP);
  const accounts: AccountMeta[] = [
    meta("global", pda([Buffer.from("global")], PUMP), false),
    meta("base_mint", input.baseMint, false),
    meta("quote_mint", quoteMint, false),
    meta("base_token_program", input.baseTokenProgram, false),
    meta("quote_token_program", quoteProgram, false),
    meta("associated_token_program", ASSOCIATED_TOKEN, false),
    meta("fee_recipient", feeRecipient, true),
    meta("associated_quote_fee_recipient", associatedTokenAddress(feeRecipient, quoteMint, quoteProgram), true),
    meta("buyback_fee_recipient", buyback, true),
    meta("associated_quote_buyback_fee_recipient", associatedTokenAddress(buyback, quoteMint, quoteProgram), true),
    meta("bonding_curve", curve, true),
    meta("associated_base_bonding_curve", associatedTokenAddress(curve, input.baseMint, input.baseTokenProgram), true),
    meta("associated_quote_bonding_curve", associatedTokenAddress(curve, quoteMint, quoteProgram), true),
    meta("user", input.user, true, true),
    meta("associated_base_user", associatedTokenAddress(input.user, input.baseMint, input.baseTokenProgram), true),
    meta("associated_quote_user", associatedTokenAddress(input.user, quoteMint, quoteProgram), true),
    meta("creator_vault", creatorVault, true),
    meta("associated_creator_vault", associatedTokenAddress(creatorVault, quoteMint, quoteProgram), true),
    meta("sharing_config", pda([Buffer.from("sharing-config"), pubkeyBytes(input.baseMint)], FEE_PROGRAM), false),
  ];
  return { accounts };
}

export function buildPumpInstruction(input: PumpTrade): Wire {
  const common = pumpCommon(input);
  if ("wireReady" in common) return common;
  const quoteMint = input.quoteMint || WSOL;
  const quoteProgram = input.quoteTokenProgram || TOKEN_PROGRAM;
  const userVolume = pda([Buffer.from("user_volume_accumulator"), pubkeyBytes(input.user)], PUMP);
  const tailVolume: AccountMeta[] = input.side === "buy"
    ? [
        meta("global_volume_accumulator", pda([Buffer.from("global_volume_accumulator")], PUMP), false),
        meta("user_volume_accumulator", userVolume, true),
        meta("associated_user_volume_accumulator", associatedTokenAddress(userVolume, quoteMint, quoteProgram), true),
      ]
    : [
        meta("user_volume_accumulator", userVolume, true),
        meta("associated_user_volume_accumulator", associatedTokenAddress(userVolume, quoteMint, quoteProgram), true),
      ];
  const accounts = common.accounts.concat(tailVolume, [
    meta("fee_config", feeConfig(PUMP), false),
    meta("fee_program", FEE_PROGRAM, false),
    meta("system_program", SYSTEM, false),
    meta("event_authority", pda([Buffer.from("__event_authority")], PUMP), false),
    meta("program", PUMP, false),
  ]);
  const data = input.side === "buy"
    ? concatBytes([BUY_EXACT_QUOTE_V2, u64le(input.limitLamports), u64le(input.amount)])
    : concatBytes([SELL_V2, u64le(input.amount), u64le(input.limitLamports)]);
  return {
    ok: true,
    wireReady: true,
    venue: "pump",
    instruction: input.side === "buy" ? "buy_exact_quote_in_v2" : "sell_v2",
    programId: PUMP,
    accounts,
    data,
    missing: [],
  };
}

export function buildPumpSwapInstruction(input: PumpSwapTrade): Wire {
  const missing: string[] = [];
  for (const key of ["user", "baseMint", "coinCreator", "baseTokenProgram", "protocolFeeRecipient"] as const) {
    if (!input[key]) missing.push(key);
  }
  if (input.amount <= 0n) missing.push("amount");
  if (input.limitLamports < 0n) missing.push("limitLamports");
  if (missing.length) return fail(missing);
  const quoteMint = input.quoteMint || WSOL;
  const quoteProgram = input.quoteTokenProgram || TOKEN_PROGRAM;
  const pool = input.pool || canonicalPumpSwapPool(input.baseMint);
  const creatorVault = pda([Buffer.from("creator_vault"), pubkeyBytes(input.coinCreator)], PUMP_AMM);
  const accounts: AccountMeta[] = [
    meta("pool", pool, true),
    meta("user", input.user, true, true),
    meta("global_config", pda([Buffer.from("global_config")], PUMP_AMM), false),
    meta("base_mint", input.baseMint, false),
    meta("quote_mint", quoteMint, false),
    meta("user_base_token_account", associatedTokenAddress(input.user, input.baseMint, input.baseTokenProgram), true),
    meta("user_quote_token_account", associatedTokenAddress(input.user, quoteMint, quoteProgram), true),
    meta("pool_base_token_account", associatedTokenAddress(pool, input.baseMint, input.baseTokenProgram), true),
    meta("pool_quote_token_account", associatedTokenAddress(pool, quoteMint, quoteProgram), true),
    meta("protocol_fee_recipient", input.protocolFeeRecipient, false),
    meta("protocol_fee_recipient_token_account", associatedTokenAddress(input.protocolFeeRecipient, quoteMint, quoteProgram), true),
    meta("base_token_program", input.baseTokenProgram, false),
    meta("quote_token_program", quoteProgram, false),
    meta("system_program", SYSTEM, false),
    meta("associated_token_program", ASSOCIATED_TOKEN, false),
    meta("event_authority", pda([Buffer.from("__event_authority")], PUMP_AMM), false),
    meta("program", PUMP_AMM, false),
    meta("coin_creator_vault_ata", associatedTokenAddress(creatorVault, quoteMint, quoteProgram), true),
    meta("coin_creator_vault_authority", creatorVault, false),
  ];
  if (input.side === "buy") {
    accounts.push(
      meta("global_volume_accumulator", pda([Buffer.from("global_volume_accumulator")], PUMP_AMM), false),
      meta("user_volume_accumulator", pda([Buffer.from("user_volume_accumulator"), pubkeyBytes(input.user)], PUMP_AMM), true),
    );
  }
  accounts.push(
    meta("fee_config", feeConfig(PUMP_AMM), false),
    meta("fee_program", FEE_PROGRAM, false),
  );
  // The published IDL ends at fee_program. The April 2026 program upgrade
  // requires these remaining accounts on buy and sell (see pump-public-docs
  // BREAKING_FEE_RECIPIENT.md and @pump-fun/pump-swap-sdk poolV2Pda).
  if (input.cashback && input.side === "buy") {
    const userVolume = pda([Buffer.from("user_volume_accumulator"), pubkeyBytes(input.user)], PUMP_AMM);
    accounts.push(meta(
      "user_volume_accumulator_wsol_ata",
      associatedTokenAddress(userVolume, WSOL, quoteProgram),
      true,
    ));
  }
  if (input.cashback && input.side === "sell") {
    const userVolume = pda([Buffer.from("user_volume_accumulator"), pubkeyBytes(input.user)], PUMP_AMM);
    accounts.push(
      meta("user_volume_accumulator_quote_ata", associatedTokenAddress(userVolume, quoteMint, quoteProgram), true),
      meta("user_volume_accumulator_cashback", userVolume, true),
    );
  }
  if (!pubkeyBytes(input.coinCreator).every((byte) => byte === 0)) {
    accounts.push(meta("pool_v2", poolV2Pda(input.baseMint), false));
  }
  const buyback = input.buybackFeeRecipient || BUYBACK_FEE_RECIPIENTS[0];
  accounts.push(
    meta("buyback_fee_recipient", buyback, false),
    meta("buyback_fee_recipient_ata", associatedTokenAddress(buyback, quoteMint, quoteProgram), true),
  );
  const data = input.side === "buy"
    ? concatBytes([AMM_BUY_EXACT_QUOTE, u64le(input.limitLamports), u64le(input.amount), new Uint8Array([1])])
    : concatBytes([AMM_SELL, u64le(input.amount), u64le(input.limitLamports)]);
  return {
    ok: true,
    wireReady: true,
    venue: "pumpswap",
    instruction: input.side === "buy" ? "buy_exact_quote_in" : "sell",
    programId: PUMP_AMM,
    accounts,
    data,
    missing: [],
  };
}

export function systemTransfer(from: string, to: string, lamports: bigint): WireInstruction {
  const data = new Uint8Array(12);
  data[0] = 2;
  data.set(u64le(lamports), 4);
  return {
    ok: true,
    wireReady: true,
    venue: "pump",
    instruction: "transfer",
    programId: SYSTEM,
    accounts: [
      meta("from", from, true, true),
      meta("to", to, true),
    ],
    data,
    missing: [],
  };
}

export function syncNative(account: string): WireInstruction {
  return {
    ok: true,
    wireReady: true,
    venue: "pumpswap",
    instruction: "sync_native",
    programId: TOKEN_PROGRAM,
    accounts: [meta("account", account, true)],
    data: new Uint8Array([17]),
    missing: [],
  };
}

export function closeTokenAccount(account: string, destination: string, owner: string): WireInstruction {
  return {
    ok: true,
    wireReady: true,
    venue: "pumpswap",
    instruction: "close_account",
    programId: TOKEN_PROGRAM,
    accounts: [
      meta("account", account, true),
      meta("destination", destination, true),
      meta("owner", owner, true, true),
    ],
    data: new Uint8Array([9]),
    missing: [],
  };
}

export function createAtaIdempotent(payer: string, owner: string, mint: string, tokenProgram: string): WireInstruction {
  const ata = associatedTokenAddress(owner, mint, tokenProgram);
  return {
    ok: true,
    wireReady: true,
    venue: "pump",
    instruction: "create_idempotent",
    programId: ASSOCIATED_TOKEN,
    accounts: [
      meta("payer", payer, true, true),
      meta("ata", ata, true),
      meta("owner", owner, false),
      meta("mint", mint, false),
      meta("system_program", SYSTEM, false),
      meta("token_program", tokenProgram, false),
    ],
    data: new Uint8Array([1]),
    missing: [],
  };
}

export function decodeBondingCurve(data: Uint8Array): { creator: string; mayhem: boolean; quoteMint: string; complete: boolean } | null {
  if (data.length < 83 + 32) return null;
  const creator = b58encode(data.subarray(49, 81));
  const mayhem = data[81] === 1;
  const quoteMint = b58encode(data.subarray(83, 115));
  return { creator, mayhem, quoteMint, complete: data[48] === 1 };
}
