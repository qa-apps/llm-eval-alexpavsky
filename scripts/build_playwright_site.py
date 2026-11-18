#!/usr/bin/env python3
"""Build a static GitHub Pages UI for Playwright CI results.

The Playwright HTML reporter is a SPA that shows "No tests found" on GitHub
Pages. This builder writes a plain HTML dashboard from results-pure.json.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXISTING = ROOT / "gh-pages-existing"
SITE = ROOT / "gh-pages-site"
RESULTS = ROOT / "test-results" / "results-pure.json"
RUN_NUMBER = os.environ.get("RUN_NUMBER", "local")
RUN_ID = os.environ.get("RUN_ID", "")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "")[:7]
REPO_SLUG = os.environ.get("REPO_SLUG", "qa-apps/llm-eval-alexpavsky")
TIMESTAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
STYLE = (
    "body{font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;"
    "margin:0 auto;max-width:1000px;padding:24px;color:#1f2328}"
    "a{color:#0969da;text-decoration:none}"
    "table{width:100%;border-collapse:collapse;margin-top:16px}"
    "th,td{padding:8px 10px;border-bottom:1px solid #d8dee4;text-align:left}"
    "th{background:#f6f8fa}"
    ".pass{color:#1a7f37}.fail{color:#cf222e}"
    ".meta{color:#656d76}"
)


def iter_tests(suites: list) -> list[dict]:
    out: list[dict] = []

    def walk(nodes: list, file_hint: str = "") -> None:
        for node in nodes or []:
            file_name = node.get("file") or file_hint
            for spec in node.get("specs") or []:
                title = spec.get("title") or "test"
                file_name = spec.get("file") or file_name
                for test in spec.get("tests") or []:
                    results = test.get("results") or []
                    last = results[-1] if results else {}
                    status = last.get("status") or test.get("status") or "unknown"
                    out.append({
                        "title": title,
                        "file": file_name,
                        "status": status,
                        "ok": status in {"passed", "expected", "skipped"},
                    })
            walk(node.get("suites") or [], file_name)

    walk(suites)
    return out


def load_results() -> tuple[list[dict], dict]:
    try:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    except Exception:
        return [], {}
    tests = iter_tests(data.get("suites") or [])
    stats = data.get("stats") or {}
    return tests, stats


def carry_forward() -> list[dict]:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)
    (SITE / ".nojekyll").write_text("")
    history: list[dict] = []
    src = EXISTING / "playwright" if (EXISTING / "playwright").is_dir() else None
    if src is not None:
        dest = SITE / "playwright"
        shutil.copytree(src, dest, dirs_exist_ok=True)
        hist = dest / "history.json"
        if hist.exists():
            try:
                raw = json.loads(hist.read_text(encoding="utf-8"))
            except Exception:
                raw = []
            history = [item for item in raw if str(item.get("run") or "").strip()]
    return history


def write_run(tests: list[dict], stats: dict) -> tuple[int, int]:
    passed = sum(1 for t in tests if t["ok"])
    failed = sum(1 for t in tests if not t["ok"])
    if not tests:
        passed = int(stats.get("expected") or 0)
        failed = int(stats.get("unexpected") or 0)
    rows = []
    for t in tests:
        mark = "PASS" if t["ok"] else "FAIL"
        cls = "pass" if t["ok"] else "fail"
        rows.append(
            "<tr>"
            f"<td class='{cls}'>{mark}</td>"
            f"<td>{escape(str(t['file']))}</td>"
            f"<td>{escape(str(t['title']))}</td>"
            f"<td>{escape(str(t['status']))}</td>"
            "</tr>"
        )
    run_url = f"https://github.com/{REPO_SLUG}/actions/runs/{RUN_ID}" if RUN_ID else ""
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Playwright run #{escape(str(RUN_NUMBER))}</title>
<style>{STYLE}</style></head><body>
<p><a href="../../index.html">&larr; all Playwright runs</a></p>
<h1>Playwright CI #{escape(str(RUN_NUMBER))}</h1>
<p class="meta">{escape(TIMESTAMP)} UTC · commit {escape(COMMIT_SHA)}
{" · <a href='" + escape(run_url) + "'>GitHub run</a>" if run_url else ""}</p>
<p><span class="pass">Passed {passed}</span> · <span class="fail">Failed {failed}</span></p>
<table><thead><tr><th>Status</th><th>File</th><th>Test</th><th>Result</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan='4'>No tests in results-pure.json</td></tr>"}</tbody></table>
</body></html>
"""
    dest = SITE / "playwright" / "runs" / str(RUN_NUMBER)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(html, encoding="utf-8")
    return passed, failed


def write_index(history: list[dict]) -> None:
    rows = []
    for item in history[:30]:
        run = str(item.get("run") or "").strip()
        if not run:
            continue
        rows.append(
            "<tr>"
            f"<td><a href='runs/{escape(run)}/index.html'>#{escape(run)}</a></td>"
            f"<td>{escape(str(item.get('timestamp', '')))}</td>"
            f"<td>{escape(str(item.get('passed', '')))}</td>"
            f"<td>{escape(str(item.get('failed', '')))}</td>"
            f"<td>{escape(str(item.get('commit', '')))}</td>"
            "</tr>"
        )
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Playwright Dashboard</title>
<style>{STYLE}</style></head><body>
<h1>Playwright Dashboard</h1>
<p>Deterministic Playwright CI runs with passed and failed test lists.</p>
<table><thead><tr><th>Run</th><th>Timestamp UTC</th><th>Passed</th><th>Failed</th><th>Commit</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>
"""
    dest = SITE / "playwright"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(html, encoding="utf-8")
    (dest / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")


def main() -> None:
    history = carry_forward()
    tests, stats = load_results()
    passed, failed = write_run(tests, stats)
    entry = {
        "run": str(RUN_NUMBER),
        "run_id": str(RUN_ID),
        "timestamp": TIMESTAMP,
        "passed": f"{passed}/{passed + failed}" if (passed or failed) else "n/a",
        "failed": failed,
        "commit": COMMIT_SHA,
    }
    history = [item for item in history if str(item.get("run") or "").strip() != str(RUN_NUMBER)]
    history.insert(0, entry)
    write_index(history)
    print(f"wrote playwright site for run {RUN_NUMBER} passed={passed} failed={failed}")


if __name__ == "__main__":
    main()
