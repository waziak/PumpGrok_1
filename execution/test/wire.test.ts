import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { PAPER_BOT_READS_KEYPAIR } from "../../bot/run.ts";
import { b58encode, pubkeyBytes } from "../codec.ts";
import { prepareInstructions, runControlled } from "../controlled.ts";
import { describeLive, signGate } from "../gate.ts";
import { ReceiptLog } from "../receipts.ts";
import { signTransaction } from "../wallet.ts";
import {
  SYSTEM,
  WSOL,
  bondingCurvePda,
  buildPumpInstruction,
  buildPumpSwapInstruction,
  canonicalPumpSwapPool,
  poolV2Pda,
} from "../venues.ts";

const NOW = Date.parse("2026-09-26T18:00:00Z");
const USER = "2LmzxcxCfijZANvbiDrf8DcVRgUqY6PFwfB7wPVbsXCp";
const PUMP_MINT = "6MwwrxeHWTwwSqWQp1huASBYF1F1azGQjva6iEWkpump";
const PUMP_CREATOR = "AYjX7q4qoAxF5UqpFW75RCVWAeZjvac4gqkEPuyhAYi4";
const TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb";
const SWAP_MINT = "kFWgzdFBd6DERYGSWUrT3ZUUjoREcv14rHJCBSbpump";
const SWAP_CREATOR = "AX5a8iS8qGE3fD6c24G6X7uUKeXMjvHPzXBZmUNLtsxB";
const SWAP_USER = "5M9DGiWmZUCTFWMB7UxQ2UE8PzrmkDX3WYF2M6HgKoJV";
const PROTOCOL_FEE = "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ";
const BUYBACK = "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD";

const MAINNET_PUMPSWAP_BUY = [
  "6i2FWwFBWpZiLnnUTkpL7RJJJP9ju7WBR11GzaZrdhDb",
  "5M9DGiWmZUCTFWMB7UxQ2UE8PzrmkDX3WYF2M6HgKoJV",
  "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw",
  "kFWgzdFBd6DERYGSWUrT3ZUUjoREcv14rHJCBSbpump",
  "So11111111111111111111111111111111111111112",
  "XWNcLPzJRE6DG96Pk29DoXemLFDDmo3ejYfbXfZLHy1",
  "FNttjks33cVFX29hJXKvmcasLfVDjK59jFKT6XQD18fq",
  "DD1XstjjSNYdS5tpp72oaAA3CFhsKTvhztomd18jfecL",
  "Geit9zQNrNih2D4fGMczVK1Ac79p8wTyvWrnLiNN13mB",
  "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
  "7GFUN3bWzJMKMRZ34JLsvcqdssDbXnp589SiE33KVwcC",
  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
  "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
  "11111111111111111111111111111111",
  "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
  "GS4CU59F31iL7aR2Q8zVS8DRrcRnXX1yjQ66TqNVQnaR",
  "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
  "Co7Fm7PtpS1az5S46mgeKtZi15msKeivt1NyHGWxX2bi",
  "7RmnVm5ghMfWfknuzu41tvPKsXZBWSwHBqsE5ZTLrSif",
  "C2aFPdENg4A2HQsmrd5rTw5TaYBX5Ku887cWjbFKtZpw",
  "GesXvNcQDLcbLT2fz6t1Cwkt1S7tcG95dDe7EhfbcRq8",
  "5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx",
  "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
  "7H3or96gQG55fWqnnuU132hzQkJPNGg9o68vDXEkr8cJ",
  "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
  "CASRL2zkwDnppxEFQ4LgdwgR9pdz5Q8R8nEMKVZ9QoLp",
];

function candidate() {
  return {
    candidate_id: "cand-wire",
    mint: PUMP_MINT,
    program: "pump",
    observed_at: "2026-09-26T18:00:00Z",
    proposed_size_sol: 0.005,
    slippage_bps: 100,
    features: {
      mint_authority: "revoked",
      freeze_authority: "revoked",
      liquidity_sol: 20,
      top_holder_pct: 0.05,
      route_ok: true,
      price_impact_bps: 20,
      price_sol: 0.00002,
    },
  };
}

function trade() {
  return {
    venue: "pump" as const,
    side: "buy" as const,
    user: USER,
    baseMint: PUMP_MINT,
    creator: PUMP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    amount: 1n,
    limitLamports: 5_000_000n,
  };
}

test("system program pubkey roundtrips", () => {
  const decoded = pubkeyBytes(SYSTEM);
  assert.equal(decoded.length, 32);
  assert.equal(decoded.every((byte) => byte === 0), true);
  assert.equal(b58encode(decoded), SYSTEM);
  assert.equal(pubkeyBytes(WSOL).length, 32);
});

test("pump and pumpswap builders are wire ready on mainnet-derived accounts", () => {
  assert.equal(bondingCurvePda(PUMP_MINT), "4zXBoLoBsMbQJyiYnK23abYdH3jVTmLEwzhT8QfEMswB");
  assert.equal(canonicalPumpSwapPool(SWAP_MINT), "6i2FWwFBWpZiLnnUTkpL7RJJJP9ju7WBR11GzaZrdhDb");
  assert.equal(poolV2Pda(SWAP_MINT), "7H3or96gQG55fWqnnuU132hzQkJPNGg9o68vDXEkr8cJ");

  const buy = buildPumpInstruction({
    side: "buy",
    user: USER,
    baseMint: PUMP_MINT,
    creator: PUMP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    amount: 1n,
    limitLamports: 5_000_000n,
  });
  assert.equal(buy.ok, true);
  if (!buy.ok) return;
  assert.equal(buy.wireReady, true);
  assert.equal(buy.instruction, "buy_exact_quote_in_v2");
  assert.equal(buy.accounts.length, 27);
  assert.equal(buy.accounts[2].pubkey, WSOL);
  assert.equal(buy.accounts[10].pubkey, bondingCurvePda(PUMP_MINT));
  assert.equal(Buffer.from(buy.data.subarray(0, 8)).toString("hex"), "c2ab1c46684d5b2f");

  const sell = buildPumpInstruction({
    side: "sell",
    user: USER,
    baseMint: PUMP_MINT,
    creator: PUMP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    amount: 1n,
    limitLamports: 1n,
  });
  assert.equal(sell.ok, true);
  if (!sell.ok) return;
  assert.equal(sell.wireReady, true);
  assert.equal(sell.accounts.length, 26);
  assert.equal(sell.instruction, "sell_v2");
  assert.equal(sell.accounts.some((account) => account.name === "global_volume_accumulator"), false);

  const swap = buildPumpSwapInstruction({
    side: "buy",
    user: SWAP_USER,
    baseMint: SWAP_MINT,
    coinCreator: SWAP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    protocolFeeRecipient: PROTOCOL_FEE,
    buybackFeeRecipient: BUYBACK,
    amount: 1n,
    limitLamports: 5_000_000n,
  });
  assert.equal(swap.ok, true);
  if (!swap.ok) return;
  assert.equal(swap.wireReady, true);
  assert.equal(swap.instruction, "buy_exact_quote_in");
  assert.deepEqual(swap.accounts.map((account) => account.pubkey), MAINNET_PUMPSWAP_BUY);
  assert.equal(swap.accounts[2].pubkey, "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw");
  assert.equal(Buffer.from(swap.data.subarray(0, 8)).toString("hex"), "c62e1552b4d9e870");

  const swapSell = buildPumpSwapInstruction({
    side: "sell",
    user: SWAP_USER,
    baseMint: SWAP_MINT,
    coinCreator: SWAP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    protocolFeeRecipient: PROTOCOL_FEE,
    buybackFeeRecipient: BUYBACK,
    amount: 1n,
    limitLamports: 1n,
  });
  assert.equal(swapSell.ok, true);
  if (!swapSell.ok) return;
  assert.equal(swapSell.wireReady, true);
  assert.equal(swapSell.accounts.length, 24);
  assert.equal(swapSell.accounts[21].name, "pool_v2");
  assert.equal(swapSell.accounts[21].pubkey, poolV2Pda(SWAP_MINT));

  const missing = buildPumpInstruction({
    side: "buy",
    user: USER,
    baseMint: PUMP_MINT,
    creator: "",
    baseTokenProgram: TOKEN_2022,
    amount: 1n,
    limitLamports: 1n,
  });
  assert.equal(missing.ok, false);
  if (!missing.ok) {
    assert.equal(missing.wireReady, false);
    assert.equal(missing.missing.includes("creator"), true);
  }
});

test("defaults do not sign or send", async () => {
  const report = describeLive({});
  assert.equal(report.sent, false);
  assert.equal(report.signed, false);
  assert.equal(signGate({}).open, false);
  let sends = 0;
  const result = await runControlled({
    env: {},
    candidate: candidate(),
    trade: trade(),
    rpc: {
      async getBalance() {
        return 0;
      },
      async simulate() {
        return { ok: true };
      },
      async simulateWire() {
        return { ok: true };
      },
      async send() {
        sends += 1;
        return { signature: "should-not-send" };
      },
      async confirm() {
        return "confirmed";
      },
    },
    jito: { async sendBundle() { throw new Error("jito_disabled"); } },
    receipts: new ReceiptLog(),
    receiptId: "paper-wire",
    nowMs: NOW,
    openExposureSol: 0,
    walletSol: 1,
    blockhash: randomBytes(32),
    blockhashFetchedAtMs: NOW,
    keypairPath: join(tmpdir(), "missing-pumpgrok-keypair.json"),
  });
  assert.equal(result.sent, false);
  assert.equal(result.signed, false);
  assert.equal(result.realTrades, false);
  assert.equal(result.wireReady, true);
  assert.equal(sends, 0);
});

test("live mode without network send can simulate and sign without broadcasting", async () => {
  const dir = mkdtempSync(join(tmpdir(), "pumpgrok-sign-"));
  const file = join(dir, "wallet.json");
  const secret = [...randomBytes(64)];
  writeFileSync(file, JSON.stringify(secret));
  let sends = 0;
  try {
    const closed = signTransaction(file, randomBytes(32), { TRADING_MODE: "paper" });
    assert.equal(closed.ok, false);
    const result = await runControlled({
      env: { TRADING_MODE: "live", LIVE_NETWORK_SEND: "false" },
      candidate: candidate(),
      trade: trade(),
      rpc: {
        async getBalance() {
          return 1_000_000_000;
        },
        async simulate() {
          return { ok: true };
        },
        async simulateWire() {
          return { ok: true };
        },
        async send() {
          sends += 1;
          return { signature: "should-not-send" };
        },
        async confirm() {
          return "confirmed";
        },
      },
      jito: { async sendBundle() { throw new Error("jito_disabled"); } },
      receipts: new ReceiptLog(),
      receiptId: "dry-sign",
      nowMs: NOW,
      openExposureSol: 0,
      walletSol: 1,
      blockhash: randomBytes(32),
      blockhashFetchedAtMs: NOW,
      keypairPath: file,
    });
    assert.equal(result.sent, false);
    assert.equal(result.signed, true);
    assert.equal(result.realTrades, false);
    assert.equal(result.wireReady, true);
    assert.equal(result.secretMaterialExposed, false);
    assert.equal(sends, 0);
    const encoded = JSON.stringify(result);
    assert.equal(encoded.includes(JSON.stringify(secret)), false);
    assert.equal(encoded.includes(secret.slice(0, 8).join(",")), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("pumpswap buys wrap SOL and pump buys spend native SOL", () => {
  const pump = buildPumpInstruction({
    side: "buy",
    user: USER,
    baseMint: PUMP_MINT,
    creator: PUMP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    amount: 1n,
    limitLamports: 5_000_000n,
  });
  const swap = buildPumpSwapInstruction({
    side: "buy",
    user: USER,
    baseMint: SWAP_MINT,
    coinCreator: SWAP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    protocolFeeRecipient: PROTOCOL_FEE,
    amount: 1n,
    limitLamports: 5_000_000n,
  });
  assert.equal(pump.ok && swap.ok, true);
  if (!pump.ok || !swap.ok) return;
  const pumpIxs = prepareInstructions(trade(), pump);
  const swapIxs = prepareInstructions({
    venue: "pumpswap",
    side: "buy",
    user: USER,
    baseMint: SWAP_MINT,
    coinCreator: SWAP_CREATOR,
    baseTokenProgram: TOKEN_2022,
    protocolFeeRecipient: PROTOCOL_FEE,
    amount: 1n,
    limitLamports: 5_000_000n,
  }, swap);
  assert.equal(pumpIxs.some((ix) => ix.instruction === "sync_native"), false);
  assert.equal(pumpIxs.some((ix) => ix.instruction === "transfer"), false);
  assert.equal(pumpIxs.at(-1)?.instruction, "buy_exact_quote_in_v2");
  assert.deepEqual(swapIxs.map((ix) => ix.instruction), [
    "create_idempotent",
    "create_idempotent",
    "transfer",
    "sync_native",
    "buy_exact_quote_in",
  ]);
});

test("paper bot source does not read a keypair", () => {
  assert.equal(PAPER_BOT_READS_KEYPAIR, false);
  const source = readFileSync(new URL("../../bot/run.ts", import.meta.url), "utf8");
  assert.equal(source.includes("execution/wallet"), false);
  assert.equal(source.includes("SOLANA_KEYPAIR_PATH"), false);
  assert.equal(source.includes("signTransaction"), false);
});
