import type { LanguageModel } from "ai";
import { createRequire } from "node:module";
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

function judgeConfig() {
  const baseURL = "https://api.deepseek.com/v1";
  const apiKey = process.env.DEEPSEEK_API_KEY;
  if (!apiKey) throw new Error("Scenario needs DEEPSEEK_API_KEY.");
  return {
    apiKey,
    baseURL,
    model: process.env.SCENARIO_JUDGE_MODEL || "deepseek-v4-pro",
  };
}

/**
 * The LLM that drives Scenario's user-simulator and judge agents.
 *
 * This is the *evaluator* model and is completely separate from the agent under
 * test (the deployed voice assistant). It needs solid tool-calling (the judge
 * emits a structured finish_test verdict). Preference order picks the key that
 * CI and developer eval runs use the DeepSeek cloud judge.
 * Override the model id with SCENARIO_JUDGE_MODEL.
 *
 * OpenAI-compatible providers use the Chat Completions API (`.chat()`): the AI
 * SDK default (Responses API) is not available on every compatible endpoint.
 */
export function judgeModel(): LanguageModel {
  const config = judgeConfig();
  // Scenario uses AI SDK 6 (LanguageModel v3). Resolve its compatible OpenAI
  // provider, rather than the top-level AI SDK 7 provider (v4).
  const scenarioRequire = createRequire(require.resolve("@langwatch/scenario"));
  const { createOpenAI } = scenarioRequire("@ai-sdk/openai") as {
    createOpenAI: (settings: { apiKey: string; baseURL: string; headers: Record<string, string> }) => {
      chat: (model: string) => LanguageModel;
    };
  };
  const p = createOpenAI({
    apiKey: config.apiKey,
    baseURL: config.baseURL,
    headers: { "X-QA-Run-ID": process.env.GITHUB_RUN_ID || "scenario" },
  });
  const model = p.chat(config.model);
  if (typeof model !== "string" && model.specificationVersion !== "v3") {
    throw new Error("Scenario requires an AI SDK v3 DeepSeek provider");
  }
  return model;
}

export function parseStrictJudgePayload(raw: string, criteria: string[]): JudgeResult {
  const cleaned = raw.trim().replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "");
  const payload = JSON.parse(cleaned) as StrictJudgePayload;
  if (!Array.isArray(payload.results) || payload.results.length !== criteria.length) {
    throw new Error(`Cloud judge returned ${payload.results?.length ?? 0}/${criteria.length} criterion decisions`);
  }

  const byIndex = new Map<number, CriterionDecision>();
  for (const decision of payload.results) {
    if (!Number.isInteger(decision.index) || typeof decision.met !== "boolean") {
      throw new Error("Cloud judge returned an invalid criterion decision");
    }
    if (byIndex.has(decision.index)) throw new Error("Cloud judge returned a duplicate criterion index");
    byIndex.set(decision.index, decision);
  }

  const decisions = criteria.map((_, index) => {
    const decision = byIndex.get(index + 1);
    if (!decision) throw new Error(`Cloud judge omitted criterion ${index + 1}`);
    return decision;
  });
  const metCriteria = criteria.filter((_, index) => decisions[index].met);
  const unmetCriteria = criteria.filter((_, index) => !decisions[index].met);
  const details = decisions
    .map((decision) => `${decision.index}. ${decision.met ? "PASS" : "FAIL"}: ${decision.explanation || "No explanation"}`)
    .join("\n");

  return {
    success: unmetCriteria.length === 0,
    reasoning: `${payload.reasoning || "Cloud judge evaluation"}\n${details}`,
    metCriteria,
    unmetCriteria,
  };
}

export function strictCloudJudge(criteria: string[]): JudgeAgentAdapter {
  return {
    name: "strict-deepseek-judge",
    role: AgentRole.JUDGE,
    criteria,
    call: async (input: AgentInput) => {
      const config = judgeConfig();
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
              "X-QA-Run-ID": process.env.GITHUB_RUN_ID || "scenario",
            },
            body: JSON.stringify({
              model: config.model,
              messages: [
                { role: "system", content: "You are a strict QA evaluator. Follow the JSON contract exactly." },
                { role: "user", content: prompt },
              ],
              temperature: 0,
              max_tokens: 4096,
              reasoning_effort: "low",
              response_format: { type: "json_object" },
              stream: false,
            }),
          });
          if (!response.ok) throw new Error(`cloud judge HTTP ${response.status}: ${await response.text()}`);
          const data: any = await response.json();
          const content = data?.choices?.[0]?.message?.content;
          if (typeof content !== "string" || !content.trim()) throw new Error("Cloud judge returned no content");
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
  return Boolean(process.env.DEEPSEEK_API_KEY);
}
