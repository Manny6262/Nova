export function normalizePhone(value) {
  return String(value || "").replace(/[^+\d]/g, "");
}

export function inboundText(space, message, allowedUser) {
  if (!message || (message.direction && message.direction !== "inbound")) return null;
  if ((space?.type || message.space?.type || "dm") !== "dm") return null;
  if (normalizePhone(message.sender?.id) !== normalizePhone(allowedUser)) return null;
  if (message.content?.type !== "text") return null;
  const content = String(message.content.text || "").trim();
  if (!content || content.length > 12000) return null;
  return content;
}

export async function novaReply(baseUrl, content, sessionId, fetcher = fetch) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 180000);
  try {
    const response = await fetcher(baseUrl + "/chat", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        message: content, session_id: sessionId, provider: "local",
        caps: { write: false, exec: false, web: false, control: false },
        approval_mode: "ask", think: false,
      }), signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error("Nova engine unavailable");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = ""; let answer = ""; let nextSession = sessionId;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (Number.isInteger(event.session_id)) nextSession = event.session_id;
        if (event.confirm_request?.token) {
          // Text cannot show a trusted native approval dialog. Decline here.
          await fetcher(baseUrl + "/confirm", { method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ token: event.confirm_request.token, approved: false }) });
        }
        if (event.error) throw new Error("Nova could not complete this reply");
        if (typeof event.token === "string") answer += event.token;
        if (answer.length > 16000) throw new Error("Nova reply exceeded the text limit");
      }
    }
    return { text: answer.trim() || "I couldn't finish that reply. Please try again in Nova Native.", sessionId: nextSession };
  } finally { clearTimeout(timer); }
}
