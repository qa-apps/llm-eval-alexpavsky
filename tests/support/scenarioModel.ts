import type { LanguageModel } from "ai";
import { createOpenAI } from "@ai-sdk/openai";
import { AgentRole, type AgentInput, type JudgeAgentAdapter, type JudgeResult } from "@langwatch/scenario";

type CriterionDecision = {
  index: number;
  met: boolean;
  explanation: string;
};

type StrictJudgePayload = {
  results: CriterionDecision[];
  reasoning: string;
};

function localConfig() {
  const baseURL = process.env.LOCAL_LLM_BASE_URL;
  if (!baseURL) throw new Error("Scenario needs an evaluator gateway. Set LOCAL_LLM_BASE_URL.");
  const apiKey = process.env.LOCAL_LLM_API_KEY;
  if (!apiKey) throw new Error("Scenario needs the evaluator gateway key. Set LOCAL_LLM_API_KEY.");
  return {
    apiKey,
    baseURL: baseURL.replace(/\/$/, ""),
    model: process.env.SCENARIO_JUDGE_MODEL || "cloud-eval",
    upstreamModel: process.env.LOCAL_LLM_UPSTREAM_MODEL || "cloud-eval",
  };
}

/**
 * The LLM that drives Scenario's user-simulator and judge agents.
 *
 * This is the *evaluator* model and is completely separate from the agent under
 * test (the deployed voice assistant). It needs solid tool-calling (the judge
 * emits a structured finish_test verdict). Preference order picks the key that
 * CI uses the cloud evaluation gateway.
 * Override the model id with SCENARIO_JUDGE_MODEL.
 *
 * OpenAI-compatible providers use the Chat Completions API (`.chat()`): the AI
 * SDK default (Responses API) is not available on every compatible endpoint.
 */
export function judgeModel(): LanguageModel {
  const config = localConfig();
  const p = createOpenAI({
    apiKey: config.apiKey,
    baseURL: config.baseURL,
    headers: {
      "X-LLM-Job-ID": process.env.GITHUB_RUN_ID || "local-scenario",
      "X-LLM-Model": config.upstreamModel,
    },
  });
  return p.chat(config.model);
}

export function parseStrictJudgePayload(raw: string, criteria: string[]): JudgeResult {
  const cleaned = raw.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  const payload = JSON.parse(cleaned) as StrictJudgePayload;
  if (!Array.isArray(payload.results) || payload.results.length !== criteria.length) {
    throw new Error(`Judge returned ${payload.results?.length ?? 0}/${criteria.length} criterion decisions`);
  }

  const byIndex = new Map<number, CriterionDecision>();
  for (const decision of payload.results) {
    if (!Number.isInteger(decision.index) || typeof decision.met !== "boolean") {
      throw new Error("Judge returned an invalid criterion decision");
    }
    if (byIndex.has(decision.index)) throw new Error("Judge returned a duplicate criterion index");
    byIndex.set(decision.index, decision);
  }

  const decisions = criteria.map((_, index) => {
    const decision = byIndex.get(index + 1);
    if (!decision) throw new Error(`Judge omitted criterion ${index + 1}`);
    return decision;
  });
  const metCriteria = criteria.filter((_, index) => decisions[index].met);
  const unmetCriteria = criteria.filter((_, index) => !decisions[index].met);
  const details = decisions
    .map((decision) => `${decision.index}. ${decision.met ? "PASS" : "FAIL"}: ${decision.explanation || "No explanation"}`)
    .join("\n");

  return {
    success: unmetCriteria.length === 0,
    reasoning: `${payload.reasoning || "Judge evaluation"}\n${details}`,
    metCriteria,
    unmetCriteria,
  };
}

export function strictLocalJudge(criteria: string[]): JudgeAgentAdapter {
  return {
    name: "strict-cloud-eval-judge",
    role: AgentRole.JUDGE,
    criteria,
    call: async (input: AgentInput) => {
      const config = localConfig();
      const transcript = JSON.stringify(input.messages);
      const prompt = [
        "Evaluate the transcript against every criterion.",
        "Return one JSON object only, with this exact shape:",
        '{"results":[{"index":1,"met":true,"explanation":"..."}],"reasoning":"..."}',
        "Include exactly one result for every numbered criterion. Use a JSON boolean for met.",
        "Do not add a separate verdict; the test runner derives it from all criterion booleans.",
        "",
        "Criteria:",
        ...criteria.map((criterion, index) => `${index + 1}. ${criterion}`),
        "",
        `Transcript: ${transcript}`,
      ].join("\n");

      let lastError: unknown;
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        try {
          const response = await fetch(`${config.baseURL}/chat/completions`, {
            method: "POST",
            headers: {
              authorization: `Bearer ${config.apiKey}`,
              "content-type": "application/json",
              "X-LLM-Job-ID": process.env.GITHUB_RUN_ID || "local-scenario",
              "X-LLM-Model": config.upstreamModel,
            },
            body: JSON.stringify({
              model: config.model,
              messages: [
                { role: "system", content: "You are a strict QA evaluator. Follow the JSON contract exactly." },
                { role: "user", content: prompt },
              ],
              temperature: 0,
              max_tokens: 1200,
              response_format: { type: "json_object" },
              stream: false,
            }),
          });
          if (!response.ok) throw new Error(`judge HTTP ${response.status}: ${await response.text()}`);
          const data: any = await response.json();
          const content = data?.choices?.[0]?.message?.content;
          if (typeof content !== "string" || !content.trim()) throw new Error("Judge returned no content");
          return parseStrictJudgePayload(content, criteria);
        } catch (error) {
          lastError = error;
        }
      }
      throw lastError;
    },
  };
}

/** True when at least one evaluator LLM key is present. */
export function hasJudgeModel(): boolean {
  return Boolean(process.env.LOCAL_LLM_BASE_URL && process.env.LOCAL_LLM_API_KEY);
}
