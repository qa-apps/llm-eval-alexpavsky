import { describe, it, expect } from "vitest";

/**
 * Langfuse check for the AI chat agent's token tracking + agentic workflow.
 *
 * Two layers, so the useful part still runs without cloud secrets:
 *
 *  Layer A (always runs): the deployed chat agent answers a real question.
 *    Proves the agent is alive and coherent — the thing Langfuse is tracing.
 *
 *  Layer B (runs only when LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY are set):
 *    send a chat turn tagged with a unique session id, then read it back through
 *    the Langfuse REST API and assert the trace actually recorded (a) the
 *    agentic steps as observations and (b) non-zero token usage. This is the
 *    "token tracking + agentic interactions are wired correctly" check. Because
 *    Langfuse ingests asynchronously, we poll for the trace before asserting.
 *
 * Nothing here mocks the agent or Langfuse — it exercises the live pipeline.
 */

const CHAT_URL = process.env.CHAT_AGENT_URL || "https://alexpavsky.com/api/chat";

const LF_PUBLIC = process.env.LANGFUSE_PUBLIC_KEY || "";
const LF_SECRET = process.env.LANGFUSE_SECRET_KEY || "";
const LF_HOST = (process.env.LANGFUSE_HOST || "https://cloud.langfuse.com").replace(/\/+$/, "");
const hasLangfuseKeys = Boolean(LF_PUBLIC && LF_SECRET);

type ChatResult = { reply: string; sessionId: string; raw: any };

async function askChat(message: string, sessionId: string): Promise<ChatResult> {
  const res = await fetch(CHAT_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    // send several common session-id aliases so whichever one the agent honours
    // lands on the Langfuse trace we later look up.
    body: JSON.stringify({
      message,
      session_id: sessionId,
      sessionId,
      conversation_id: sessionId,
    }),
  });
  if (!res.ok) {
    throw new Error(`chat agent HTTP ${res.status}: ${await res.text()}`);
  }
  const raw: any = await res.json();
  const reply = (raw.reply || raw.answer || raw.message || raw.text || "").trim();
  return { reply, sessionId, raw };
}

function lfHeaders(): Record<string, string> {
  const token = Buffer.from(`${LF_PUBLIC}:${LF_SECRET}`).toString("base64");
  return { authorization: `Basic ${token}`, "content-type": "application/json" };
}

async function lfGet(path: string): Promise<any> {
  const res = await fetch(`${LF_HOST}${path}`, { headers: lfHeaders() });
  if (!res.ok) {
    throw new Error(`Langfuse GET ${path} -> HTTP ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

/** Poll the supported observations endpoint until ingestion includes token usage. */
async function waitForTrace(sessionId: string, attempts = 12, delayMs = 5000): Promise<any> {
  let latestTrace: any = null;
  for (let i = 0; i < attempts; i++) {
    const from = new Date(Date.now() - 5 * 60_000).toISOString();
    const to = new Date(Date.now() + 60_000).toISOString();
    const query = new URLSearchParams({
      sessionId,
      fromStartTime: from,
      toStartTime: to,
      fields: "core,basic,usage",
      limit: "100",
    });
    const page = await lfGet(`/api/public/v2/observations?${query}`);
    const observations: any[] = page?.data || [];
    if (observations.length > 0) {
      latestTrace = { observations };
      if (totalTokens(latestTrace) > 0) return latestTrace;
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  return latestTrace;
}

function totalTokens(trace: any): number {
  const observations: any[] = trace?.observations || [];
  let sum = 0;
  for (const o of observations) {
    const u = o?.usageDetails || o?.usage || {};
    const t =
      o?.totalUsage ??
      u.total ??
      u.totalTokens ??
      (Number(o?.inputUsage || u.input || u.promptTokens || 0) +
        Number(o?.outputUsage || u.output || u.completionTokens || 0));
    sum += Number(t || 0);
  }
  // fall back to trace-level aggregate if observations carried no usage
  if (sum === 0) {
    const tu = trace?.usage || trace?.totalUsage || {};
    sum = Number(tu.total || tu.totalTokens || 0);
  }
  return sum;
}

describe("chat agent — Langfuse token tracking + agentic workflow", () => {
  it("Layer A: the deployed chat agent returns a coherent answer", async () => {
    const sessionId = `pw-lf-a-${Date.now()}`;
    const { reply } = await askChat(
      "In one sentence, what is a flaky test in automation?",
      sessionId,
    );
    expect(reply.length).toBeGreaterThan(0);
    // a real answer should mention the concept, not echo an error envelope
    expect(reply.toLowerCase()).not.toContain("(brain error");
  }, 60_000);

  (hasLangfuseKeys ? it : it.skip)(
    "Layer B: Langfuse recorded the trace with agentic steps and token usage",
    async () => {
      const sessionId = `pw-lf-b-${Date.now()}`;
      const { reply } = await askChat(
        "Why should I add CI quality gates to a test suite?",
        sessionId,
      );
      expect(reply.length).toBeGreaterThan(0);

      const trace = await waitForTrace(sessionId);
      expect(
        trace,
        `No Langfuse trace found for session ${sessionId} at ${LF_HOST}. ` +
          "Either the deployed chat agent is not emitting Langfuse traces, or the " +
          "session id is not propagated to the trace.",
      ).not.toBeNull();

      const observations: any[] = trace?.observations || [];
      expect(
        observations.length,
        "Trace exists but has no observations — the agentic steps were not traced.",
      ).toBeGreaterThan(0);

      const tokens = totalTokens(trace);
      expect(
        tokens,
        "Trace exists but recorded zero tokens — token tracking is not wired.",
      ).toBeGreaterThan(0);
    },
    120_000,
  );
});

if (!hasLangfuseKeys) {
  // eslint-disable-next-line no-console
  console.warn(
    "[langfuse] Layer B skipped: set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY " +
      "(+ optional LANGFUSE_HOST) to verify token tracking against the dashboard.",
  );
}
