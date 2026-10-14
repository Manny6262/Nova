import { test } from "node:test";
import assert from "node:assert/strict";
import { ReadableStream } from "node:stream/web";
import { inboundText, novaReply } from "./bridge-core.mjs";

test("only allowlisted inbound direct text reaches Nova", () => {
  const message = { id: "1", direction: "inbound", sender: { id: "+15550001111" }, content: { type: "text", text: " Hello " } };
  assert.equal(inboundText({ type: "dm" }, message, "+1 (555) 000-1111"), "Hello");
  assert.equal(inboundText({ type: "group" }, message, "+15550001111"), null);
  assert.equal(inboundText({ type: "dm" }, message, "+15550002222"), null);
  assert.equal(inboundText({ type: "dm" }, { ...message, direction: "outbound" }, "+15550001111"), null);
  assert.equal(inboundText({ type: "dm" }, { ...message, content: { type: "attachment" } }, "+15550001111"), null);
});

test("bridge refuses tool confirmations and collects only reply text", async () => {
  const encoder = new TextEncoder(); const calls = [];
  const stream = new ReadableStream({ start(controller) {
    controller.enqueue(encoder.encode('{"session_id":42}\n{"token":"Hi"}\n{"confirm_request":{"token":"abc"}}\n{"token":" User"}\n{"done":true}\n'));
    controller.close();
  } });
  const fetcher = async (url, options) => { calls.push([url, JSON.parse(options.body)]); return url.endsWith("/chat") ? { ok: true, body: stream } : { ok: true }; };
  const result = await novaReply("http://127.0.0.1:5100", "Hello", undefined, fetcher);
  assert.deepEqual(result, { text: "Hi User", sessionId: 42 });
  assert.deepEqual(calls[0][1].caps, { write: false, exec: false, web: false, control: false });
  assert.deepEqual(calls[1][1], { token: "abc", approved: false });
});
