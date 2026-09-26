/**
 * Qualitative Grok notes. The key is never returned, logged, or written.
 * Missing key: GROK_API_KEY_REQUIRED. Timeout: features stay UNKNOWN.
 */

export type FetchImpl = (url: string, init?: RequestInit) => Promise<Response>;

const KEY_NAMES = ["XAI_API_KEY", "GROK_API_KEY", "GROKBOT_GROK_API_KEY"] as const;

export function grokKeyState(env: Record<string, string | undefined>): { available: boolean; reason?: string } {
  const present = KEY_NAMES.some((name) => {
    const value = env[name];
    return typeof value === "string" && value.trim().length > 0;
  });
  if (!present) return { available: false, reason: "GROK_API_KEY_REQUIRED" };
  return { available: true };
}

export async function qualitativeNotes(
  env: Record<string, string | undefined>,
  mints: { mint: string; symbol?: string | null }[],
  opts: { fetchImpl?: FetchImpl; timeoutMs?: number } = {},
): Promise<{ available: boolean; reason?: string; notes: { mint: string; narrative: string }[] }> {
  const state = grokKeyState(env);
  if (!state.available || mints.length === 0) return { ...state, notes: [] };
  const key = KEY_NAMES.map((name) => env[name]).find((value) => typeof value === "string" && value.trim()) as string;
  const fetchImpl = opts.fetchImpl || fetch;
  const timeoutMs = opts.timeoutMs ?? 8_000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl("https://api.x.ai/v1/chat/completions", {
      method: "POST",
      signal: controller.signal,
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${key}`,
      },
      body: JSON.stringify({
        model: "grok-3",
        temperature: 0,
        messages: [
          {
            role: "user",
            content:
              "Reply with JSON {notes:[{mint,narrative}]} . narrative is one short qualitative sentence. Do not invent numbers, authorities, or holder stats. Mints: " +
              mints.map((item) => `${item.mint} ${item.symbol || ""}`).join("; "),
          },
        ],
      }),
    });
    if (!response.ok) return { available: true, reason: "GROK_UNAVAILABLE", notes: [] };
    const body = (await response.json()) as {
      choices?: { message?: { content?: string } }[];
    };
    const content = body.choices?.[0]?.message?.content || "";
    const parsed = JSON.parse(content) as { notes?: { mint?: string; narrative?: string }[] };
    const notes = Array.isArray(parsed.notes)
      ? parsed.notes
          .filter((item) => typeof item.mint === "string" && typeof item.narrative === "string")
          .map((item) => ({ mint: item.mint as string, narrative: (item.narrative as string).slice(0, 500) }))
      : [];
    return { available: true, notes };
  } catch (error) {
    const aborted = error instanceof Error && (error.name === "AbortError" || error.message.includes("abort"));
    return { available: true, reason: aborted ? "GROK_TIMEOUT" : "GROK_UNAVAILABLE", notes: [] };
  } finally {
    clearTimeout(timer);
  }
}
