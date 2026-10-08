# Ragas Evaluation Report

_Generated: 2026-10-08T16:38:10.360457+00:00_


## Summary

| Metric | Value | Threshold | Status |
|---|---|---|---|
| Average faithfulness    | 0.892 | ≥ 0.50 | PASS |
| Average answer_relevancy | 0.719 | ≥ 0.35 | PASS |
| Keyword check pass rate | 90.0% | ≥ 60% | PASS |
| Questions evaluated     | 10 | — | — |
| Judge model             | `deepseek/deepseek-v4-pro (+0 fallbacks)` | — | — |

## Per-Question Results

| ID | Category | Faithfulness | Relevancy | Keywords | Answer (preview) |
|---|---|---|---|---|---|
| finnova-01-agent-count | facts | 1.00 | 0.86 | PASS | FinNova Bank’s QA transformation deployed **seven specialized AI agents**.... |
| finnova-02-bug-reduction | metrics | 1.00 | 0.39 | PASS | 67%... |
| finnova-03-cost-savings | metrics | 1.00 | 0.34 | PASS | $2.4 million... |
| finnova-04-mttd | metrics | 1.00 | 0.90 | PASS | According to the provided context, the mean time to detection (MTTD) before the engagement was 4 hou... |
| finnova-05-test-cases | facts | 1.00 | 0.89 | PASS | According to the provided context, Agent 1 — Test Generator: Generated 3,247 automated test cases ov... |
| finnova-06-flake-rate | metrics | 0.00 | 0.00 | FAIL | I couldn't find the flake rate before and after the engagement in the provided context.... |
| finnova-07-customers | facts | 1.00 | 1.00 | PASS | FinNova Bank serves 2.4 million retail customers.... |
| finnova-08-transaction-volume | facts | 1.00 | 1.00 | PASS | According to the provided context, the annual transaction volume of FinNova Bank is approximately $4... |
| concept-01-test-isolation | best-practices | 1.00 | 1.00 | PASS | Based on the provided context, the best practices for test data isolation in Playwright include:  1.... |
| concept-02-prompt-injection-types | best-practices | 0.92 | 0.80 | PASS | **Main prompt‑injection categories that a QA team should cover**  \| Category \| What the attacker doe... |