#!/usr/bin/env python3
"""
ragas_eval.py — Ragas-based RAG quality evaluation for alexpavsky.com.

Runs golden questions through the RAG API, then evaluates each answer with
Ragas metrics (faithfulness, answer_relevancy) using a cloud judge and CPU embeddings.

Designed for both local development and nightly CI runs.

Usage (local):
    pip install -r eval/requirements.txt
    python eval/cloud_gateway.py  # in a separate process
    python eval/ragas_eval.py

Usage (CI):
    See .github/workflows/ragas-nightly.yml

Environment variables:
    CLOUD_EVAL_BASE_URL Optional. OpenAI-compatible judge gateway endpoint.
    RAG_API_URL        Optional. Default: http://localhost:8001
    MIN_FAITHFULNESS   Optional. Default: 0.50. Build fails if avg below.
    MIN_RELEVANCY      Optional. Default: 0.35. Build fails if avg below.
    MAX_QUESTIONS      Optional. Limit number of questions (debugging).
    OUTPUT_PATH        Optional. Default: eval/results/report.md
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = ROOT / "tests" / "rag" / "golden_questions.json"
RESULTS_DIR = ROOT / "eval" / "results"

RAG_API_URL = os.environ.get("RAG_API_URL", "http://localhost:8001").rstrip("/")

# The judge and embedding provider are configured by rotating_llm.py.

MIN_FAITHFULNESS = float(os.environ.get("MIN_FAITHFULNESS", "0.50"))
MIN_RELEVANCY = float(os.environ.get("MIN_RELEVANCY", "0.35"))
MAX_QUESTIONS = int(os.environ.get("MAX_QUESTIONS", "0"))  # 0 = all
# How many individual question failures are tolerated before the run is marked
# failed. The default allows a small minority of weak answers while still
# catching broad regressions.
# Set ALLOWED_FAILURES=-1 to fall back to average-only gating (legacy behaviour).
ALLOWED_FAILURES = int(os.environ.get("ALLOWED_FAILURES", "3"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", str(RESULTS_DIR / "report.md")))

REQUEST_DELAY_SEC = float(os.environ.get("REQUEST_DELAY_SEC", "1.0"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def merge_metric_rows(records: list[dict], scored_samples: list[dict], rows: list[dict]) -> None:
    """Merge successful metric values while preserving earlier retry results."""
    for i, sample in enumerate(scored_samples):
        row = rows[i] if i < len(rows) else {}
        record = records[sample["_idx"]]
        for source, destination in (
            ("faithfulness", "faithfulness"),
            ("answer_relevancy", "relevancy"),
        ):
            try:
                value = float(row.get(source))
            except (TypeError, ValueError):
                continue
            if not math.isnan(value):
                record[destination] = value


def samples_missing_metrics(records: list[dict], samples: list[dict]) -> list[dict]:
    """Return only samples that still lack either required judge metric."""
    return [
        sample for sample in samples
        if math.isnan(records[sample["_idx"]]["faithfulness"])
        or math.isnan(records[sample["_idx"]]["relevancy"])
    ]


def query_rag_api(question: str, max_retries: int = 3) -> dict[str, Any]:
    """Hit /api/rag/query and return the JSON response.

    Retries on:
      - Transient HTTP / network errors (requests.RequestException).
      - 200 OK responses whose `answer` is a sentinel string emitted by the
        upstream chatbot when all of its own LLM providers are rate-limited
        (e.g. "LLM unavailable — all providers failed. Last error: …").
        These look like successes to the HTTP layer but are functional
        failures we want to give a chance to recover.

    Exponential backoff between attempts: 5s, 15s, 45s.
    """
    TRANSIENT_MARKERS = (
        "llm unavailable",
        "all providers failed",
        "last error:",
    )

    for attempt in range(max_retries):
        is_last = attempt == max_retries - 1
        delay = 5 * (3 ** attempt)  # 5, 15, 45
        try:
            r = requests.post(
                f"{RAG_API_URL}/api/rag/query",
                json={"query": question},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            answer_lc = (data.get("answer") or "").lower()
            if any(m in answer_lc for m in TRANSIENT_MARKERS):
                msg = (f"upstream chatbot returned transient LLM-unavailable "
                       f"(attempt {attempt + 1}/{max_retries})")
                if is_last:
                    log(f"  WARN: {msg} — giving up, keeping unavailable answer")
                    return data
                log(f"  WARN: {msg}; backing off {delay}s before retry")
                time.sleep(delay)
                continue
            return data
        except requests.RequestException as e:
            if is_last:
                log(f"  WARN: RAG API call failed for '{question[:60]}...': {e}")
                return {"answer": "", "sources": [], "metrics": {}}
            log(f"  WARN: RAG API call failed ({e}); backing off {delay}s before retry")
            time.sleep(delay)

    return {"answer": "", "sources": [], "metrics": {}}


def keyword_score(answer: str, expected_any: list[str], min_matches: int = 1) -> dict[str, Any]:
    """Light keyword check — counts how many expected terms appear in the answer."""
    answer_lower = answer.lower()
    matches = [kw for kw in expected_any if kw.lower() in answer_lower]
    passed = len(matches) >= min_matches
    return {"matched": matches, "match_count": len(matches), "required": min_matches, "passed": passed}


def write_report(records: list[dict], summary: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append(f"# Ragas Evaluation Report\n")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()}_\n")
    lines.append(f"\n## Summary\n")
    lines.append(f"| Metric | Value | Threshold | Status |")
    lines.append(f"|---|---|---|---|")
    lines.append(
        f"| Average faithfulness    | {summary['avg_faithfulness']:.3f} "
        f"| ≥ {MIN_FAITHFULNESS:.2f} | {'PASS' if summary['avg_faithfulness'] >= MIN_FAITHFULNESS else 'FAIL'} |"
    )
    lines.append(
        f"| Average answer_relevancy | {summary['avg_relevancy']:.3f} "
        f"| ≥ {MIN_RELEVANCY:.2f} | {'PASS' if summary['avg_relevancy'] >= MIN_RELEVANCY else 'FAIL'} |"
    )
    lines.append(f"| Keyword check pass rate | {summary['keyword_pass_rate']:.1%} | ≥ 60% | "
                 f"{'PASS' if summary['keyword_pass_rate'] >= 0.6 else 'FAIL'} |")
    lines.append(f"| Questions evaluated     | {summary['total']} | — | — |")
    lines.append(f"| Judge model             | `{summary.get('judge_model', 'unknown')}` | — | — |")
    lines.append(f"")

    lines.append(f"## Per-Question Results\n")
    lines.append("| ID | Category | Faithfulness | Relevancy | Keywords | Answer (preview) |")
    lines.append("|---|---|---|---|---|---|")
    import math
    for r in records:
        kw = "—" if r["keyword_passed"] is None else ("PASS" if r["keyword_passed"] else "FAIL")
        ans_prev = (r["answer"] or "")[:100].replace("\n", " ").replace("|", "\\|")
        f_str = "n/a" if math.isnan(r["faithfulness"]) else f"{r['faithfulness']:.2f}"
        r_str = "n/a" if math.isnan(r["relevancy"]) else f"{r['relevancy']:.2f}"
        lines.append(
            f"| {r['id']} | {r['category']} | {f_str} | {r_str} | {kw} | {ans_prev}... |"
        )

    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    log(f"\nReport written to: {OUTPUT_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    log("=" * 72)
    log("Ragas RAG Quality Evaluation — alexpavsky.com")
    log("=" * 72)
    log(f"RAG API:       {RAG_API_URL}")
    log(f"Thresholds:    faithfulness >= {MIN_FAITHFULNESS}, relevancy >= {MIN_RELEVANCY}")
    log("")

    from rotating_llm import build_provider_list
    providers = build_provider_list()
    if not providers:
        fail("No cloud judge is configured. Start eval/cloud_gateway.py.")
    log(f"Judge providers available: {len(providers)}")
    for p in providers:
        log(f"  - {p['name']:20s} / {p['model']}")

    # Health check
    try:
        h = requests.get(f"{RAG_API_URL}/api/health", timeout=5).json()
        log(f"Health: postgres={h.get('postgres')}, qdrant={h.get('qdrant')}")
    except Exception as e:
        fail(f"RAG API not reachable at {RAG_API_URL}: {e}")

    # Load questions
    if not QUESTIONS_PATH.exists():
        fail(f"Golden questions file not found: {QUESTIONS_PATH}")
    data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    questions = (
        data.get("specific_facts", [])
        + data.get("concept_questions", [])
        + data.get("negative_tests", [])
    )
    if MAX_QUESTIONS > 0:
        questions = questions[:MAX_QUESTIONS]
    log(f"Loaded {len(questions)} golden questions\n")

    # Step 1: Hit RAG API for every question, collect (question, answer, contexts)
    log("Step 1/3: Querying RAG API for all questions...")
    log("-" * 72)
    samples_for_ragas: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    for i, q in enumerate(questions, 1):
        qid = q.get("id", f"q-{i}")
        question_text = q["question"]
        log(f"  [{i}/{len(questions)}] {qid}: {question_text[:70]}...")

        resp = query_rag_api(question_text)
        answer = resp.get("answer", "")
        # Prefer the full `contexts` array (un-truncated chunk text the LLM
        # actually saw). Fall back to `sources[].content` (300-char preview)
        # only if the API is on an older build — the preview is too short for
        # Ragas faithfulness to verify multi-fact answers.
        contexts = resp.get("contexts") or [
            s.get("content", "") for s in resp.get("sources", []) if s.get("content")
        ]
        sources = resp.get("sources", [])

        # Keyword check (cheap, doesn't need LLM)
        kw_terms = q.get("expected_contains") or q.get("expected_contains_any") or []
        min_matches = q.get("min_keyword_matches", 1)
        kw = keyword_score(answer, kw_terms, min_matches) if kw_terms else None
        if kw:
            log(f"      keywords: {kw['match_count']}/{kw['required']} required "
                f"({'PASS' if kw['passed'] else 'FAIL'}) — matched: {kw['matched']}")

        records.append({
            "id": qid,
            "category": q.get("category", "—"),
            "question": question_text,
            "answer": answer,
            "sources": sources,
            "expected_keywords": kw_terms,
            "contexts_count": len(contexts),
            "faithfulness": 0.0,    # filled in step 2
            "relevancy": 0.0,       # filled in step 2
            "keyword_passed": kw["passed"] if kw else None,
            "keyword_matched": kw["matched"] if kw else [],
            # If true, an honest refusal counts as PASS. Used for questions
            # whose facts aren't in the RAG corpus on purpose, or for
            # provocations where refusal is the desired behavior.
            "accept_refusal": q.get("accept_refusal", False),
        })

        if contexts and answer:
            samples_for_ragas.append({
                "user_input": question_text,
                "response": answer,
                "retrieved_contexts": contexts,
                "_idx": len(records) - 1,
            })

        time.sleep(REQUEST_DELAY_SEC)

    log(f"\n  Collected {len(samples_for_ragas)} valid samples for Ragas evaluation\n")

    if not samples_for_ragas:
        fail("No valid samples to evaluate — check that documents are loaded in the RAG DB.")

    # Step 2: Run Ragas
    log("Step 2/3: Running Ragas evaluation (faithfulness + answer_relevancy)...")
    log("-" * 72)
    log("  Loading Ragas + LangChain (first run can be slow)...")

    try:
        from langchain_core.embeddings import Embeddings
        from ragas import evaluate, EvaluationDataset, SingleTurnSample
        from ragas.metrics import Faithfulness, ResponseRelevancy
        from ragas.llms import LangchainLLMWrapper
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.run_config import RunConfig
    except ImportError as e:
        fail(f"Missing dependency: {e}. Run: pip install -r eval/requirements.txt")

    # The wrapper shape is retained for Ragas compatibility.
    from rotating_llm import RotatingJudgeLLM, lifecycle_headers
    judge_llm = RotatingJudgeLLM(
        providers=providers,
        temperature=0,
        timeout=int(os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600")),
    )
    primary_name = providers[0]["name"]
    primary_model = providers[0]["model"]
    log(f"  Judge: cloud gateway ({len(providers)} configured provider)")
    log(f"  Primary: {primary_name} / {primary_model}")
    fallbacks = providers[1:]

    embedding_model = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_root = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
    if not embedding_root:
        embedding_root = providers[0]["base_url"].removesuffix("/v1")

    class OllamaNativeEmbeddings(Embeddings):
        def _embed(self, texts: list[str]) -> list[list[float]]:
            response = requests.post(
                f"{embedding_root}/api/embed",
                headers={
                    "Authorization": f"Bearer {providers[0]['api_key']}",
                    **lifecycle_headers(embedding_model),
                },
                json={"model": embedding_model, "input": texts, "keep_alive": -1},
                timeout=int(os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600")),
            )
            response.raise_for_status()
            return response.json()["embeddings"]

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return self._embed(texts)

        def embed_query(self, text: str) -> list[float]:
            return self._embed([text])[0]

    judge_embeds = OllamaNativeEmbeddings()
    log(f"  Embeddings: {embedding_model} via {embedding_root}/api/embed")

    ragas_llm = LangchainLLMWrapper(judge_llm)
    ragas_embeds = LangchainEmbeddingsWrapper(judge_embeds)

    samples = [
        SingleTurnSample(
            user_input=s["user_input"],
            response=s["response"],
            retrieved_contexts=s["retrieved_contexts"],
        )
        for s in samples_for_ragas
    ]
    dataset = EvaluationDataset(samples=samples)

    log(f"  Evaluating {len(samples)} samples (this takes ~{len(samples) * 3}s)...")
    for sample in samples_for_ragas:
        records[sample["_idx"]]["faithfulness"] = float("nan")
        records[sample["_idx"]]["relevancy"] = float("nan")

    def run_metrics(samples_to_score, *, show_progress):
        retry_dataset = EvaluationDataset(samples=[
            SingleTurnSample(
                user_input=s["user_input"],
                response=s["response"],
                retrieved_contexts=s["retrieved_contexts"],
            )
            for s in samples_to_score
        ])
        return evaluate(
            dataset=retry_dataset,
            metrics=[Faithfulness(), ResponseRelevancy()],
            llm=ragas_llm,
            embeddings=ragas_embeds,
            run_config=RunConfig(
                timeout=int(os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600")),
                max_retries=2,
                max_workers=1,
            ),
            raise_exceptions=False,
            show_progress=show_progress,
        ).to_pandas().to_dict("records")

    try:
        merge_metric_rows(
            records,
            samples_for_ragas,
            run_metrics(samples_for_ragas, show_progress=True),
        )
    except Exception as e:
        fail(f"Ragas evaluation failed: {e}")

    # Ragas returns NaN when a local judge call times out or its JSON cannot be
    # parsed. Retry only those samples so one transient generation cannot leave
    # a superficially green report with missing verdicts.
    metric_retry_limit = max(0, int(os.environ.get("RAGAS_METRIC_RETRIES", "2")))
    metric_retry_attempts = 0
    for attempt in range(1, metric_retry_limit + 1):
        missing = samples_missing_metrics(records, samples_for_ragas)
        if not missing:
            break
        metric_retry_attempts = attempt
        log(
            f"  Retrying missing judge metrics for {len(missing)} sample(s) "
            f"(attempt {attempt}/{metric_retry_limit})..."
        )
        try:
            merge_metric_rows(records, missing, run_metrics(missing, show_progress=False))
        except Exception as e:
            log(f"  Judge metric retry {attempt} failed: {type(e).__name__}: {e}")

    # Step 3: Summary + Report
    log("\nStep 3/3: Building report...")
    log("-" * 72)
    # Average only over non-NaN values (judge parsing failures excluded).
    f_vals = [r["faithfulness"] for r in records if not math.isnan(r["faithfulness"])]
    r_vals = [r["relevancy"] for r in records if not math.isnan(r["relevancy"])]
    avg_f = sum(f_vals) / max(len(f_vals), 1) if f_vals else 0.0
    avg_r = sum(r_vals) / max(len(r_vals), 1) if r_vals else 0.0
    parse_failures_f = len(records) - len(f_vals)
    parse_failures_r = len(records) - len(r_vals)

    kw_passes = [r["keyword_passed"] for r in records if r["keyword_passed"] is not None]
    kw_rate = sum(1 for p in kw_passes if p) / max(len(kw_passes), 1) if kw_passes else 0.0

    summary = {
        "avg_faithfulness": avg_f,
        "avg_relevancy": avg_r,
        "keyword_pass_rate": kw_rate,
        "total": len(records),
        "f_parsed": len(f_vals),
        "r_parsed": len(r_vals),
        "f_failures": parse_failures_f,
        "r_failures": parse_failures_r,
        "metric_retry_attempts": metric_retry_attempts,
        "judge_model": f"{primary_name}/{primary_model} (+{len(fallbacks)} fallbacks)",
    }

    log("")
    log("Results")
    log("-" * 72)
    log(f"  Average faithfulness:     {avg_f:.3f}   (threshold {MIN_FAITHFULNESS:.2f}, parsed {summary['f_parsed']}/{len(records)})")
    log(f"  Average answer_relevancy: {avg_r:.3f}   (threshold {MIN_RELEVANCY:.2f}, parsed {summary['r_parsed']}/{len(records)})")
    log(f"  Keyword check pass rate:  {kw_rate:.1%}")
    log(f"  Samples evaluated:        {len(records)}")
    if parse_failures_f or parse_failures_r:
        log(f"  Judge parse failures:     faithfulness={parse_failures_f}, relevancy={parse_failures_r}")
        log(f"  (These are usually short answers the judge can't fact-check)")
    log("")

    write_report(records, summary)

    # Per-question pass/fail summary, consumed by .github/scripts/notify_slack.py
    # (via ragas-nightly.yml) to populate the #qa-rag-eval Slack post.
    #
    # Pass/fail policy is category-aware:
    #   - `refusal` category: the question is intentionally provocative or
    #     about info not in the corpus. PASS iff the model properly refused
    #     (matched refusal pattern OR keyword check matched a refusal term).
    #     Ragas faithfulness/relevancy are meaningless here (a refusal is
    #     not "relevant to the question" by Ragas's definition).
    #   - Questions with `accept_refusal: true` in golden_questions.json:
    #     same logic as refusal — these are facts the RAG corpus is known
    #     to not cover; honest refusal is the correct behavior.
    #   - All other categories: PASS iff keyword check matched AND judge
    #     scores cleared thresholds. If the judge couldn't score (NaN), we
    #     fall back to keyword check alone instead of auto-failing.
    REFUSAL_PHRASES = (
        "does not contain", "doesn't contain", "does not provide",
        "doesn't provide", "no information", "not provided",
        "cannot find", "unable to", "i don't have", "no specific",
        "not mentioned", "no data", "no details",
    )

    def _is_refusal_answer(answer: str) -> bool:
        a = (answer or "").lower()
        return any(p in a for p in REFUSAL_PHRASES)

    def _is_passed(r: dict) -> bool:
        cat = r.get("category", "")
        accept_refusal = r.get("accept_refusal", False)
        refused = _is_refusal_answer(r.get("answer", ""))

        # Refusal-style scoring: PASS iff model refused honestly.
        if cat == "refusal" or accept_refusal:
            return refused or bool(r.get("keyword_passed"))

        # Standard scoring with NaN-tolerant judge fallback.
        # Requires keyword match. For judge metrics we accept either of:
        #  - both above their own threshold (strictest, classical pass);
        #  - one above threshold AND the other still credible (>= 0.3),
        #    which covers terse-but-correct answers where the judge can
        #    fact-check one dimension but not the other.
        # A missing judge metric is incomplete evaluation, not a quality pass.
        if not r.get("keyword_passed"):
            return False
        f = r.get("faithfulness", float("nan"))
        rel = r.get("relevancy", float("nan"))
        if math.isnan(f) or math.isnan(rel):
            return False
        if f >= MIN_FAITHFULNESS and rel >= MIN_RELEVANCY:
            return True
        MIN_CREDIBLE = 0.3
        one_strong = f >= MIN_FAITHFULNESS or rel >= MIN_RELEVANCY
        other_credible = min(f, rel) >= MIN_CREDIBLE
        return one_strong and other_credible

    passed_count = sum(1 for r in records if _is_passed(r))

    # Per-question rows for the Ragas Pages report (scripts/build_eval_site.py).
    def _num(v):
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else v

    case_rows = [{
        "id": r.get("id"),
        "category": r.get("category"),
        "prompt": r.get("question", ""),
        "answer": r.get("answer", ""),
        "sources": [
            {"file": s.get("filename") or s.get("title") or s.get("source") or "", "score": s.get("score")}
            for s in (r.get("sources") or []) if isinstance(s, dict)
        ],
        "contexts_count": r.get("contexts_count", 0),
        "faithfulness": _num(r.get("faithfulness")),
        "relevancy": _num(r.get("relevancy")),
        "expected_keywords": r.get("expected_keywords", []),
        "keyword_passed": r.get("keyword_passed"),
        "keyword_matched": r.get("keyword_matched", []),
        "accept_refusal": r.get("accept_refusal", False),
        "passed": _is_passed(r),
    } for r in records]

    failed_count = len(records) - passed_count
    failed_checks: list[str] = []
    if parse_failures_f or parse_failures_r:
        failed_checks.append(
            "incomplete judge metrics "
            f"(faithfulness={parse_failures_f}, relevancy={parse_failures_r})"
        )
    if avg_f < MIN_FAITHFULNESS:
        failed_checks.append(f"avg faithfulness {avg_f:.3f} < {MIN_FAITHFULNESS}")
    if avg_r < MIN_RELEVANCY:
        failed_checks.append(f"avg relevancy {avg_r:.3f} < {MIN_RELEVANCY}")
    if ALLOWED_FAILURES >= 0 and failed_count > ALLOWED_FAILURES:
        failed_checks.append(
            f"{failed_count} question(s) failed "
            f"(> {ALLOWED_FAILURES} allowed)")
    completion = {
        "evaluation_completed": True,
        "quality_passed": not failed_checks,
    }

    (RESULTS_DIR / "ragas_cases.json").write_text(
        json.dumps({
            **completion,
            "thresholds": {
                "faithfulness": MIN_FAITHFULNESS,
                "relevancy": MIN_RELEVANCY,
                "allowed_failures": ALLOWED_FAILURES,
            },
            "cases": case_rows,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    summary_json = {
        "passed": passed_count,
        "failed": failed_count,
        "flaky": 0,
        "skipped": 0,
        "total": len(records),
        "avg_faithfulness": avg_f,
        "avg_relevancy": avg_r,
        "keyword_pass_rate": kw_rate,
        **completion,
    }
    (RESULTS_DIR / "summary.json").write_text(
        json.dumps(summary_json, indent=2), encoding="utf-8"
    )

    # Exit code: pass/fail based on thresholds.
    # Previously this only considered the *average* faithfulness/relevancy, so a
    # run with individual question failures (e.g. 24/27 passed) still exited 0,
    # GitHub marked it "success", and the auto-fix gate (conclusion == failure)
    # never fired. We now also fail when too many individual questions fail.
    if failed_checks:
        log("FAIL: " + "; ".join(failed_checks))
        return 1

    log("PASS: all thresholds met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
