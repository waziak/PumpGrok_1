/**
 * Live trading gate. Closed unless every explicit switch is set.
 * The default environment (TRADING_MODE unset or paper) never opens it.
 */

export const LIVE_CONFIRM = "I_UNDERSTAND_LIVE_TRADING_IS_DISABLED_UNTIL_I_SET_THIS";
export const NETWORK_CONFIRM = "I_ACCEPT_NETWORK_SEND";

export type Gate = {
  open: boolean;
  reasons: string[];
  mode: string;
  realTrades: false;
};

export function liveGate(env: Record<string, string | undefined>): Gate {
  const mode = (env.TRADING_MODE || "paper").trim().toLowerCase();
  const reasons: string[] = [];
  if (mode !== "live") reasons.push("mode_not_live");
  if (env.LIVE_TRADING_CONFIRM !== LIVE_CONFIRM) reasons.push("missing_live_confirmation");
  if (env.LIVE_NETWORK_SEND !== NETWORK_CONFIRM) reasons.push("network_send_not_enabled");
  return { open: reasons.length === 0, reasons, mode, realTrades: false };
}

export function describeLive(env: Record<string, string | undefined>): Record<string, unknown> {
  const gate = liveGate(env);
  if (!gate.open) {
    return {
      ok: false,
      refused: true,
      live: false,
      mode: gate.mode === "live" ? "live" : "paper",
      effectiveMode: "paper",
      reasons: gate.reasons,
      signed: false,
      sent: false,
      realTrades: false,
      privateKeyExposed: false,
    };
  }
  return {
    ok: false,
    refused: true,
    liveGate: "open",
    sent: false,
    signed: false,
    realTrades: false,
    privateKeyExposed: false,
    reason: "no network transport is bound in this CLI; refusing to fake a live send",
  };
}
