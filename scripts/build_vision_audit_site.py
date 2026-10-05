#!/usr/bin/env python3
"""Build a static, evidence-first UI for Agentic Vision Audit runs."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("VISION_AUDIT_SOURCE", ROOT / "vision-audit"))
EXISTING = Path(os.environ.get("VISION_AUDIT_EXISTING", ROOT / "gh-pages-existing"))
SITE = Path(os.environ.get("VISION_AUDIT_SITE", ROOT / "gh-pages-site"))
RUN_NUMBER = os.environ.get("RUN_NUMBER", "local")
RUN_ID = os.environ.get("RUN_ID", "")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "")[:7]
REPO_SLUG = os.environ.get("REPO_SLUG", "qa-apps/PW_alexpavsky")
TIMESTAMP = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def h(value: object) -> str:
    return escape("" if value is None else str(value))


def carry_forward() -> list[dict]:
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / ".nojekyll").write_text("")
    history: list[dict] = []
    if EXISTING.exists():
        for item in EXISTING.iterdir():
            if item.name == ".git":
                continue
            destination = SITE / item.name
            if item.is_dir():
                shutil.copytree(item, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(item, destination)
        history_path = SITE / "vision-audit" / "history.json"
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                history = []
    return history


def finding_list(findings: list[dict]) -> str:
    if not findings:
        return '<p class="empty">None.</p>'
    items = []
    for finding in findings:
        severity = h(str(finding.get("severity", "unknown")).upper())
        items.append(
            '<li><span class="severity">'
            f'{severity}</span><strong>{h(finding.get("title", "Issue"))}</strong>'
            f'<p>{h(finding.get("evidence", ""))}</p></li>'
        )
    return f'<ul class="findings">{"".join(items)}</ul>'


def browser_evidence(evidence: dict) -> str:
    problems: list[str] = []
    for check in evidence.get("deterministic_checks") or []:
        problems.append(f"<li><strong>Passed:</strong> {h(check)}</li>")
    for label, key in (
        ("Console", "console_errors"),
        ("Page", "page_errors"),
        ("Server", "server_errors"),
    ):
        for value in evidence.get(key) or []:
            problems.append(f"<li><strong>{label}:</strong> {h(value)}</li>")
    if not problems:
        problems.append("<li>No browser, page, or HTTP 5xx errors captured.</li>")
    return f'<ul class="evidence">{"".join(problems)}</ul>'


def step_html(step: dict) -> str:
    number = int(step.get("step", 0))
    screenshot_name = Path(str(step.get("screenshot", ""))).name
    screenshot = f"screenshots/{screenshot_name}" if screenshot_name else ""
    decision = step.get("decision") or {}
    action_result = step.get("action_result") or {}
    verdict = str(step.get("verdict", "unknown"))
    verdict_class = "pass" if verdict == "passed" else "fail"
    actual_action = action_result.get("action") or action_result.get("rejected") or "not executed"
    scenario_input = step.get("scenario_input") or "No text input required."
    scenario_output = step.get("scenario_output") or "The browser assertions and screenshot are the output."
    candidate_findings = step.get("candidate_findings") or []
    confirmed_findings = step.get("confirmed_findings") or []
    return f"""
    <article class="test-case" id="test-{number}">
      <header>
        <div><span class="case-id">{h(step.get('test_case_id', f'VISION-{number:03d}'))}</span>
        <h2>{h(step.get('test_case_name') or step.get('title') or step.get('url') or 'Untitled page')}</h2></div>
        <span class="verdict {verdict_class}">{h(verdict.upper())}</span>
      </header>
      <p class="url"><a href="{h(step.get('url'))}">{h(step.get('url'))}</a></p>
      <div class="case-grid">
        <div class="shot">{f'<a href="{h(screenshot)}"><img src="{h(screenshot)}" alt="Screenshot for {h(step.get("test_case_id"))}"></a>' if screenshot else '<p>No screenshot produced.</p>'}</div>
        <div class="facts">
          <h3>Test case</h3>
          <dl>
            <dt>Type</dt><dd>{h(step.get('test_case_type', 'dynamic exploratory check'))}</dd>
            <dt>Objective</dt><dd>{h(step.get('objective'))}</dd>
            <dt>Expected</dt><dd>{h(step.get('expected_result'))}</dd>
            <dt>Actual</dt><dd>{h(step.get('actual_result'))}</dd>
            <dt>Input</dt><dd><pre>{h(scenario_input)}</pre></dd>
            <dt>Output</dt><dd><pre>{h(scenario_output)}</pre></dd>
          </dl>
          <h3>Local Vision review</h3>
          <p>{h(step.get('summary'))}</p>
          <dl>
            <dt>Execution</dt><dd><code>{h(actual_action)}</code></dd>
            <dt>Latency</dt><dd>{h(step.get('model_latency_ms', 0))} ms</dd>
          </dl>
        </div>
      </div>
      <div class="details-grid">
        <section><h3>Browser evidence</h3>{browser_evidence(step.get('browser_evidence') or {})}</section>
        <section><h3>Raw observations ({len(candidate_findings)})</h3>{finding_list(candidate_findings)}</section>
        <section><h3>Confirmed defects ({len(confirmed_findings)})</h3>{finding_list(confirmed_findings)}</section>
      </div>
    </article>"""


def run_html(report: dict) -> str:
    status = str(report.get("status", "unknown"))
    status_class = "pass" if status == "passed" else "fail"
    provenance = report.get("model_provenance") or {}
    usage = report.get("model_usage") or {}
    steps = report.get("steps") or []
    video_name = Path(str(report.get("video", ""))).name
    video = f"videos/{video_name}" if video_name else ""
    run_url = f"https://github.com/{REPO_SLUG}/actions/runs/{RUN_ID}" if RUN_ID else ""
    test_cases = "".join(step_html(step) for step in steps) or '<p class="empty">No test cases were produced.</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlexPavsky Daily Audit #{h(RUN_NUMBER)}</title>
<style>
:root{{--ink:#1f2328;--muted:#59636e;--line:#d0d7de;--soft:#f6f8fa;--green:#116329;--green-bg:#dafbe1;--red:#a40e26;--red-bg:#ffebe9;--blue:#0969da;--gold:#7d4e00}}
*{{box-sizing:border-box}} body{{margin:0;color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#fff}}
main{{max-width:1240px;margin:0 auto;padding:28px 22px 72px}} a{{color:var(--blue);text-decoration:none}} a:hover{{text-decoration:underline}}
.top{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;border-bottom:1px solid var(--line);padding-bottom:20px}} h1{{font-size:28px;margin:4px 0 6px;letter-spacing:0}} h2{{font-size:18px;margin:4px 0;letter-spacing:0}} h3{{font-size:14px;margin:18px 0 7px;letter-spacing:0}}
.meta,.url,.empty{{color:var(--muted)}} .status,.verdict{{font-weight:700;border-radius:6px;padding:6px 10px;white-space:nowrap}} .pass{{color:var(--green);background:var(--green-bg)}} .fail{{color:var(--red);background:var(--red-bg)}}
.proof{{margin:20px 0;padding:14px 16px;border-left:4px solid var(--green);background:#f0fff4}} .proof strong{{color:var(--green)}}
.metrics{{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:10px;margin:18px 0 26px}} .metric{{border:1px solid var(--line);border-radius:6px;padding:12px}} .metric b{{display:block;font-size:20px}} .metric span{{color:var(--muted)}}
.test-case{{border-top:3px solid #24292f;padding:18px 0 28px;margin-top:24px}} .test-case header{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}} .case-id{{font:12px ui-monospace,SFMono-Regular,monospace;color:var(--gold)}}
.case-grid{{display:grid;grid-template-columns:minmax(360px,1.15fr) minmax(320px,.85fr);gap:24px;align-items:start}} .shot img{{display:block;width:100%;border:1px solid var(--line);border-radius:6px;background:var(--soft)}}
dl{{display:grid;grid-template-columns:110px 1fr;gap:7px 12px;margin:0}} dt{{font-weight:600;color:var(--muted)}} dd{{margin:0;min-width:0;overflow-wrap:anywhere}} code,pre{{font:12px/1.5 ui-monospace,SFMono-Regular,monospace;background:var(--soft);padding:2px 4px;border-radius:4px}} pre{{white-space:pre-wrap;margin:0;padding:8px;max-height:240px;overflow:auto}}
.details-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}} .details-grid section{{border-top:1px solid var(--line)}} ul{{padding-left:20px}} .findings li{{margin-bottom:10px}} .findings p{{margin:3px 0;color:var(--muted)}} .severity{{font:11px ui-monospace,SFMono-Regular,monospace;color:var(--red);margin-right:7px}}
video{{width:100%;max-width:900px;border:1px solid var(--line);border-radius:6px;background:#000}}
@media(max-width:820px){{.top,.test-case header{{align-items:flex-start}}.metrics{{grid-template-columns:repeat(2,1fr)}}.case-grid,.details-grid{{grid-template-columns:1fr}}.case-grid{{gap:12px}}}}
</style></head><body><main>
<p><a href="../../index.html">All Daily Audit runs</a></p>
<div class="top"><div><h1>AlexPavsky Daily Audit #{h(RUN_NUMBER)}</h1><p class="meta">{h(TIMESTAMP)} UTC · commit {h(COMMIT_SHA or 'unknown')} · {f'<a href="{h(run_url)}">GitHub run</a>' if run_url else 'local build'}</p></div><span class="status {status_class}">{h(status.upper())}</span></div>
<div class="proof"><strong>Audit evaluator: {h(provenance.get('execution', 'unknown'))}.</strong> Provider: {h(provenance.get('provider', 'unknown'))}; endpoint: <code>{h(provenance.get('endpoint', 'unknown'))}</code>; model: <code>{h(provenance.get('model', report.get('model', 'unknown')))}</code>; cloud evaluator calls: <strong>{h(provenance.get('cloud_llm_calls', 0))}</strong>. AI Chat, Voice, and Challenge are production systems under test and may use their own configured providers.</div>
<div class="metrics"><div class="metric"><b>{len(steps)}</b><span>documented test cases</span></div><div class="metric"><b>{h(usage.get('calls', 0))}</b><span>Vision calls</span></div><div class="metric"><b>{h(usage.get('prompt_tokens', 0))}</b><span>input tokens</span></div><div class="metric"><b>{h(usage.get('completion_tokens', 0))}</b><span>output tokens</span></div><div class="metric"><b>{len(report.get('confirmed_findings') or [])}</b><span>confirmed defects</span></div></div>
<p>The run executes ten required UI openings plus live AI Chat, Voice Agent, and Challenge journeys. Every case records its screenshot, objective, expected result, exact input and output, deterministic browser checks, Vision review, and calibrated verdict.</p>
{test_cases}
<section><h2>Full session recording</h2>{f'<video controls preload="metadata" src="{h(video)}"></video>' if video else '<p class="empty">No video produced.</p>'}</section>
<p class="meta"><a href="vision-audit-report.json">Raw JSON decision trail</a></p>
</main></body></html>"""


def index_html(history: list[dict]) -> str:
    def sort_key(record: dict) -> int:
        try:
            return int(record.get("run_number", 0))
        except (TypeError, ValueError):
            return 0

    rows = []
    for record in sorted(history, key=sort_key, reverse=True)[:50]:
        status = str(record.get("status", "unknown"))
        rows.append(
            f'<tr><td><a href="runs/{h(record.get("run_number"))}/index.html">#{h(record.get("run_number"))}</a></td>'
            f'<td><span class="{h(status)}">{h(status.upper())}</span></td><td>{h(record.get("timestamp", ""))}</td>'
            f'<td>{h(record.get("test_cases", 0))}</td><td>{h(record.get("confirmed_findings", 0))}</td>'
            f'<td>{h(record.get("model", ""))}</td><td>{h(record.get("commit", ""))}</td></tr>'
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AlexPavsky Daily Audit</title>
<style>body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0 auto;max-width:1050px;padding:28px 20px;color:#1f2328}}h1{{font-size:28px;letter-spacing:0}}a{{color:#0969da;text-decoration:none}}table{{width:100%;border-collapse:collapse;margin-top:18px}}th,td{{padding:9px 10px;border-bottom:1px solid #d0d7de;text-align:left}}th{{background:#f6f8fa}}.passed{{color:#116329;font-weight:700}}.failed{{color:#a40e26;font-weight:700}}@media(max-width:760px){{table{{font-size:12px}}th,td{{padding:7px 5px}}}}</style></head>
<body><h1>AlexPavsky Daily Audit</h1><p>Daily visual and functional checks with recorded model provenance.</p><table><thead><tr><th>Run</th><th>Status</th><th>UTC</th><th>Cases</th><th>Defects</th><th>Model</th><th>Commit</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="7">No runs yet.</td></tr>'}</tbody></table></body></html>"""


def main() -> None:
    report_path = SOURCE / "vision-audit-report.json"
    if not report_path.exists():
        raise SystemExit(f"Missing report: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    history = carry_forward()
    audit_root = SITE / "vision-audit"
    run_dir = audit_root / "runs" / str(RUN_NUMBER)
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, run_dir, dirs_exist_ok=True)
    (run_dir / "index.html").write_text(run_html(report), encoding="utf-8")

    record = {
        "run_number": RUN_NUMBER,
        "run_id": RUN_ID,
        "timestamp": TIMESTAMP,
        "status": report.get("status", "unknown"),
        "test_cases": len(report.get("steps") or []),
        "confirmed_findings": len(report.get("confirmed_findings") or []),
        "model": report.get("model", "unknown"),
        "commit": COMMIT_SHA,
    }
    history = [item for item in history if str(item.get("run_number")) != str(RUN_NUMBER)]
    history.append(record)
    audit_root.mkdir(parents=True, exist_ok=True)
    (audit_root / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    (audit_root / "index.html").write_text(index_html(history), encoding="utf-8")


if __name__ == "__main__":
    main()
