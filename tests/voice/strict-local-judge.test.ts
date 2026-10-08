import { describe, expect, it } from "vitest";
import { parseStrictJudgePayload } from "../support/scenarioModel";

const criteria = ["understands the question", "answers clearly", "uses plain speech"];

describe("strict cloud judge contract", () => {
  it("derives success only when every criterion passes", () => {
    const result = parseStrictJudgePayload(
      JSON.stringify({
        results: criteria.map((_, index) => ({
          index: index + 1,
          met: true,
          explanation: "satisfied",
        })),
        reasoning: "All requirements are satisfied.",
      }),
      criteria,
    );

    expect(result.success).toBe(true);
    expect(result.metCriteria).toEqual(criteria);
    expect(result.unmetCriteria).toEqual([]);
  });

  it("rejects an incomplete criterion list", () => {
    expect(() =>
      parseStrictJudgePayload(
        '{"results":[{"index":1,"met":true,"explanation":"ok"}],"reasoning":"incomplete"}',
        criteria,
      ),
    ).toThrow("1/3 criterion decisions");
  });
});
