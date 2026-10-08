#!/usr/bin/env python3
"""
auto_fix_agent.py — AI-powered auto-fix agent running in GitHub Actions.

Triggered by auto-fix.yml when any CI pipeline fails.
Workflow:
  1. Download failure logs from GitHub Actions API
  2. Find which test files are failing
  3. Call LLM (rotating through all configured providers) with logs + files
  4. Apply the suggested file patches
  5. Push a fix branch and open a PR (labelled `bug`)
  6. Dispatch the exact PR commit to the two-model OpenCode merge gate

The agent never merges directly. The separate merge gate requires deterministic
tests plus two independent OpenCode approvals; any rejection goes to human review.

LLM rotation lives in .github/scripts/llm_client.py — same provider list
as eval/rotating_llm.py. When one provider returns 401/403/429 the next
one is tried automatically, so a single rotated key doesn't break the
whole pipeline.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Local import — llm_client.py sits next to this file in .github/scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client  # noqa: E402

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
REPO      = os.environ.get("REPO", "")
RUN_ID    = os.environ.get("FAILED_RUN_ID", "")
RUN_URL   = os.environ.get("FAILED_RUN_URL", "")
PIPELINE  = os.environ.get("PIPELINE", "CI")
HEAD_SHA  = os.environ.get("HEAD_SHA", "")
GH_TOKEN  = os.environ.get("GITHUB_TOKEN", "")

# Slack notification — uses the bot token via chat.postMessage (no webhook).
# PR-review pings go to the dedicated #ci-pr-review channel
# (PR_REVIEW_CHANNEL_ID); fall back to the bug-reports channel if it isn't set
# so we never silently lose a notification.
SLACK_TOKEN   = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = (os.environ.get("PR_REVIEW_CHANNEL_ID", "")
                 or os.environ.get("BUG_REPORTS_CHANNEL_ID", ""))

# Detailed Investigation / Fix / Resolution reports go to #qa-agent-reports.
# Fall back to the PR-review/bug channel so a report is never silently lost.
AGENT_REPORTS_CHANNEL = (os.environ.get("AGENT_REPORTS_CHANNEL_ID", "")
                         or SLACK_CHANNEL)
HUMAN_REVIEW_CHANNEL = (os.environ.get("HUMAN_REVIEW_CHANNEL_ID", "")
                        or AGENT_REPORTS_CHANNEL)

# Pipelines where we must NOT auto-patch code. RAG / eval-quality failures have
# their root cause in the RAG app, the knowledge base, or the eval dataset —
# none of which the agent is allowed to edit. For these we investigate, post a
# detailed report, and open a tracking issue instead of a PR.
REPORT_ONLY_KEYWORDS = ("ragas", "rag eval", "eval nightly", "giskard", "i-copilot")

MAX_LOG_CHARS  = 18000
MAX_FILE_CHARS = 12000
AGENT_LLM_TIMEOUT = int(os.environ.get("AGENT_FIX_LLM_TIMEOUT_SEC", "480"))


def is_report_only(pipeline: str) -> bool:
    """True for RAG/eval pipelines that should be investigated + reported but
    never auto-patched."""
    p = pipeline.lower()
    return any(k in p for k in REPORT_ONLY_KEYWORDS)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_request(path: str, accept: str = "application/vnd.github+json") -> bytes:
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        print(f"  GitHub API {path}: HTTP {e.code}", file=sys.stderr)
        return b""
    except Exception as e:
        print(f"  GitHub API {path}: {e}", file=sys.stderr)
        return b""


def get_workflow_logs() -> str:
    """Download zipped logs from the failed run and return concatenated text."""
    print("  Downloading workflow logs...")
    raw = gh_request(
        f"repos/{REPO}/actions/runs/{RUN_ID}/logs",
        accept="application/vnd.github+json",
    )
    if not raw:
        return ""
    zip_path = Path("/tmp/ci_logs.zip")
    log_dir  = Path("/tmp/ci_logs")
    zip_path.write_bytes(raw)
    log_dir.mkdir(exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(log_dir)
    except Exception as e:
        print(f"  Log zip extract error: {e}", file=sys.stderr)
        return ""

    parts: list[str] = []
    for log_file in sorted(log_dir.rglob("*.txt"))[:8]:
        text = log_file.read_text(errors="replace")
        # Keep only the last 3000 chars of each log (tail has the errors)
        parts.append(f"=== {log_file.name} ===\n{text[-3000:]}")

    combined = "\n\n".join(parts)
    return combined[:MAX_LOG_CHARS]


def get_job_logs_via_api() -> str:
    """Fallback: get individual job logs text via GitHub API."""
    data = gh_request(f"repos/{REPO}/actions/runs/{RUN_ID}/jobs")
    if not data:
        return ""
    jobs = json.loads(data).get("jobs", [])
    parts: list[str] = []
    for job in jobs:
        if job.get("conclusion") not in ("failure", "cancelled"):
            continue
        job_id  = job["id"]
        log_raw = gh_request(f"repos/{REPO}/actions/jobs/{job_id}/logs",
                              accept="application/vnd.github+json")
        if log_raw:
            parts.append(f"=== Job: {job['name']} ===\n{log_raw.decode(errors='replace')[-3000:]}")
    return "\n\n".join(parts)[:MAX_LOG_CHARS]


# ---------------------------------------------------------------------------
# Find failing test files
# ---------------------------------------------------------------------------

def find_failing_files(logs: str) -> list[str]:
    """Extract paths to test files mentioned in failure logs."""
    patterns = [
        r"(tests?/[\w/.-]+\.spec\.ts)",
        r"(tests?/[\w/.-]+\.spec\.js)",
        r"(tests?/[\w/.-]+_test\.py)",
        r"(tests?/[\w/.-]+\.py)",
        r"(e2e/[\w/.-]+\.spec\.ts)",
        r"FAILED\s+(tests?/[\w/.-]+)",
        r"● ([\w /.-]+?) ›",
    ]
    found: set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, logs):
            candidate = m.group(1).strip()
            if Path(candidate).exists():
                found.add(candidate)
    result = sorted(found)[:8]
    # If nothing found, fall back to all test files
    if not result:
        for glob in ["tests/**/*.spec.ts", "tests/**/*.py", "e2e/**/*.spec.ts"]:
            result += [str(p) for p in Path(".").glob(glob)][:3]
    return result[:8]


def read_files(paths: list[str]) -> str:
    parts: list[str] = []
    total = 0
    for p in paths:
        try:
            text = Path(p).read_text(errors="replace")
            chunk = f"### {p}\n```\n{text[:4000]}\n```"
            parts.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_CHARS:
                break
        except Exception:
            pass
    return "\n\n".join(parts)


def gather_eval_context() -> str:
    """For RAG/eval pipelines: collect the human-readable report + machine
    summary so the LLM can investigate which questions failed and why. Reads
    both the in-repo eval/results/ and any artifact dirs the workflow extracted
    from the failed run."""
    patterns = [
        "eval/results/report.md",
        "eval/results/summary.json",
        "eval/results/giskard_rag.json",
        "eval/results/giskard_scan.json",
        "**/report.md",
        "**/summary.json",
        "**/giskard_rag.json",
        "**/giskard_scan.json",
    ]
    seen: set[str] = set()
    parts: list[str] = []
    total = 0
    for pat in patterns:
        for p in sorted(Path(".").glob(pat)):
            sp = str(p)
            if sp in seen or not p.is_file():
                continue
            seen.add(sp)
            try:
                text = p.read_text(errors="replace")
            except Exception:
                continue
            chunk = f"### {sp}\n{text[:4000]}"
            parts.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_CHARS:
                return "\n\n".join(parts)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "edit_file",
        "description": (
            "Apply a targeted fix to a test file. "
            "Only call this when you are confident the fix is correct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Relative path to the file (e.g. tests/foo.spec.ts)"
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to be replaced (must exist verbatim in the file)"
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text"
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence: why this change fixes the failure"
                },
            },
            "required": ["file_path", "old_text", "new_text", "reason"],
        },
    },
    {
        "name": "give_up",
        "description": "Cannot determine a safe, confident fix. Use this instead of guessing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why no fix was possible"}
            },
            "required": ["reason"],
        },
    },
]

SYSTEM_PROMPT = """You are an expert QA engineer fixing failing automated tests in CI.

Rules:
- ONLY fix test code (*.spec.ts, *.spec.js, *_test.py, conftest.py). Never touch application code.
- Make the minimal change needed to fix the failure.
- Fix root causes: broken selectors, timing issues, changed API responses, wrong assertions.
- Do NOT add arbitrary waitForTimeout() calls or sleep() — fix the underlying cause.
- Use POM selectors where they exist; do not add raw page.locator() in spec bodies.
- If you are not confident in a fix, call give_up instead of guessing.
- Call edit_file once per logical change. You may call it multiple times for multiple files."""


def build_prompt(logs: str, files: str) -> str:
    return f"""Pipeline: {PIPELINE}
Failed run: {RUN_URL}

## CI failure logs (truncated to last ~3000 chars per job):
{logs}

## Test file contents:
{files}

Analyze the failure. If you can determine a confident, minimal fix, call edit_file.
If the failure requires application changes or you cannot determine the root cause safely, call give_up."""


def call_llm(logs: str, files: str) -> tuple[list[dict], str]:
    """Call the rotating LLM client with native tool-use support.

    Returns (tool_calls, provider_used). tool_calls items follow the existing
    {"name": str, "input": dict} shape used by apply_fixes().

    If the provider returned tool_calls natively, use them as-is. If it only
    returned text content (some weaker models do this even with tools=...),
    fall back to JSON-parsing a fenced array — same trick the old call_groq
    used. Either way, downstream code is unchanged.
    """
    providers = llm_client.configured_providers()
    if not providers:
        print("  No LLM providers configured (no API keys in env).",
              file=sys.stderr)
        return [], ""
    print(f"  Available providers: {', '.join(providers)}")

    result = llm_client.chat(
        messages=[{"role": "user", "content": build_prompt(logs, files)}],
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        max_tokens=4096,
        temperature=0.1,
        timeout=AGENT_LLM_TIMEOUT,
    )

    provider = result.get("provider", "")
    tool_calls = result.get("tool_calls", [])
    content = result.get("content", "")

    if tool_calls:
        print(f"  ✅ {provider} returned {len(tool_calls)} tool call(s)")
        return tool_calls, provider

    # Fallback: parse a JSON array out of the content (some models don't
    # honor tool_choice and just write the JSON instead).
    if content.strip():
        cleaned = re.sub(r"^```[a-z]*\n?", "", content.strip()).rstrip("` \n")
        try:
            fixes = json.loads(cleaned)
            if isinstance(fixes, list) and fixes:
                print(f"  ✅ {provider} returned {len(fixes)} fixes via "
                      f"text-JSON fallback")
                return [{"name": "edit_file", "input": f} for f in fixes], provider
        except json.JSONDecodeError:
            pass

    print(f"  All providers failed or returned no fix. Errors:")
    for e in result.get("errors", []):
        print(f"    {e}", file=sys.stderr)
    return [], ""


REPORT_SYSTEM_PROMPT = """You are a senior QA / LLM-evaluation engineer triaging a failed CI run.
Write a concise incident report grounded in concrete evidence from the logs
(test names, metrics, thresholds, error messages). Never invent details — if
the logs are inconclusive, say so.

Return ONLY a JSON object (no markdown fence, no prose around it) with keys:
- "investigation": 2-4 sentences. What failed and the most likely root cause,
  citing specific evidence.
- "fix": 2-4 sentences. For test-code failures, the concrete code change that
  resolves it. For RAG / eval-quality failures, the concrete fix you recommend
  (which dataset/threshold/file or which RAG-app behaviour) — note that test
  code cannot fix a genuine answer-quality regression.
- "severity": one of "low", "medium", "high"."""


def investigate(logs: str, context: str) -> dict:
    """Ask the LLM for a structured Investigation/Fix narrative. Returns a dict
    with keys investigation, fix, severity. Never raises — returns {} on fail."""
    prompt = f"""Pipeline: {PIPELINE}
Failed run: {RUN_URL}

## CI failure logs (truncated):
{logs}

## Additional context (test files / eval reports):
{context}

Produce the JSON incident report described in the system prompt."""
    try:
        result = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=REPORT_SYSTEM_PROMPT,
            max_tokens=4096,
            temperature=0.2,
            timeout=AGENT_LLM_TIMEOUT,
        )
    except Exception as e:
        print(f"  ⚠ investigate() LLM error: {e}", file=sys.stderr)
        return {}
    content = (result.get("content") or "").strip()
    if not content:
        return {}
    cleaned = re.sub(r"^```[a-z]*\n?", "", content).rstrip("` \n")
    # Some models wrap the JSON in prose — grab the first {...} block.
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        cleaned = m.group(0)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        print("  ⚠ investigate() returned non-JSON; using raw text",
              file=sys.stderr)
        return {"investigation": content[:600], "fix": "", "severity": "medium"}
    return {}


# ---------------------------------------------------------------------------
# Apply fixes
# ---------------------------------------------------------------------------

def apply_fixes(tool_uses: list[dict]) -> list[dict]:
    applied: list[dict] = []
    for tool in tool_uses:
        name = tool.get("name")
        inp  = tool.get("input", {})

        if name == "give_up":
            print(f"  Agent gave up: {inp.get('reason', '—')}")
            continue

        if name != "edit_file":
            continue

        path     = Path(inp.get("file_path", ""))
        old_text = inp.get("old_text", "")
        new_text = inp.get("new_text", "")
        reason   = inp.get("reason", "")

        if not path.exists():
            print(f"  ⚠ File not found: {path}", file=sys.stderr)
            continue

        content = path.read_text(errors="replace")
        if old_text not in content:
            print(f"  ⚠ old_text not found in {path} — skipping", file=sys.stderr)
            continue

        path.write_text(content.replace(old_text, new_text, 1))
        print(f"  ✅ {path}: {reason}")
        applied.append(inp)

    return applied


# ---------------------------------------------------------------------------
# Git + GitHub PR
# ---------------------------------------------------------------------------

def git(*args: str) -> str:
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if result.returncode != 0 and args[0] not in ("diff",):
        print(f"  git {' '.join(args)}: {result.stderr.strip()}", file=sys.stderr)
    return result.stdout.strip()


def slack_post(channel: str, text: str, label: str = "Slack") -> bool:
    """Best-effort chat.postMessage with the bot token. Never raises."""
    if not SLACK_TOKEN or not channel:
        print(f"  ({label} skipped — SLACK_BOT_TOKEN/channel not set)")
        return False
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        if resp.get("ok"):
            print(f"  💬 {label} sent")
            return True
        print(f"  ⚠ {label} error: {resp.get('error')}", file=sys.stderr)
    except Exception as e:
        print(f"  ⚠ {label} failed: {e}", file=sys.stderr)
    return False


def slack_notify(pr_url: str, fixes: list[dict]) -> None:
    """Short Slack ping when an automated fix PR is created."""
    changed = ", ".join(sorted({f["file_path"] for f in fixes})) or "test files"
    text = (f":robot_face: *Auto-fix PR sent to automated merge gates* — {PIPELINE}\n"
            f"Files: {changed}\n{pr_url}\n"
            "Merge requires deterministic tests and two independent OpenCode approvals.")
    slack_post(SLACK_CHANNEL, text, label="Slack PR notify")


_SEVERITY_EMOJI = {"high": ":rotating_light:", "medium": ":warning:",
                   "low": ":information_source:"}


def post_agent_report(report: dict, resolution: str) -> None:
    """Post the detailed Investigation / Fix / Resolution report to
    #qa-agent-reports. `report` comes from investigate(); `resolution` is set
    by the caller based on what actually happened (PR opened, issue opened, or
    no safe fix). Best-effort — never raises."""
    severity = (report.get("severity") or "medium").lower()
    emoji = _SEVERITY_EMOJI.get(severity, ":warning:")
    investigation = report.get("investigation") or "_(no analysis produced)_"
    fix = report.get("fix") or "_(no fix proposed)_"
    text = (
        f"{emoji} *Agent incident report — {PIPELINE}*  _(severity: {severity})_\n"
        f"*Failed run:* {RUN_URL}\n\n"
        f"*:mag: Investigation*\n{investigation}\n\n"
        f"*:wrench: Fix*\n{fix}\n\n"
        f"*:white_check_mark: Resolution*\n{resolution}"
    )
    slack_post(AGENT_REPORTS_CHANNEL, text, label="Agent report")


def open_tracking_issue(report: dict) -> str:
    """Open a GitHub issue capturing the investigation + recommended fix for a
    failure the agent must not auto-patch (RAG/eval) or couldn't fix safely.
    Returns the issue URL, or "" on failure. Best-effort."""
    severity = (report.get("severity") or "medium").lower()
    investigation = report.get("investigation") or "(no analysis produced)"
    fix = report.get("fix") or "(no fix proposed)"
    title = f"[auto-triage] {PIPELINE} failure — needs human review"
    body = f"""## 🤖 Agent triage report

**Failed CI run:** {RUN_URL}
**Head commit:** `{HEAD_SHA}`
**Severity:** {severity}

### 🔍 Investigation
{investigation}

### 🔧 Recommended fix
{fix}

---
*Opened automatically by the auto-fix agent. This failure was not auto-patched
(RAG/eval-quality root causes live in the app, knowledge base, or dataset, which
the agent must not edit, or no confident test-code fix was found). A human should
review and decide on the fix.*
"""
    create = subprocess.run(
        ["gh", "issue", "create", "--title", title, "--body", body],
        capture_output=True, text=True,
    )
    if create.returncode != 0:
        print(f"  ⚠ issue creation failed: {create.stderr.strip()}",
              file=sys.stderr)
        return ""
    issue_url = create.stdout.strip()
    print(f"  ✅ tracking issue created: {issue_url}")
    # Best-effort label — a missing label must never fail the run.
    lbl = subprocess.run(
        ["gh", "issue", "edit", issue_url, "--add-label", "bug"],
        capture_output=True, text=True,
    )
    if lbl.returncode != 0:
        print(f"  (issue label not applied: {lbl.stderr.strip()})",
              file=sys.stderr)
    return issue_url


def create_pr(fixes: list[dict]) -> str:
    """Push the fix branch and open a PR. Returns the PR URL, or "" on failure."""
    slug      = re.sub(r"[^a-z0-9]+", "-", PIPELINE.lower())[:25].strip("-")
    short_sha = (HEAD_SHA or "unknown")[:8]
    branch    = f"fix/{slug}-{short_sha}"

    git("checkout", "-b", branch)
    git("add", "-A")

    # Commit message
    files_changed = sorted({f["file_path"] for f in fixes})
    fix_lines = "\n".join(f"- {f['file_path']}: {f['reason']}" for f in fixes)
    commit_msg = (
        f"fix(tests): auto-fix {PIPELINE} failures\n\n"
        f"Failing CI run: {RUN_URL}\n"
        f"Files changed: {', '.join(files_changed)}"
    )
    git("commit", "-m", commit_msg)
    git("push", "origin", branch)

    # PR body
    pr_body = f"""## 🤖 Auto-fix: {PIPELINE}

**Failing CI run:** {RUN_URL}
**Head commit:** `{HEAD_SHA}`

### Changes applied
{fix_lines}

### Review checklist
- [ ] Tests pass locally (`npx playwright test` or `pytest`)
- [ ] No new `waitForTimeout` / `sleep` calls added
- [ ] Change is minimal and targeted to the failure

---
*This PR was created automatically by the auto-fix agent. Review carefully before merging.*
"""

    # Create the PR with only the flags that can't fail. A bad --reviewer or a
    # non-existent --label makes `gh pr create` exit non-zero AFTER pushing the
    # branch, which orphans it with no PR. Metadata is applied best-effort below.
    create = subprocess.run(
        [
            "gh", "pr", "create",
            "--title", f"fix: auto-fix {PIPELINE} ({short_sha})",
            "--body", pr_body,
            "--head", branch,
            "--base", "master",
        ],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        print(f"  ⚠ PR creation failed: {create.stderr.strip()}", file=sys.stderr)
        return ""
    pr_url = create.stdout.strip()
    print(f"  ✅ PR created: {pr_url}")

    # Ping the human in Slack — GitHub won't, since the bot authored the PR.
    slack_notify(pr_url, fixes)

    # Best-effort metadata — a missing label/reviewer must never orphan the PR.
    # Reviewer is opt-in via PR_REVIEWER env (must be a real repo collaborator).
    reviewer = os.environ.get("PR_REVIEWER", "").strip()
    label = subprocess.run(
        ["gh", "pr", "edit", pr_url, "--add-label", "bug"],
        capture_output=True, text=True,
    )
    if label.returncode != 0:
        print(f"  (label not applied: {label.stderr.strip()})", file=sys.stderr)
    if reviewer:
        rev = subprocess.run(
            ["gh", "pr", "edit", pr_url, "--add-reviewer", reviewer],
            capture_output=True, text=True,
        )
        if rev.returncode != 0:
            print(f"  (reviewer not added: {rev.stderr.strip()})", file=sys.stderr)

    return pr_url


def dispatch_merge_gate(pr_url: str) -> bool:
    """Send the exact fix commit to the independent review-and-merge gate."""
    if not pr_url:
        return False

    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    review_sha = git("rev-parse", "HEAD").strip()
    rerun_workflow = {
        "LLM Quality (LLM Judge tests)": "llm-quality.yml",
        "Playwright CI (deterministic tests)": "playwright-ci.yml",
    }.get(PIPELINE, "")
    command = [
        "gh", "workflow", "run", "agent-observability.yml",
        "--repo", REPO,
        "--ref", "master",
        "-f", f"review_sha={review_sha}",
        "-f", f"review_repo={REPO}",
        "-f", "base_ref=master",
        "-f", f"auto_merge_pr={pr_number}",
        "-f", f"rerun_workflow={rerun_workflow}",
    ]
    dispatched = subprocess.run(command, capture_output=True, text=True)
    if dispatched.returncode != 0:
        print(f"  ⚠ merge-gate dispatch failed: {dispatched.stderr.strip()}",
              file=sys.stderr)
        slack_post(
            HUMAN_REVIEW_CHANNEL,
            (f":warning: *Human review required — merge gate did not start*\n"
             f"Pipeline: {PIPELINE}\nPR: {pr_url}\nFailed run: {RUN_URL}"),
            label="Human review",
        )
        return False
    print(f"  ✅ two-model merge gate dispatched for {review_sha[:12]}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{'='*55}")
    print(f"Auto-fix Agent: {PIPELINE}")
    print(f"{'='*55}")

    if not GH_TOKEN:
        print("No GITHUB_TOKEN — aborting", file=sys.stderr)
        sys.exit(1)

    if not llm_client.configured_providers():
        print("OpenCode is not configured for agent-fix "
              "(OPENCODE_API_KEY is missing or OpenCode is disabled)",
              file=sys.stderr)
        sys.exit(1)

    # 1. Get logs
    print("\n[1] Fetching failure logs...")
    logs = get_workflow_logs()
    if not logs:
        print("  Zip download failed — trying per-job API...")
        logs = get_job_logs_via_api()
    if not logs:
        # Try to read from downloaded artifact
        for p in Path(".").rglob("*.json"):
            try:
                data = p.read_text(errors="replace")
                if any(k in data for k in ("unexpected", "FAILED", "error")):
                    logs += data[:3000]
            except Exception:
                pass

    if not logs:
        print("  No logs available — cannot triage. Exiting.")
        sys.exit(0)

    print(f"  Got {len(logs)} chars of logs")

    # -----------------------------------------------------------------
    # REPORT-ONLY path: RAG / eval-quality pipelines. We never auto-patch
    # (the root cause lives in the app / KB / dataset, which the agent must
    # not edit). Instead: investigate, open a tracking issue, post a detailed
    # Investigation / Fix / Resolution report to #qa-agent-reports.
    # -----------------------------------------------------------------
    if is_report_only(PIPELINE):
        print(f"\n[2] Report-only pipeline ({PIPELINE}) — investigating...")
        context = gather_eval_context()
        report = investigate(logs, context)
        if not report:
            report = {"investigation": "Automated analysis was inconclusive — "
                                       "see the failed run logs.",
                      "fix": "", "severity": "medium"}
        print("\n[3] Opening tracking issue...")
        issue_url = open_tracking_issue(report)
        if issue_url:
            resolution = (f"No safe auto-fix — RAG/eval-quality regressions need "
                          f"a human decision. Opened tracking issue for review: "
                          f"{issue_url}")
        else:
            resolution = ("No safe auto-fix — RAG/eval-quality regressions need a "
                          "human decision. (Tracking-issue creation failed; see "
                          "the failed run logs.)")
        print("\n[4] Posting detailed report to #qa-agent-reports...")
        post_agent_report(report, resolution)
        print("\nDone.")
        return

    # -----------------------------------------------------------------
    # PATCH path: test-code pipelines (Playwright, LLM-judge). Try to fix,
    # open a PR, and always post a detailed report (even when no fix is found).
    # -----------------------------------------------------------------
    # 2. Find failing files
    print("\n[2] Identifying failing test files...")
    failing = find_failing_files(logs)
    print(f"  Found: {failing or '(none — will scan all tests)'}")
    file_content = read_files(failing)

    # 3. Call LLM (rotates through all configured providers)
    print("\n[3] Calling LLM for fix analysis...")
    tool_uses, provider = call_llm(logs, file_content)
    print(f"  Fix author: {provider or '(none)'}")

    # 4. Apply fixes
    applied: list[dict] = []
    if tool_uses:
        print("\n[4] Applying fixes...")
        applied = apply_fixes(tool_uses)

    has_changes = bool(applied) and bool(git("diff", "--name-only", "HEAD"))

    # 5. Open a PR if we have real changes
    pr_url = ""
    if has_changes:
        print("\n[5] Creating PR...")
        pr_url = create_pr(applied)

    # 6. Always post a detailed Investigation / Fix / Resolution report.
    print("\n[6] Building detailed report...")
    report = investigate(logs, file_content)
    if not report:
        report = {"investigation": "Automated analysis was inconclusive — "
                                   "see the failed run logs.",
                  "fix": "", "severity": "medium"}
    # Overlay the concrete fixes we actually applied, so the report's "Fix"
    # section reflects reality rather than only the LLM's narrative.
    if applied:
        applied_lines = "\n".join(
            f"• `{f['file_path']}` — {f['reason']}" for f in applied)
        report["fix"] = (report.get("fix", "").strip() + "\n\n*Applied changes:*\n"
                         + applied_lines).strip()

    if pr_url:
        gate_dispatched = dispatch_merge_gate(pr_url)
        if gate_dispatched:
            resolution = (f"Auto-fix PR opened and sent to deterministic tests plus "
                          f"two independent OpenCode reviewers: {pr_url}")
        else:
            resolution = (f"Auto-fix PR opened, but the automated merge gate could "
                          f"not start — needs human review: {pr_url}")
    elif applied and not has_changes:
        resolution = "Proposed edits produced no net change — needs human review."
    else:
        # No confident fix — open a tracking issue so it isn't lost.
        issue_url = open_tracking_issue(report)
        resolution = (f"No confident auto-fix found. Opened tracking issue: {issue_url}"
                      if issue_url else
                      "No confident auto-fix found — needs human review (see logs).")

    post_agent_report(report, resolution)
    print("\nDone.")


if __name__ == "__main__":
    main()
