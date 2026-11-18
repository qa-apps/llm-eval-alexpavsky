# Ragas Evaluation Report

_Generated: 2026-11-18T09:44:35.500103+00:00_


## Summary

| Metric | Value | Threshold | Status |
|---|---|---|---|
| Average faithfulness    | 1.000 | ≥ 0.50 | PASS |
| Average answer_relevancy | 0.816 | ≥ 0.35 | PASS |
| Keyword check pass rate | 90.0% | ≥ 60% | PASS |
| Questions evaluated     | 10 | — | — |
| Judge model             | `opencode/glm-5.3-flash (+1 fallbacks)` | — | — |

## Per-Question Results

| ID | Category | Faithfulness | Relevancy | Keywords | Answer (preview) |
|---|---|---|---|---|---|
| finnova-01-agent-count | facts | 1.00 | 0.91 | PASS | FinNova Bank’s transformation deployed **seven specialized AI agents** as part of the solution.【sour... |
| finnova-02-bug-reduction | metrics | 1.00 | 0.56 | PASS | Production P1/P2 bugs per month dropped from 18.4 to 6.1, which is a 67% reduction.... |
| finnova-03-cost-savings | metrics | 1.00 | 0.71 | PASS | FinNova saved $2.4 million annually in operating costs after implementing the QA transformation with... |
| finnova-04-mttd | metrics | 1.00 | 1.00 | PASS | Mean time to detection (MTTD) before the engagement was 4 hours 30 minutes, and after the engagement... |
| finnova-05-test-cases | facts | 1.00 | 1.00 | PASS | The Test Generator agent produced 3,247 automated test cases.... |
| finnova-06-flake-rate | metrics | 1.00 | 0.00 | FAIL | The provided context does not contain any information about the flake rate before or after the engag... |
| finnova-07-customers | facts | 1.00 | 1.00 | PASS | FinNova Bank serves 2.4 million retail customers.... |
| finnova-08-transaction-volume | facts | 1.00 | 0.97 | PASS | FinNova Bank handles approximately $48 billion in annual transaction volume.... |
| concept-01-test-isolation | best-practices | 1.00 | 1.00 | PASS | Based on the provided context, the best practices for test data isolation in Playwright include:  1.... |
| concept-02-prompt-injection-types | best-practices | 1.00 | 1.00 | PASS | Based on the provided context, the main categories of prompt injection attacks that QA should test f... |