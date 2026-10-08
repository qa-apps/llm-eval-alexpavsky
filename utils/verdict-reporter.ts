import * as fs from 'fs';
import * as path from 'path';
import { test } from '@playwright/test';
import type { JudgeConfig, JudgeVerdict } from './llm-judges';

const REPORT_DIR = path.join(process.cwd(), 'test-results', 'judge-verdicts');
const MARKER_FILE = path.join(REPORT_DIR, '.current-report');
// One JSON object per judged answer. scripts/build_llm_judge_site.py joins
// these to the Playwright JSON results so every test in the Pages report
// expands to prompt -> chatbot answer -> judge score and explanation.
export const VERDICTS_JSONL = path.join(REPORT_DIR, 'verdicts.jsonl');

export interface VerdictRecord {
  /** Title path without the file name, e.g. ["Describe", "test title"]. */
  titlePath: string[];
  testFile: string;
  retry: number;
  judgeName: string;
  judge: string;
  judgeModel?: string;
  criteria: string[];
  score: number;
  maxScore: number;
  passingScore: number;
  passed: boolean;
  /** What was sent to the system under test (or judged directly). */
  prompt: string;
  /** What the system under test answered; empty when the judge graded `prompt` itself. */
  response: string;
  reasoning: string;
  timestamp: string;
}

function ensureDir(dir: string) {
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

function getReportPath(): string {
  ensureDir(REPORT_DIR);
  if (fs.existsSync(MARKER_FILE)) {
    return fs.readFileSync(MARKER_FILE, 'utf-8').trim();
  }
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const p = path.join(REPORT_DIR, `verdict-report-${ts}.md`);
  fs.writeFileSync(MARKER_FILE, p);
  fs.writeFileSync(p, `# LLM Judge Verdict Report\n\nGenerated: ${new Date().toISOString()}\n\n---\n\n**Scoring Scale:** 1-5\n\n---\n\n`);
  return p;
}

function currentTest(): { titlePath: string[]; testFile: string; retry: number } {
  try {
    const info = test.info();
    return {
      titlePath: info.titlePath.slice(1),
      testFile: path.relative(process.cwd(), info.file),
      retry: info.retry,
    };
  } catch {
    // Called outside a running Playwright test.
    return { titlePath: [], testFile: '', retry: 0 };
  }
}

/** Append one structured verdict for the current test to verdicts.jsonl. */
export function recordJudgeVerdict(
  verdict: Omit<VerdictRecord, 'titlePath' | 'testFile' | 'retry' | 'timestamp'> & { titlePath?: string[] },
): void {
  const ctx = currentTest();
  const record: VerdictRecord = {
    ...verdict,
    titlePath: ctx.titlePath.length ? ctx.titlePath : verdict.titlePath || [],
    testFile: ctx.testFile,
    retry: ctx.retry,
    response: verdict.response.slice(0, 20_000),
    timestamp: new Date().toISOString(),
  };
  ensureDir(REPORT_DIR);
  fs.appendFileSync(VERDICTS_JSONL, `${JSON.stringify(record)}\n`);
}

export function writeVerdictReport(
  judgeName: string,
  cfg: JudgeConfig,
  prompt: string,
  response: string,
  verdict: JudgeVerdict,
  testTitle: string
): void {
  recordJudgeVerdict({
    titlePath: [testTitle],
    judgeName,
    judge: cfg.name,
    judgeModel: verdict.judgeModel,
    criteria: cfg.criteria,
    score: verdict.score,
    maxScore: 5,
    passingScore: Number(process.env.LLM_JUDGE_PASSING_SCORE || 3),
    passed: verdict.passed,
    prompt,
    response,
    reasoning: verdict.reasoning,
  });

  const reportPath = getReportPath();
  const section = `
## ${testTitle}

**Judge:** ${cfg.name} (${judgeName})
**Model:** ${verdict.judgeModel || 'unknown'}
**Score:** ${verdict.score}/5
**Result:** ${verdict.passed ? 'PASS' : 'FAIL'}

**Criteria:** ${cfg.criteria.join('; ')}

### Prompt
\`\`\`
${prompt}
\`\`\`

### Response
\`\`\`
${response.slice(0, 2000)}${response.length > 2000 ? '... (truncated)' : ''}
\`\`\`

### Reasoning
${verdict.reasoning}

---

`;
  fs.appendFileSync(reportPath, section);
}
