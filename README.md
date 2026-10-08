# llm-eval-alexpavsky

LLM evaluation, AI safety testing and end-to-end quality coverage for the live
[alexpavsky.com](https://www.alexpavsky.com) assistant and site.

The application source of truth lives in the separate site repo:

- [qa-apps/alexpavsky](https://github.com/qa-apps/alexpavsky)

## What is here

| Area | Path | Stack |
| --- | --- | --- |
| LLM-as-a-Judge | `utils/llm-judges.ts`, `tests/llm-judge/` | Custom judges wired into Playwright |
| Prompt-level evaluation | `promptfooconfig.yaml`, `promptfooconfig.basic.yaml` | Promptfoo |
| RAG evaluation | `eval/ragas_eval.py`, `eval/giskard_rag.py`, `tests/rag/golden_questions.json` | Ragas, Giskard, a golden question set |
| Automated red teaming | `eval/giskard_scan.py`, `tests/llmRedTeaming.spec.ts` | Giskard scan |
| Security | `tests/securityVulnerability.spec.ts`, `security-ssrf-xss.spec.ts`, `apiTokenSecurity.spec.ts`, `security-daily.spec.ts` | Playwright |
| Voice AI | `tests/voice/` | Vitest, LangWatch Scenario, a DeepSeek cloud judge |
| Tracing | `tests/observability/langfuse-agent-tracking.test.ts` | Langfuse |
| Deterministic checks | `tests/regex/pattern-llm.spec.ts` | Regex assertions over model output |
| E2E, API and UI | 32 specs at `tests/` | Playwright |
| Performance | `performance/site.js` | k6 |

## Custom LLM judges

`utils/llm-judges.ts` exposes `safety`, `promptLeak`, `jailbreak`, `relevance`,
`tone`, and `piiProtection`. They are callable from inside a Playwright spec, so
a semantic verdict can be asserted in the same test that drove the UI, instead
of living in a separate offline evaluation.

`utils/article-quality-judge.ts`, `utils/model-registry.ts`, and
`utils/verdict-reporter.ts` provide content scoring, provider rotation, and
verdict reporting around them.

## Promptfoo

Config-driven evaluations against the chatbot assistant covering safety,
jailbreak resistance, prompt-leak protection, tone, and relevance.

```bash
npm run eval          # full daily suite
npm run eval:basic    # 10 essential probes
npm run eval:view
```

Promptfoo is pinned to `deepseek:deepseek-v4-pro`. Set `DEEPSEEK_API_KEY` before
running evaluations. `npm run providers:refresh` validates both pinned configs;
`npm run providers:ping` checks the live judge with one small request.

## RAG and red-team evaluation

`eval/` holds the Python side, pinned in `eval/requirements.txt`
(Ragas 0.2.10, Giskard 2.19.2, LangChain, sentence-transformers):

- `ragas_eval.py` — faithfulness and groundedness over `tests/rag/golden_questions.json`
- `giskard_rag.py` — RAG-specific Giskard evaluation
- `giskard_scan.py` — automated vulnerability scan of the assistant
- `rotating_llm.py` — provider rotation for the judge model

## Commands

```bash
npm test                  # full Playwright suite
npm run test:smoke        # smoke only
npm run test:security     # security specs
npm run test:voice        # voice AI (Vitest)
npm run test:observability
npm run test:agents
npm run performance       # k6
```

## GitHub Actions CI

Fourteen workflows gate the repo. The main ones:

| Workflow | What it gates |
| --- | --- |
| `playwright-ci.yml` | E2E on push, PR, and nightly at 10:00 PM New York |
| `llm-quality.yml` | LLM quality gate |
| `promptfoo-basic.yml` | Promptfoo essential probes |
| `ragas-nightly.yml` | Nightly RAG groundedness |
| `agent-observability.yml` | Langfuse agent tracing |
| `agentic-vision-audit.yml` | Agentic visual audit |
| `k6-performance.yml` | Performance thresholds |
| `weekly-qa-report.yml` | Weekly report |
| `auto-fix.yml` | Agent-assisted failure triage |

Required repository secret for Slack notifications:

- `SLACK_WEBHOOK_URL`

Repository secrets for live LLM judge runs:

- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY`

Nightly schedules use two UTC cron entries and a New York time gate so they stay
aligned across DST changes.

## Playwright MCP

This project includes `@playwright/mcp` so the browser can be attached to an AI
orchestrator or agent client for live DOM inspection.

```bash
npm run mcp:playwright
```

Example Codex MCP config:

```toml
[mcp_servers.playwright]
command = "npx"
args = ["playwright-mcp", "--headless", "--browser", "chrome", "--output-dir", ".playwright-mcp", "--save-trace", "--save-session"]
```

The repo also carries agent definitions used for test generation, healing,
planning, triage and weekly reporting (`playwright-test-*.agent.md`,
`auto-fix.agent.md`).

## Coverage areas

- Hero ticker and navigation
- Live feed filters and article modal
- Essential Principles and tooling sections
- AI Lab tools and challenge playground
- AI assistant UI and LLM-as-a-judge checks
- YouTube/video carousel
- Responsive navigation and newsletter digest flow
- Auth flows, forum chains, and cross-device journeys
