/** In-memory position book for the execution layer. Paper positions live in SQLite. */

export type ExecPosition = {
  positionId: string;
  mint: string;
  sizeSol: number;
  remainingFraction: number;
  status: "open" | "partial" | "closed";
};

export class PositionBook {
  rows: ExecPosition[];

  constructor(rows: ExecPosition[] = []) {
    this.rows = rows;
  }

  static load(json: string): PositionBook {
    const parsed = JSON.parse(json) as ExecPosition[];
    return new PositionBook(parsed);
  }

  save(): string {
    return JSON.stringify(this.rows);
  }

  upsert(row: ExecPosition): void {
    const index = this.rows.findIndex((item) => item.positionId === row.positionId);
    if (index >= 0) this.rows[index] = row;
    else this.rows.push(row);
  }

  get(positionId: string): ExecPosition | undefined {
    return this.rows.find((item) => item.positionId === positionId);
  }

  exposure(): number {
    return this.rows
      .filter((item) => item.status === "open" || item.status === "partial")
      .reduce((sum, item) => sum + item.sizeSol * item.remainingFraction, 0);
  }
}
