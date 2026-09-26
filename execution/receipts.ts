/** Duplicate-protected execution receipts. Uncertain results are not retried. */

export type ReceiptStatus = "simulated" | "refused" | "failed" | "uncertain" | "submitted";

export type Receipt = {
  receiptId: string;
  status: ReceiptStatus;
  signature: string | null;
  retry: false;
  payload: Record<string, unknown>;
};

export class ReceiptLog {
  rows = new Map<string, Receipt>();

  static load(json: string): ReceiptLog {
    const log = new ReceiptLog();
    const parsed = JSON.parse(json) as Receipt[];
    for (const row of parsed) log.rows.set(row.receiptId, row);
    return log;
  }

  save(): string {
    return JSON.stringify([...this.rows.values()]);
  }

  record(receipt: Receipt): { ok: true } | { ok: false; error: "duplicate_receipt" } {
    if (this.rows.has(receipt.receiptId)) return { ok: false, error: "duplicate_receipt" };
    this.rows.set(receipt.receiptId, { ...receipt, retry: false });
    return { ok: true };
  }

  get(receiptId: string): Receipt | undefined {
    return this.rows.get(receiptId);
  }

  markUncertain(receiptId: string): Receipt | undefined {
    const row = this.rows.get(receiptId);
    if (!row) return undefined;
    const next: Receipt = { ...row, status: "uncertain", retry: false };
    this.rows.set(receiptId, next);
    return next;
  }
}

export function reconcileSignature(status: "confirmed" | "failed" | null | undefined, rpcThrew: boolean): {
  status: ReceiptStatus;
  retry: false;
} {
  if (rpcThrew || status == null) return { status: "uncertain", retry: false };
  if (status === "failed") return { status: "failed", retry: false };
  return { status: "submitted", retry: false };
}
