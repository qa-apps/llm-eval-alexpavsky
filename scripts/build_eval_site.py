#!/usr/bin/env python3
"""
build_eval_site.py — Build the GitHub Pages site for nightly eval reports.

Reads eval/results/* from the current run, merges with any existing
history.json on the gh-pages branch, and writes a static site to
./gh-pages-site/ ready for peaceiris/actions-gh-pages to publish.

Ragas and Giskard get separate pages and separate run lists.

Output structure:
  gh-pages-site/
    .nojekyll
    index.html              — Ragas runs table + faithfulness trend chart
    giskard.html            — Giskard runs table
    history.json            — accumulating list of all past runs
    runs/<run_number>/
      index.html            — Ragas report: per-question PASS/FAIL, scores, answers
      giskard.html          — Giskard report: per-question PASS/FAIL, judge reasons
      report.md             — Ragas markdown
      ragas_cases.json      — Ragas per-question results
      giskard_rag_cases.json — Giskard per-question results
      giskard_rag.html      — Giskard RAG eval (self-contained)
      giskard_scan.html     — Giskard vulnerability scan (self-contained)
      metrics.json          — extracted summary numbers for the index

Environment:
  RUN_NUMBER       — GitHub Actions run_number (defaults to "local")
  RUN_ID           — GitHub Actions run_id (link target)
  COMMIT_SHA       — git SHA at run time
  REPO_SLUG        — "owner/repo" for run link
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
SITE = ROOT / "gh-pages-site"
EXISTING = ROOT / "gh-pages-existing"  # checked out gh-pages, if present

RUN_NUMBER = os.environ.get("RUN_NUMBER", "local")
RUN_ID = os.environ.get("RUN_ID", "")
COMMIT_SHA = os.environ.get("COMMIT_SHA", "")[:7]
REPO_SLUG = os.environ.get("REPO_SLUG", "qa-apps/PW_alexpavsky")
TIMESTAMP = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

STYLE = """
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, sans-serif; margin: 0 auto; max-width: 1100px; padding: 24px; color: #1a1a1a; }
  h1 { margin: 0 0 4px; } h2 { margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }
  nav { font-size: 13px; color: #666; } nav a, a { color: #0366d6; text-decoration: none; }
  .meta, .muted { color: #666; font-size: 13px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 16px 0; }
  .card { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 12px 14px; }
  .card .label { color: #666; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
  .status-pass { color: #0a7c0a; } .status-fail { color: #b00020; }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eaeaea; vertical-align: top; }
  th { background: #f6f8fa; font-weight: 600; }
  iframe { width: 100%; height: 600px; border: 1px solid #d0d7de; border-radius: 6px; }
  #ragas-md { background: #fff; border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; }
  .tools button { font: inherit; margin-right: 6px; padding: 4px 10px; border: 1px solid #d0d7de; border-radius: 6px; background: #fff; cursor: pointer; }
  details.case { border: 1px solid #d0d7de; border-radius: 6px; margin: 8px 0; background: #fff; }
  details.case > summary { cursor: pointer; list-style: none; display: flex; gap: 10px; align-items: baseline; padding: 10px 12px; }
  details.case > summary::-webkit-details-marker { display: none; }
  details.case > summary::before { content: '▸'; color: #666; } details.case[open] > summary::before { content: '▾'; }
  details.case[open] > summary { border-bottom: 1px solid #eaeaea; background: #f6f8fa; }
  .q { flex: 1; } .nums { color: #666; white-space: nowrap; }
  .body { padding: 4px 14px 12px; } h4 { margin: 12px 0 4px; font-size: 13px; }
  .badge { display: inline-block; min-width: 54px; text-align: center; border-radius: 10px; padding: 1px 8px; font-size: 12px; font-weight: 700; color: #fff; }
  .badge.pass { background: #1a7f37; } .badge.fail { background: #cf222e; } .badge.na { background: #6e7781; }
  pre { white-space: pre-wrap; word-break: break-word; background: #f6f8fa; border: 1px solid #eaeaea; border-radius: 6px; padding: 8px 10px; margin: 0; max-height: 420px; overflow: auto; }
  .reason { margin: 0; background: #fff8c5; border: 1px solid #eac54f; border-radius: 6px; padding: 8px 10px; }
"""

TOOLS = (
    "<p class='tools'>"
    "<button onclick=\"document.querySelectorAll('details.case').forEach(d=>d.open=true)\">Expand all</button>"
    "<button onclick=\"document.querySelectorAll('details.case').forEach(d=>d.open=false)\">Collapse all</button></p>"
)


def extract_ragas_summary(md_path: Path) -> dict:
    """Pull faithfulness / relevancy / status out of the Ragas markdown."""
    out: dict = {"avg_faithfulness": None, "avg_relevancy": None, "status": "unknown"}
    if not md_path.exists():
        return out
    text = md_path.read_text(encoding="utf-8", errors="ignore")

    def grab(label: str) -> float | None:
        m = re.search(rf"Average\s+{label}.*?\|\s*([0-9.]+)", text, re.IGNORECASE)
        return float(m.group(1)) if m else None

    out["avg_faithfulness"] = grab("faithfulness")
    out["avg_relevancy"] = grab("(?:answer_relevancy|relevancy)")
    if "FAIL" in text.upper():
        out["status"] = "fail"
    elif out["avg_faithfulness"] is not None:
        out["status"] = "pass"
    return out


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _fmt(value, digits: int = 2) -> str:
    if value is None or value == "—":
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return escape(str(value))


def _pre(text) -> str:
    text = str(text or "")
    text = re.sub(r"<br\s*/?>", "\n", text)
    return f"<pre>{escape(text) if text.strip() else '(empty response)'}</pre>"


def _badge(passed) -> str:
    if passed is True:
        return "<span class='badge pass'>PASS</span>"
    if passed is False:
        return "<span class='badge fail'>FAIL</span>"
    return "<span class='badge na'>N/A</span>"


def _meta_line(metrics: dict) -> str:
    sha = str(metrics.get("commit") or "")
    run_id = str(metrics.get("run_id") or "")
    number = metrics.get("run_number", RUN_NUMBER)
    commit = (
        f'<a href="https://github.com/{REPO_SLUG}/commit/{escape(sha)}">{escape(sha)}</a>'
        if sha else "(local)"
    )
    run = (
        f'<a href="https://github.com/{REPO_SLUG}/actions/runs/{escape(run_id)}">GitHub run #{escape(str(number))}</a>'
        if run_id else f"run #{escape(str(number))}"
    )
    return f'<p class="meta">{escape(str(metrics.get("timestamp", "")))} · commit {commit} · {run}</p>'


def _counts(cases: list[dict]) -> tuple[int, int, int]:
    passed = sum(1 for c in cases if c.get("passed") is True)
    failed = sum(1 for c in cases if c.get("passed") is False)
    return passed, failed, len(cases)


def ragas_case_html(case: dict) -> str:
    kw = case.get("keyword_passed")
    kw_txt = "—" if kw is None else ("keywords ✓" if kw else "keywords ✗")
    nums = f"faithfulness {_fmt(case.get('faithfulness'))} · relevancy {_fmt(case.get('relevancy'))} · {kw_txt}"
    body = [
        "<h4>Prompt</h4>" + _pre(case.get("prompt")),
        "<h4>Chatbot answer</h4>" + _pre(case.get("answer")),
    ]
    expected = case.get("expected_keywords") or []
    if expected:
        matched = case.get("keyword_matched") or []
        body.append(
            "<h4>Keyword check</h4><p>Expected any of: "
            + escape(", ".join(map(str, expected)))
            + "<br>Matched: " + escape(", ".join(map(str, matched)) or "none") + "</p>"
        )
    sources = case.get("sources") or []
    if sources:
        body.append("<h4>Retrieved sources</h4><ul>" + "".join(
            f"<li>{escape(str(s.get('file') or s.get('title') or 'source'))} "
            f"<span class='muted'>score {_fmt(s.get('score'))}</span></li>"
            for s in sources[:10]
        ) + "</ul>")
    why = []
    if case.get("passed") is False:
        if kw is False:
            why.append("the answer is missing the expected keywords")
        f, r = case.get("faithfulness"), case.get("relevancy")
        if f is not None or r is not None:
            why.append(f"faithfulness {_fmt(f)} / relevancy {_fmt(r)} below the credible thresholds")
        body.append("<h4>Why FAIL</h4><p class='reason'>" + escape("; ".join(why) or "thresholds not met") + "</p>")
    if case.get("accept_refusal"):
        body.append("<p class='muted'>An honest refusal counts as PASS for this question.</p>")
    return (
        f"<details class='case'{' open' if case.get('passed') is False else ''}><summary>{_badge(case.get('passed'))}"
        f"<span class='q'><b>{escape(str(case.get('id', '')))}</b> <span class='muted'>{escape(str(case.get('category', '')))}</span><br>"
        f"{escape(str(case.get('prompt', '')))}</span><span class='nums'>{nums}</span></summary>"
        f"<div class='body'>{''.join(body)}</div></details>"
    )


def giskard_case_html(case: dict) -> str:
    body = [
        "<h4>Prompt (generated by Giskard)</h4>" + _pre(case.get("prompt")),
        "<h4>Chatbot answer</h4>" + _pre(case.get("answer")),
        "<h4>Reference answer</h4>" + _pre(case.get("reference_answer")),
        f"<h4>Judge explanation — why {escape(case.get('status') or 'N/A')}</h4>"
        f"<p class='reason'>{escape(str(case.get('reason') or 'No reason recorded.'))}</p>",
    ]
    qtype = case.get("question_type") or case.get("category") or ""
    return (
        f"<details class='case'{' open' if case.get('passed') is False else ''}><summary>{_badge(case.get('passed'))}"
        f"<span class='q'><b>{escape(str(case.get('id', '')))}</b> <span class='muted'>{escape(str(qtype))}</span><br>"
        f"{escape(str(case.get('prompt', '')))}</span></summary>"
        f"<div class='body'>{''.join(body)}</div></details>"
    )


def ragas_page(metrics: dict, ragas_cases: dict) -> str:
    number = escape(str(metrics.get("run_number", RUN_NUMBER)))
    ragas = metrics.get("ragas", {})
    cases = ragas_cases.get("cases") or []
    passed, failed, total = _counts(cases)
    faith = ragas.get("avg_faithfulness")
    rows = "".join(ragas_case_html(c) for c in cases) or "<p>No per-question Ragas results were produced for this run.</p>"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Ragas run #{number}</title>
<style>{STYLE}</style>
<script>if (location.hash.startsWith('#giskard')) location.replace('giskard.html' + location.hash);</script>
</head>
<body>
<nav><a href="../../index.html">← all Ragas runs</a> · <a href="giskard.html">Giskard report for this run →</a></nav>
<h1 id="ragas">Ragas report — run #{number}</h1>
{_meta_line(metrics)}

<div class="grid">
  <div class="card"><div class="label">Questions passed</div><div class="value status-pass">{f"{passed}/{total}" if total else "—"}</div></div>
  <div class="card"><div class="label">Questions failed</div><div class="value {'status-fail' if failed else ''}">{failed if total else "—"}</div></div>
  <div class="card"><div class="label">Avg faithfulness</div><div class="value {('status-pass' if (faith or 0) >= 0.65 else 'status-fail')}">{_fmt(faith, 3)}</div></div>
  <div class="card"><div class="label">Avg relevancy</div><div class="value">{_fmt(ragas.get("avg_relevancy"), 3)}</div></div>
</div>

<h2>Questions ({total})</h2>
{TOOLS}
{rows}

<h2>Ragas summary report</h2>
<div id="ragas-md">Loading…</div>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
fetch('report.md').then(r => r.ok ? r.text() : 'Ragas report not produced for this run.').then(t => {{
  document.getElementById('ragas-md').innerHTML = marked.parse(t);
}}).catch(e => {{ document.getElementById('ragas-md').textContent = 'Failed to load Ragas report: ' + e; }});
</script>
</body></html>
"""


def giskard_page(metrics: dict, giskard_cases: dict) -> str:
    number = escape(str(metrics.get("run_number", RUN_NUMBER)))
    gr = metrics.get("giskard_rag", {})
    gs = metrics.get("giskard_scan", {})
    cases = giskard_cases.get("cases") or []
    passed, failed, total = _counts(cases)
    rows = "".join(giskard_case_html(c) for c in cases) or (
        "<p>No per-question Giskard results were produced for this run "
        "(older runs only stored the aggregate correctness).</p>"
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Giskard run #{number}</title>
<style>{STYLE}</style>
</head>
<body>
<nav><a href="../../giskard.html">← all Giskard runs</a> · <a href="index.html">Ragas report for this run →</a></nav>
<h1 id="giskard-rag">Giskard report — run #{number}</h1>
{_meta_line(metrics)}

<div class="grid">
  <div class="card"><div class="label">Questions passed</div><div class="value status-pass">{f"{passed}/{total}" if total else "—"}</div></div>
  <div class="card"><div class="label">Questions failed</div><div class="value {'status-fail' if failed else ''}">{failed if total else "—"}</div></div>
  <div class="card"><div class="label">RAG correctness</div><div class="value">{_fmt(gr.get("correctness"))}</div></div>
  <div class="card"><div class="label">Minimum</div><div class="value">{_fmt(gr.get("minimum_correctness"))}</div></div>
  <div class="card"><div class="label">Scan issues</div><div class="value">{escape(str(gs.get("total_issues", "—")))}</div></div>
</div>
<p class="muted">Judge: {escape(str(gr.get("judge", "—")))} · RAG API: {escape(str(gr.get("rag_api", "—")))}</p>

<h2>Generated questions ({total})</h2>
{TOOLS}
{rows}

<h2>Giskard RAG evaluation (full Giskard report)</h2>
<iframe src="giskard_rag.html" title="Giskard RAG eval"></iframe>

<h2 id="giskard-scan">Giskard vulnerability scan</h2>
<iframe src="giskard_scan.html" title="Giskard scan"></iframe>
</body></html>
"""


def _history_rows(rows: list[dict], giskard: bool) -> list[str]:
    body_rows = []
    for r in rows[:50]:
        rn = r.get("run_number", "?")
        ts = r.get("timestamp", "")[:19].replace("T", " ")
        commit = escape(r.get("commit", ""))
        if giskard:
            gr = r.get("giskard_rag", {})
            gs = r.get("giskard_scan", {})
            counts = r.get("giskard_counts") or {}
            q = f"{counts.get('passed')}/{counts.get('total')}" if counts.get("total") else "—"
            body_rows.append(
                f'<tr><td><a href="runs/{escape(str(rn))}/giskard.html">#{escape(str(rn))}</a></td>'
                f"<td>{escape(ts)}</td><td>{q}</td><td>{_fmt(gr.get('correctness'))}</td>"
                f"<td>{escape(str(gs.get('total_issues', '—')))}</td><td>{commit}</td></tr>"
            )
        else:
            ragas = r.get("ragas", {})
            counts = r.get("ragas_counts") or {}
            q = f"{counts.get('passed')}/{counts.get('total')}" if counts.get("total") else "—"
            faith = ragas.get("avg_faithfulness")
            cls = "status-pass" if (faith or 0) >= 0.65 else "status-fail"
            body_rows.append(
                f'<tr><td><a href="runs/{escape(str(rn))}/index.html">#{escape(str(rn))}</a></td>'
                f"<td>{escape(ts)}</td><td>{q}</td>"
                f'<td class="{cls}">{_fmt(faith, 3)}</td><td>{_fmt(ragas.get("avg_relevancy"), 3)}</td>'
                f'<td><a href="runs/{escape(str(rn))}/giskard.html">Giskard</a></td><td>{commit}</td></tr>'
            )
    return body_rows


def _sorted_history(history: list[dict]) -> list[dict]:
    def key(r):
        try:
            return int(r.get("run_number", 0))
        except Exception:
            return 0
    return sorted(history, key=key, reverse=True)


def index_html(history: list[dict]) -> str:
    rows = _sorted_history(history)
    body_rows = _history_rows(rows, giskard=False)
    chart_data = [
        {
            "x": r.get("run_number"),
            "faith": r.get("ragas", {}).get("avg_faithfulness"),
            "rel": r.get("ragas", {}).get("avg_relevancy"),
        }
        for r in reversed(rows)
        if r.get("ragas", {}).get("avg_faithfulness") is not None
    ]
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Ragas Nightly — {escape(REPO_SLUG)}</title>
<style>{STYLE}
  #chart-wrap {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; margin-top: 8px; }}
</style></head>
<body>
<nav><a href="giskard.html">Giskard runs →</a></nav>
<h1>Ragas Nightly</h1>
<p class="meta">
  Repo: <a href="https://github.com/{escape(REPO_SLUG)}">{escape(REPO_SLUG)}</a>
  · Runs: {len(rows)}
  · Latest: {escape((rows[0]['timestamp'][:19] if rows else '—').replace('T',' '))}
</p>
<p class="meta">GitHub run numbers can have gaps: skipped hourly checks and failed partial runs still consume numbers. Only runs that published reports are listed.</p>

<h2>Faithfulness trend</h2>
<div id="chart-wrap"><canvas id="chart" height="80"></canvas></div>

<h2>Runs (last 50)</h2>
<table>
  <thead><tr>
    <th>Run</th><th>Timestamp (UTC)</th><th>Passed</th><th>Faithfulness</th><th>Relevancy</th><th>Giskard</th><th>Commit</th>
  </tr></thead>
  <tbody>
    {''.join(body_rows) or '<tr><td colspan="7">No runs yet.</td></tr>'}
  </tbody>
</table>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script>
const data = {json.dumps(chart_data)};
new Chart(document.getElementById('chart'), {{
  type: 'line',
  data: {{
    labels: data.map(d => '#' + d.x),
    datasets: [
      {{ label: 'Faithfulness', data: data.map(d => d.faith), borderColor: '#0a7c0a', tension: 0.2 }},
      {{ label: 'Answer relevancy', data: data.map(d => d.rel), borderColor: '#0366d6', tension: 0.2 }},
    ]
  }},
  options: {{
    scales: {{ y: {{ min: 0, max: 1, ticks: {{ stepSize: 0.1 }} }} }},
    plugins: {{ legend: {{ position: 'bottom' }} }}
  }}
}});
</script>
</body></html>
"""


def giskard_index_html(history: list[dict]) -> str:
    rows = [r for r in _sorted_history(history) if r.get("giskard_rag")]
    body_rows = _history_rows(rows, giskard=True)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Giskard Nightly — {escape(REPO_SLUG)}</title>
<style>{STYLE}</style></head>
<body>
<nav><a href="index.html">← Ragas runs</a></nav>
<h1>Giskard Nightly</h1>
<p class="meta">Auto-generated RAG questions judged by the local GPT-OSS model, plus the vulnerability scan. Runs: {len(rows)}</p>
<table>
  <thead><tr><th>Run</th><th>Timestamp (UTC)</th><th>Questions passed</th><th>Correctness</th><th>Scan issues</th><th>Commit</th></tr></thead>
  <tbody>{''.join(body_rows) or '<tr><td colspan="6">No Giskard runs yet.</td></tr>'}</tbody>
</table>
</body></html>
"""


def backfill_giskard_pages(history: list[dict]) -> int:
    """Give older runs (single combined page) their own giskard.html too."""
    made = 0
    for record in history:
        run_dir = SITE / "runs" / str(record.get("run_number"))
        if not run_dir.is_dir() or (run_dir / "giskard.html").exists():
            continue
        metrics = {**record, **load_json(run_dir / "metrics.json")}
        cases = load_json(run_dir / "giskard_rag_cases.json")
        for c in cases.get("cases") or []:
            c.setdefault("passed", None)
        (run_dir / "giskard.html").write_text(giskard_page(metrics, cases), encoding="utf-8")
        made += 1
    return made


def main() -> None:
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)
    (SITE / ".nojekyll").write_text("")

    # 1. Bring forward the existing site so dashboards for other tools
    # (/promptfoo, /k6, /llm-judge, etc.) are not wiped by this publish.
    history: list[dict] = []
    if EXISTING.exists():
        for item in EXISTING.iterdir():
            if item.name == ".git":
                continue
            dest = SITE / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        hist_path = EXISTING / "history.json"
        if hist_path.exists():
            try:
                history = json.loads(hist_path.read_text())
            except Exception:
                history = []

    # 2. Build this run's directory.
    run_dir = SITE / "runs" / str(RUN_NUMBER)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Copy whatever the eval steps produced (any may be missing on partial runs).
    names = [
        "report.md", "summary.json", "ragas_cases.json",
        "giskard_rag.html", "giskard_rag.json", "giskard_rag_cases.json",
        "giskard_scan.html", "giskard_scan.json",
    ]
    copied = []
    for name in names:
        src = RESULTS / name
        if src.exists():
            shutil.copy2(src, run_dir / name)
            copied.append(name)

    # Per-run placeholder HTMLs so iframes don't 404.
    for stub in ("giskard_rag.html", "giskard_scan.html"):
        p = run_dir / stub
        if not p.exists():
            p.write_text(
                f"<html><body><p>{stub} not produced for run #{RUN_NUMBER}.</p></body></html>"
            )

    ragas_cases = load_json(RESULTS / "ragas_cases.json")
    giskard_cases = load_json(RESULTS / "giskard_rag_cases.json")
    rp, rf, rt = _counts(ragas_cases.get("cases") or [])
    gp, gf, gt = _counts(giskard_cases.get("cases") or [])

    # 3. Aggregate metrics for the index.
    metrics = {
        "run_number": (int(RUN_NUMBER) if str(RUN_NUMBER).isdigit() else RUN_NUMBER),
        "run_id": RUN_ID,
        "timestamp": TIMESTAMP,
        "commit": COMMIT_SHA,
        "ragas": extract_ragas_summary(RESULTS / "report.md"),
        "ragas_counts": {"passed": rp, "failed": rf, "total": rt},
        "giskard_rag": load_json(RESULTS / "giskard_rag.json"),
        "giskard_counts": {"passed": gp, "failed": gf, "total": gt},
        "giskard_scan": load_json(RESULTS / "giskard_scan.json"),
        "files": copied,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))

    # 4. Update history (replace if same run_number re-published).
    history = [h for h in history if h.get("run_number") != metrics["run_number"]]
    history.append(metrics)
    (SITE / "history.json").write_text(json.dumps(history, indent=2))

    # 5. Per-run + root pages.
    (run_dir / "index.html").write_text(ragas_page(metrics, ragas_cases), encoding="utf-8")
    (run_dir / "giskard.html").write_text(giskard_page(metrics, giskard_cases), encoding="utf-8")
    (SITE / "index.html").write_text(index_html(history), encoding="utf-8")
    (SITE / "giskard.html").write_text(giskard_index_html(history), encoding="utf-8")
    backfilled = backfill_giskard_pages(history)

    print(f"Site built at {SITE}")
    print(f"  Run #{RUN_NUMBER}: {len(copied)} report files copied")
    print(f"  Ragas questions: {rp}/{rt} passed · Giskard questions: {gp}/{gt} passed")
    print(f"  History now has {len(history)} runs ({backfilled} older Giskard pages backfilled)")


if __name__ == "__main__":
    main()
