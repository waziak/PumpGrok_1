import assert from "node:assert/strict";
import { test } from "node:test";

import { WSOL } from "../venues.ts";
import { createSolanaRpc } from "../rpc.ts";

test("rpc transport simulates and sends as separate calls without retry", async () => {
  const methods: string[] = [];
  const rpc = createSolanaRpc("http://rpc.test", {
    confirmMs: 1,
    sleep: async () => {},
    fetchImpl: async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as { method: string };
      methods.push(body.method);
      if (body.method === "getLatestBlockhash") {
        return Response.json({ result: { value: { blockhash: WSOL } } });
      }
      if (body.method === "simulateTransaction") {
        return Response.json({ result: { value: { err: null } } });
      }
      if (body.method === "sendRawTransaction") {
        const params = (JSON.parse(String(init?.body)) as { params: unknown[] }).params;
        const options = params[1] as { maxRetries?: number };
        assert.equal(options.maxRetries, 0);
        return Response.json({ result: "5".repeat(40) });
      }
      if (body.method === "getSignatureStatuses") {
        return Response.json({ result: { value: [{ confirmationStatus: "confirmed", err: null }] } });
      }
      return Response.json({ error: { message: "unexpected" } });
    },
  });
  const latest = await rpc.getLatestBlockhash();
  assert.equal(latest.blockhash.length, 32);
  const simulated = await rpc.simulateWire(Buffer.from([1, 0, 0]).toString("base64"));
  assert.equal(simulated.ok, true);
  const sent = await rpc.send(Buffer.from("signed").toString("base64"));
  assert.equal(typeof sent.signature, "string");
  assert.equal(await rpc.confirm(sent.signature || ""), "confirmed");
  assert.deepEqual(methods, ["getLatestBlockhash", "simulateTransaction", "sendRawTransaction", "getSignatureStatuses"]);
});
