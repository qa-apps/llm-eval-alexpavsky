# PW_alexpavsky — Claude Code instructions

End-to-end Playwright suite for the live site **https://www.alexpavsky.com**.
The site source lives in a **separate** repo: [`qa-apps/alexpavsky`](https://github.com/qa-apps/alexpavsky).
This repo contains tests only — never edit application code from here.

## Stack

- **Playwright** (`@playwright/test`) — TypeScript, single `chromium` project, `baseURL = https://www.alexpavsky.com`
- **Promptfoo** — standalone LLM evals for the site's chatbot (`promptfooconfig.yaml`, judges in `utils/llm-judges.ts`)
- **k6** — performance scenarios in `performance/site.js`
- **Python eval tools** in `eval/` — Giskard, RAGAS, rotating-LLM helpers (separate `requirements.txt`)
- **Playwright MCP** (`@playwright/mcp`) — agent-driven DOM inspection

## Layout

```
tests/         specs (+ smoke/, llm-judge/, rag/, regex/ subfolders)
pages/         Page Object Model classes (HomePage, AuthPage, ChatbotPage, …)
utils/         fixtures.ts, llm-judges.ts, verdict-reporter.ts, model-registry.ts
performance/   k6 scripts
eval/          Python-based LLM eval scripts (Giskard, RAGAS)
scripts/       pinned provider validation, build_eval_site.py
judge-verdicts/  generated LLM judge verdict reports (do not hand-edit)
```

## Common commands

```bash
npm test                          # full suite
npm run test:smoke                # smoke only, workers=1
npm run test:chromium             # explicit chromium project
npm run test:debug                # Playwright inspector
npm run test:ui                   # tests/loginDashboard.spec.ts (single-worker)
npm run test:security             # securityVulnerability.spec.ts (single-worker)
npm run report                    # open last HTML report
npm run performance               # k6 default scenario
npm run eval                      # promptfoo eval, no cache, concurrency=1
npm run eval:view                 # promptfoo viewer
npm run providers:ping            # check LLM provider keys are live
```

## Running a single test

```bash
npx playwright test tests/auth.spec.ts            # by file
npx playwright test -g "requires auth"            # by test name pattern
npx playwright test tests/auth.spec.ts --debug    # with inspector
npx playwright test tests/auth.spec.ts --headed   # see the browser
npm run test:smoke -- -g "footer"                 # smoke + name filter
```

## Flaky test triage

- Replace `waitForTimeout(N)` with `locator.waitFor({ state: 'visible' })` or a state assertion (`expect(locator).toBeVisible()`).
- Avoid `waitForLoadState('networkidle')` on this site — the chat widget and analytics keep network alive and the wait can hang. Use `'domcontentloaded'` + element waits instead.
- Current hotspots: `tests/live-rail.spec.ts` (~11 `waitForTimeout` calls) and `tests/smoke/smoke-ui.spec.ts`. Fix these before adding similar patterns elsewhere.
- For intermittent CI failures, check the trace via `npm run report` and look for `recordings/.last-run.json` timing data.

## POM rules

- Repeating selectors belong in `pages/`, never inline in specs. Repeated offenders today: `#liveHandle`, `#liveRail`, `#chat-toggle-btn`.
- Missing POM classes worth creating when touched: `LiveRailPage`, `FeedPage`, `NoveltyBarPage`.
- New specs must import a POM from `pages/` — no fresh `page.locator()` chains in test bodies.

## CI: what blocks merges, what doesn't

- `.github/workflows/playwright-ci.yml` — deterministic tests, **blocks PR merges**.
- `.github/workflows/llm-quality.yml` — essential LLM-judge tests on GitHub-hosted runners with DeepSeek; failures are reported after artifacts and notifications are published.
- Split is by name: deterministic = everything not matching `grep "LLM Judge|Content quality"`.
- If a deterministic test goes flaky, prefer disabling the assertion locally and opening an issue over moving it to llm-quality.

## Generated artifacts — don't hand-edit

- `judge-verdicts/<run-id>/*.json` — written by `npm run eval` and CI
- `test-results/`, `playwright-report/`, `recordings/.last-run.json`
- `.playwright-report-temp/` — CI scratch space

Stale reports mislead future runs — let the tooling regenerate them.

## Local `.env` — what's actually required

For UI / security / feed / smoke specs: no keys needed.
For `npm run eval` and LLM-judge specs: `DEEPSEEK_API_KEY`.
For Slack notifications from CI only: `SLACK_WEBHOOK_URL`.
Run `npm run providers:ping` to verify keys before a judge run.

## Conventions

- Add new tests under `tests/` matching existing topic split (chatbot, feed, security, lab-tools, mobile-responsive, …). Use POM classes from `pages/` instead of raw selectors in specs.
- Single chromium project — don't add browsers/devices without asking.
- Tests run against **production**. No staging URL. Don't write specs that mutate site state or hit rate-limited endpoints aggressively.
- Promptfoo runs with `--max-concurrency 2` against DeepSeek cloud.
- Judge verdicts in `judge-verdicts/` are generated artifacts — they may be committed, but Claude should not author them by hand.
- CI runs on push to `master`, PRs into `master`, manual dispatch, and nightly at 10pm New York time (two UTC crons + NY gate for DST).

## Secrets

`.env` is loaded by `playwright.config.ts` via `dotenv`. Live LLM judge runs use `DEEPSEEK_API_KEY`; vision audit uses `GEMINI_API_KEY`. Slack notifications need `SLACK_WEBHOOK_URL`. Never commit `.env` or echo key values.

## When changing application behavior

If a test fails because the site changed, fix the test here. If the site itself is broken, the fix belongs in [`qa-apps/alexpavsky`](https://github.com/qa-apps/alexpavsky) — flag it, don't try to patch it from this repo.


## Agent fix workflow — MANDATORY

**Never push directly to `master`.** Every agent fix goes through a PR — but the PR is
gated by CI, not by waiting for a person. `master` requires the `playwright` check to
pass and does not require an approving review, so a fix that is green merges itself and
a fix that is red cannot merge at all.

When you identify and fix a failing test or CI issue, follow this exact sequence:

### 1. Create a fix branch
```bash
git checkout master && git pull origin master
git checkout -b fix/<short-kebab-description>
# e.g. fix/retry-rag-api, fix/flaky-live-rail-timeout
```

### 2. Make your changes and commit
```bash
git add -A
git commit -m "fix(<scope>): <what was fixed>

Failing test: <test name or CI workflow>
Root cause: <1 sentence>
Fix: <1 sentence>
CI run: <GITHUB_RUN_URL>"
```

### 3. Push the branch
```bash
git push origin fix/<branch-name>
```

### 4. Open a PR
```bash
gh pr create \
  --title "fix: <description>" \
  --body "..." \
  --reviewer alexpavsky \
  --label bug-fix
```

### 5. Merge it yourself once CI is green

Do not stop and wait. A fix that sits in an open PR is not a fix.

```bash
gh pr merge --squash --auto --delete-branch
```

`--auto` queues the merge and GitHub completes it the moment the required check
passes, so this is safe to run immediately after opening the PR. Branch protection
is the gate: if `playwright` fails, nothing merges.

### 5a. Escalate instead of merging — the exceptions

For the changes below, green CI is **not** sufficient evidence. Open the PR, do
**not** enable auto-merge, and say plainly in the PR body why it needs a human:

- **Weakened verification.** Deleting or skipping a test, loosening an assertion,
  raising a timeout, or lowering a pass threshold. A suite made quieter passes CI
  by definition, so CI cannot be the judge of it.
- **Auth, sessions, tokens, or anything reading a secret.**
- **Production deploy, server config, or systemd units.**
- **Data or schema migrations**, and anything that deletes stored data.
- **Major dependency bumps**, or adding a new runtime dependency.
- **A root cause you could not identify.** Open the PR, describe what you tried,
  and leave it for review — that is a legitimate outcome, unlike silence.

### Important rules
- One PR per bug fix — do not bundle unrelated changes
- PR title must start with `fix:` or `test:` or `ci:`
- If the fix is for a RAGAS / LLM eval issue, add label `rag-eval`
- Never fix a failing test by making it assert less. That is the one failure mode
  this whole pipeline exists to catch, and it is on the escalation list above.
