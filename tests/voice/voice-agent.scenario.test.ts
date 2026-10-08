import { describe, it, expect } from "vitest";
import scenario from "@langwatch/scenario";
import { voiceAgent, SAY_URL } from "../support/voiceAgentAdapter";
import { judgeModel, hasJudgeModel, strictCloudJudge } from "../support/scenarioModel";

/**
 * LangWatch Scenario check for Alex Pavlovsky's AI voice assistant.
 *
 * This is a behavioural check of the DEPLOYED voice agent (the orb widget's
 * brain on alexpavsky.com), driven by LangWatch Scenario:
 *   - a scripted user turn poses a real QA/testing question,
 *   - the live voice agent answers,
 *   - an LLM judge scores the answer against explicit criteria.
 *
 * Scope is intentionally the *main* voice functionality — does the assistant
 * understand a QA question and give a relevant, on-topic spoken answer — not the
 * full ASR/latency/accent/noise matrix (those need a separate audio harness).
 *
 * Set LANGWATCH_API_KEY to also stream results to the LangWatch dashboard;
 * without it the simulation + judge still run fully locally.
 */

const describeVoice = hasJudgeModel() ? describe : describe.skip;

const criteria = [
  "The assistant understands that the user is asking a QA / test-automation question.",
  "The assistant gives a relevant, on-topic answer about QA / testing and does NOT wrongly refuse it as out of scope.",
  "The answer is coherent and phrased for speech (concise, plain sentences, no markdown or code blocks).",
];

if (!hasJudgeModel()) {
  // eslint-disable-next-line no-console
  console.warn(
    "[voice scenario] skipped: no evaluator LLM key " +
      "(set DEEPSEEK_API_KEY).",
  );
}

describeVoice("voice assistant — LangWatch Scenario", () => {
  it("understands a QA question and gives a relevant spoken answer", async () => {
    const model = judgeModel();

    const result = await scenario.run({
      name: "voice assistant answers a QA/testing question",
      description:
        `A visitor talks to Alex Pavlovsky's voice assistant at ${SAY_URL}. ` +
        "The assistant is a QA and AI-testing helper built on Alex's knowledge base.",
      agents: [
        voiceAgent(),
        scenario.userSimulatorAgent({ model }),
        strictCloudJudge(criteria),
      ],
      script: [
        scenario.user(
          "What is Playwright and when should I use it for test automation?",
        ),
        scenario.agent(),
        scenario.judge(),
      ],
    });

    if (!result.success) {
      // eslint-disable-next-line no-console
      console.error("[voice scenario] verdict:", JSON.stringify(result, null, 2));
    }
    expect(result.metCriteria).toHaveLength(criteria.length);
    expect(result.unmetCriteria).toHaveLength(0);
    expect(result.success).toBe(true);
  }, 120_000);
});
