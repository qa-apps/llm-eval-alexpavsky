#!/usr/bin/env python3
"""Build a static GitHub Pages UI for Playwright CI results."""
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
REPORT = ROOT / "playwright-report"
RESULTS = ROOT / "test-results" / "results-pure.json"
RUN_NUMBER = os.environ.get("RUN_NUMBER", "local")
RUN_ID = os.environ.get("RUN_ID", "")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "")[:7]
REPO_SLUG = os.environ.get("REPO_SLUG", "qa-apps/llm-eval-alexpavsky")
TIMESTAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_counts() -> tuple[int, int]:
    try:
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
    except Exception:
        return 0, 0
    suites = data.get("suites") or []
    passed = failed = 0

    def walk(nodes: list) -> None:
        nonlocal passed, failed
        for node in nodes:
            for spec in node.get("specs") or []:
                for test in spec.get("tests") or []:
                    results = test.get("results") or []
                    status = (results[-1].get("status") if results else test.get("status")) or ""
                    if status in {"passed", "expected", "skipped"}:
                        if status != "skipped":
                            passed += 1
                    else:
                        failed += 1
            walk(node.get("suites") or [])

    walk(suites)
    if passed == 0 and failed == 0:
        stats = data.get("stats") or {}
        passed = int(stats.get("expected") or stats.get("passed") or 0)
        failed = int(stats.get("unexpected") or stats.get("failed") or 0)
    return passed, failed


def carry_forward() -> list[dict]:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)
    (SITE / ".nojekyll").write_text("")
    history: list[dict] = []
    src = None
    if (EXISTING / "playwright").is_dir():
        src = EXISTING / "playwright"
    elif EXISTING.is_dir():
        src = EXISTING
    if src is not None:
        dest = SITE / "playwright"
        shutil.copytree(src, dest, dirs_exist_ok=True)
        hist = dest / "history.json"
        if hist.exists():
            try:
                history = json.loads(hist.read_text(encoding="utf-8"))
            except Exception:
                history = []
    return history


def write_index(history: list[dict]) -> None:
    rows = []
    for item in history[:30]:
        run = escape(str(item.get("run", "")))
        rows.append(
            "<tr>"
            f"<td><a href='runs/{run}/index.html'>#{run}</a></td>"
            f"<td>{escape(str(item.get('timestamp', '')))}</td>"
            f"<td>{escape(str(item.get('passed', '')))}</td>"
            f"<td>{escape(str(item.get('failed', '')))}</td>"
            f"<td>{escape(str(item.get('commit', '')))}</td>"
            "</tr>"
        )
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Playwright Dashboard</title>
<style>body{{font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;margin:0 auto;max-width:1000px;padding:24px;color:#1f2328}}a{{color:#0969da;text-decoration:none}}table{{width:100%;border-collapse:collapse;margin-top:16px}}th,td{{padding:8px 10px;border-bottom:1px solid #d8dee4;text-align:left}}th{{background:#f6f8fa}}</style>
</head><body><h1>Playwright Dashboard</h1><p>Deterministic Playwright CI runs with passed and failed test lists.</p>
<table><thead><tr><th>Run</th><th>Timestamp UTC</th><th>Passed</th><th>Failed</th><th>Commit</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>
"""
    dest = SITE / "playwright"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(html, encoding="utf-8")
    (dest / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")


def main() -> None:
    history = carry_forward()
    passed, failed = load_counts()
    run_dir = SITE / "playwright" / "runs" / str(RUN_NUMBER)
    run_dir.mkdir(parents=True, exist_ok=True)
    if REPORT.exists():
        shutil.copytree(REPORT, run_dir, dirs_exist_ok=True)
    else:
        (run_dir / "index.html").write_text(
            f"<!doctype html><title>Playwright #{escape(str(RUN_NUMBER))}</title>"
            f"<p>No HTML report for run {escape(str(RUN_NUMBER))} "
            f"(<a href='https://github.com/{escape(REPO_SLUG)}/actions/runs/{escape(str(RUN_ID))}'>GitHub Actions</a>).</p>",
            encoding="utf-8",
        )
    entry = {
        "run": str(RUN_NUMBER),
        "run_id": str(RUN_ID),
        "timestamp": TIMESTAMP,
        "passed": f"{passed}/{passed + failed}" if (passed or failed) else "n/a",
        "failed": failed,
        "commit": COMMIT_SHA,
    }
    history = [item for item in history if str(item.get("run")) != str(RUN_NUMBER)]
    history.insert(0, entry)
    write_index(history)
    print(f"wrote playwright site for run {RUN_NUMBER} passed={passed} failed={failed}")


if __name__ == "__main__":
    main()
