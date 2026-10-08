# Ragas Evaluation Report

_Generated: 2026-10-08T16:57:46.927680+00:00_


## Summary

| Metric | Value | Threshold | Status |
|---|---|---|---|
| Average faithfulness    | 1.000 | ≥ 0.50 | PASS |
| Average answer_relevancy | 0.718 | ≥ 0.35 | PASS |
| Keyword check pass rate | 70.0% | ≥ 60% | PASS |
| Questions evaluated     | 10 | — | — |
| Judge model             | `deepseek/deepseek-v4-pro (+0 fallbacks)` | — | — |

## Per-Question Results

| ID | Category | Faithfulness | Relevancy | Keywords | Answer (preview) |
|---|---|---|---|---|---|
| finnova-01-agent-count | facts | 1.00 | 0.87 | PASS | FinNova Bank’s QA transformation deployed **seven (7) specialized AI agents**. 【Context: “The engage... |
| finnova-02-bug-reduction | metrics | 1.00 | 0.32 | PASS | 67%... |
| finnova-03-cost-savings | metrics | 1.00 | 0.71 | PASS | FinNova saved $2.4 million annually in operating costs after implementing the QA transformation with... |
| finnova-04-mttd | metrics | 1.00 | 0.33 | FAIL | Before: 4 hours 30 minutes   After: 12 minutes... |
| finnova-05-test-cases | facts | 1.00 | 1.00 | PASS | The Test Generator agent produced 3,247 automated test cases.... |
| finnova-06-flake-rate | metrics | 1.00 | 0.00 | FAIL | The provided context does not give specific numbers for the flake rate before and after the engageme... |
| finnova-07-customers | facts | 1.00 | 1.00 | PASS | FinNova Bank serves 2.4 million retail customers.... |
| finnova-08-transaction-volume | facts | 1.00 | 0.96 | FAIL | FinNova Bank handles approximately $48 billion in annual transaction volume.... |
| concept-01-test-isolation | best-practices | 1.00 | 1.00 | PASS | The best practices for test data isolation in Playwright include:  1. **Dynamic API-driven seeding**... |
| concept-02-prompt-injection-types | best-practices | 1.00 | 1.00 | PASS | Based on the provided context, the main categories of prompt injection attacks that QA should test f... |