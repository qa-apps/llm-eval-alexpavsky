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
 *    Langfuse ingests asynchronously, we poll for the observations before
 *    asserting.
 *
 * The read goes through `GET /api/public/v2/observations`. The v1 endpoints this
 * check used before (`/api/public/traces?sessionId=` plus `/api/public/traces/{id}`)
 * are removed on 2026-11-16, are capped at 5 requests/minute per organization,
 * and can lag real time by ~10 minutes. v2 is the real-time read path, sits in
 * the general API bucket (>= 30 requests/minute), and has no get-by-id route:
 * you read observation rows and group them by trace yourself.
 *
 * Nothing here mocks the agent or Langfuse — it exercises the live pipeline.
 */

const CHAT_URL = process.env.CHAT_AGENT_URL || "https://alexpavsky.com/api/chat";

const LF_PUBLIC = process.env.LANGFUSE_PUBLIC_KEY || "";
const LF_SECRET = process.env.LANGFUSE_SECRET_KEY || "";
const LF_HOST = (process.env.LANGFUSE_HOST || "https://cloud.langfuse.com").replace(/\/+$/, "");
const hasLangfuseKeys = Boolean(LF_PUBLIC && LF_SECRET);

const OBSERVATIONS_PATH = "/api/public/v2/observations";

/** Whole-test budget. Shared with the `it()` timeout so polling always stops
 *  early enough to report a real assertion instead of a bare vitest timeout. */
const LAYER_B_TIMEOUT_MS = 120_000;
const POLL_INTERVAL_MS = Number(process.env.LANGFUSE_POLL_INTERVAL_MS || 5_000);
const POLL_BUDGET_MS = Number(process.env.LANGFUSE_POLL_BUDGET_MS || 85_000);
/** Trace is seconds old by the time we poll; a wide floor costs nothing. */
const LOOKBACK_MS = 15 * 60_000;
const MAX_PAGES = 10;

type ChatResult = { reply: string; sessionId: string; raw: any };

type Observation = {
  id: string;
  traceId?: string | null;
  sessionId?: string | null;
  type?: string | null;
  usageDetails?: Record<string, number> | null;
};

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

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, Math.max(0, ms)));
}

/** Responses worth waiting out rather than failing on: rate limiting (429) and
 *  transient server errors. Everything else (401, 403, 400) is a configuration
 *  problem that no amount of polling fixes. */
function isRetryable(status: number): boolean {
  return status === 429 || status >= 500;
}

/** Langfuse sends `Retry-After` on a 429 and repeats it in the error envelope
 *  as `details.retryAfterSeconds`. Honour whichever one we get. */
function retryDelayMs(res: Response, body: string): number | null {
  const header = res.headers.get("retry-after");
  if (header) {
    const seconds = Number(header);
    if (Number.isFinite(seconds)) {
      return seconds * 1000;
    }
    const until = Date.parse(header);
    if (!Number.isNaN(until)) {
      return Math.max(0, until - Date.now());
    }
  }
  try {
    const seconds = Number(JSON.parse(body)?.details?.retryAfterSeconds);
    if (Number.isFinite(seconds)) {
      return seconds * 1000;
    }
  } catch {
    // not JSON — fall back to the caller's poll interval
  }
  return null;
}

type LfGetResult =
  | { ok: true; body: any }
  | { ok: false; status: number; retryAfterMs: number | null; text: string };

async function lfGet(path: string): Promise<LfGetResult> {
  const res = await fetch(`${LF_HOST}${path}`, { headers: lfHeaders() });
  if (res.ok) {
    return { ok: true, body: await res.json() };
  }
  const text = await res.text();
  return { ok: false, status: res.status, retryAfterMs: retryDelayMs(res, text), text };
}

/**
 * v2 has no `?sessionId=` of its own that we can rely on — the documented
 * replacement for `GET /traces?sessionId=` is a structured `filter` condition
 * on the `sessionId` column, which takes precedence over the plain query
 * parameters. `fields` is opt-in, so ask for `usage` explicitly or
 * `usageDetails` comes back absent.
 */
function observationsPath(sessionId: string, since: Date, cursor: string | null): string {
  const params = new URLSearchParams({
    fields: "core,basic,usage",
    filter: JSON.stringify([
      { type: "string", column: "sessionId", operator: "=", value: sessionId },
    ]),
    fromStartTime: since.toISOString(),
    toStartTime: new Date(Date.now() + 5 * 60_000).toISOString(),
    limit: "100",
  });
  if (cursor) {
    params.set("cursor", cursor);
  }
  return `${OBSERVATIONS_PATH}?${params.toString()}`;
}

type ReadResult = { rows: Observation[] } | { retryAfterMs: number | null; detail: string };

/** One sweep of the session's observations, following the cursor pages. */
async function readSessionObservations(sessionId: string, since: Date): Promise<ReadResult> {
  const rows: Observation[] = [];
  let cursor: string | null = null;

  for (let page = 0; page < MAX_PAGES; page++) {
    const res = await lfGet(observationsPath(sessionId, since, cursor));
    if (!res.ok) {
      if (isRetryable(res.status)) {
        return {
          retryAfterMs: res.retryAfterMs,
          detail: `HTTP ${res.status}: ${res.text.slice(0, 300)}`,
        };
      }
      throw new Error(`Langfuse GET ${OBSERVATIONS_PATH} -> HTTP ${res.status}: ${res.text}`);
    }
    rows.push(...((res.body?.data as Observation[]) || []));
    cursor = res.body?.meta?.cursor || null;
    if (!cursor) {
      break;
    }
  }
  return { rows };
}

/**
 * Poll until the asynchronously ingested session carries observations with
 * token usage. Trace creation and generation events are ingested
 * independently, so a session can briefly show up with no usage yet; a 429 or
 * 5xx means "not ready yet" too, and used to fail the whole check whenever two
 * workflow runs polled Langfuse in the same window.
 */
async function waitForSessionObservations(sessionId: string, budgetMs: number) {
  const since = new Date(Date.now() - LOOKBACK_MS);
  const deadline = Date.now() + budgetMs;
  let rows: Observation[] = [];
  let foreignRows = 0;
  let lastTransient: string | null = null;

  for (;;) {
    const result = await readSessionObservations(sessionId, since);

    if ("rows" in result) {
      // Narrow to our own session: if the server-side filter were ever ignored,
      // another run's rows must not be able to satisfy these assertions.
      foreignRows = result.rows.filter((o) => o.sessionId && o.sessionId !== sessionId).length;
      rows = result.rows.filter((o) => !o.sessionId || o.sessionId === sessionId);
      if (rows.length > 0 && totalTokens(rows) > 0) {
        break;
      }
    } else {
      lastTransient = result.detail;
    }

    const waitMs = "rows" in result ? POLL_INTERVAL_MS : (result.retryAfterMs ?? POLL_INTERVAL_MS);
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      break;
    }
    await sleep(Math.min(waitMs, remaining));
  }

  return { rows, foreignRows, lastTransient };
}

/** v2 reports usage as `usageDetails`, a map of usage-metric name to units.
 *  Prefer an explicit total; otherwise sum the component metrics rather than
 *  every key, so a `total` entry is never counted twice. */
function totalTokens(observations: Observation[]): number {
  let sum = 0;
  for (const o of observations) {
    const usage = o?.usageDetails || {};
    const explicit = Number(usage.total ?? usage.totalTokens ?? 0);
    if (explicit > 0) {
      sum += explicit;
      continue;
    }
    for (const [metric, units] of Object.entries(usage)) {
      if (/^total/i.test(metric)) {
        continue;
      }
      sum += Number(units) || 0;
    }
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
      const startedAt = Date.now();
      const sessionId = `pw-lf-b-${Date.now()}`;
      const { reply } = await askChat(
        "Why should I add CI quality gates to a test suite?",
        sessionId,
      );
      expect(reply.length).toBeGreaterThan(0);

      // leave a margin so the assertions below, not vitest, report the failure
      const budgetMs = Math.min(
        POLL_BUDGET_MS,
        Math.max(POLL_INTERVAL_MS, LAYER_B_TIMEOUT_MS - (Date.now() - startedAt) - 10_000),
      );
      const { rows, foreignRows, lastTransient } = await waitForSessionObservations(
        sessionId,
        budgetMs,
      );

      if (foreignRows > 0) {
        // eslint-disable-next-line no-console
        console.warn(
          `[langfuse] ${foreignRows} observation(s) for other sessions came back from the ` +
            "sessionId filter; narrowed client-side.",
        );
      }
      const transientNote = lastTransient
        ? ` Last transient Langfuse response: ${lastTransient}.`
        : "";

      expect(
        rows.length,
        `No Langfuse observations found for session ${sessionId} at ${LF_HOST} within ` +
          `${budgetMs}ms. Either the chat agent emitted no trace for this session, or its ` +
          "traces still arrive through the legacy batch ingestion API — v2 reads serve live " +
          `data only for OpenTelemetry ingestion, and otherwise lag.${transientNote}`,
      ).toBeGreaterThan(0);

      const traceIds = new Set(rows.map((o) => o.traceId).filter(Boolean));
      expect(
        traceIds.size,
        "Observations exist but none carry a traceId — the agentic steps were not grouped " +
          "into a trace.",
      ).toBeGreaterThan(0);

      const tokens = totalTokens(rows);
      expect(
        tokens,
        "Observations exist for this session but none carried token usage: no GENERATION " +
          "reported usageDetails, so the route that answered did not record a model call." +
          transientNote,
      ).toBeGreaterThan(0);
    },
    LAYER_B_TIMEOUT_MS,
  );
});

if (!hasLangfuseKeys) {
  // eslint-disable-next-line no-console
  console.warn(
    "[langfuse] Layer B skipped: set LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY " +
      "(+ optional LANGFUSE_HOST) to verify token tracking against the dashboard.",
  );
}
