from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


SITE = load_module("build_vision_audit_site", ROOT / "scripts/build_vision_audit_site.py")
SLACK = load_module("notify_vision_slack", ROOT / ".github/scripts/notify_vision_slack.py")
PIPELINE_SLACK = load_module("notify_slack", ROOT / ".github/scripts/notify_slack.py")
OBS_SLACK = load_module(
    "notify_observability_slack",
    ROOT / ".github/scripts/notify_observability_slack.py",
)
BOSGAME_SLACK = load_module(
    "notify_bosgame_run_slack",
    ROOT / ".github/scripts/notify_bosgame_run_slack.py",
)
LLM_SITE = load_module("build_llm_judge_site", ROOT / "scripts/build_llm_judge_site.py")


def sample_report() -> dict:
    return {
        "status": "passed",
        "model": "deepseek-flash",
        "model_provenance": {
            "execution": "cloud-api",
            "provider": "DeepSeek",
            "endpoint": "https://api.deepseek.com",
            "cloud_llm_calls": 1,
            "cloud_api_requests": 2,
            "provider_calls": {"Google Gemini": 0, "DeepSeek": 1},
            "cloud_api_keys_used": ["GEMINI_API_KEY", "DEEPSEEK_API_KEY"],
            "local_llm_calls": 0,
        },
        "model_usage": {"calls": 1, "prompt_tokens": 100, "completion_tokens": 20},
        "steps": [{
            "step": 1,
            "test_case_id": "VISION-001",
            "test_case_name": "AI Chat returns a real answer",
            "test_case_type": "planned browser journey with cloud Vision review",
            "objective": "Verify initial render.",
            "expected_result": "No concrete defect.",
            "actual_result": "No calibrated defects confirmed.",
            "scenario_input": "What does a smoke test verify?",
            "scenario_output": "It verifies that critical functionality is available.",
            "deterministic_passed": True,
            "verdict": "passed",
            "url": "https://www.alexpavsky.com/",
            "title": "Alex Pavlovsky",
            "screenshot": "/runner/vision-audit/screenshots/step-01.png",
            "summary": "The page rendered correctly.",
            "decision": {
                "next_action_executed": {
                    "kind": "scroll",
                    "direction": "down",
                    "reason": "Expand coverage.",
                },
            },
            "action_result": {"executed": True, "action": "scroll:down"},
            "model_latency_ms": 1234,
            "llm_execution": "cloud-api",
            "llm_provider": "DeepSeek",
            "llm_model": "deepseek-flash",
            "llm_fallback_reason": "Gemini HTTP 403",
            "browser_evidence": {},
            "candidate_findings": [],
            "confirmed_findings": [],
        }],
        "confirmed_findings": [],
    }


class VisionAuditReportingTests(unittest.TestCase):
    def test_agent_has_ten_required_openings_and_live_ai_journeys(self):
        source = (ROOT / ".github/scripts/vision_audit_agent.mjs").read_text(encoding="utf-8")
        for index in range(1, 11):
            self.assertIn(f"LINK-{index:03d}", source)
        for case_id in ("CHAT-001", "CHAT-002", "VOICE-001", "VOICE-002", "CHALLENGE-001"):
            self.assertIn(case_id, source)

    def test_hosted_workflow_uses_gemini_with_deepseek_fallback(self):
        workflow = (ROOT / ".github/workflows/agentic-vision-audit.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("runs-on: [self-hosted", workflow)
        self.assertIn("GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}", workflow)
        self.assertIn("DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}", workflow)
        self.assertIn("@playwright/test@1.61.0", workflow)
        self.assertNotIn("OLLAMA_BASE_URL", workflow)
        self.assertNotIn("LOCAL_LLM_API_KEY", workflow)
        self.assertIn("daily-audit-runs/${{ github.run_id }}", workflow)
        self.assertIn("Daily Audit report is stale", workflow)
        self.assertIn("vision-audit-start-ns", workflow)
        self.assertIn("Post summary and every test case to Slack", workflow)
        self.assertIn("if: always() && steps.bootstrap.outcome == 'success'", workflow)
        self.assertIn('[[ "$outcome" == "success" ]] || failed=1', workflow)
        self.assertIn('provenance.get("cloud_llm_calls") != len(expected)', workflow)
        self.assertIn('provider_calls.items()', workflow)
        source = (ROOT / ".github/scripts/vision_audit_agent.mjs").read_text(encoding="utf-8")
        self.assertIn("report.operational_error ||= `Cloud Vision review failed", source)
        self.assertIn("x-goog-api-key", source)
        self.assertIn("inlineData: { mimeType: 'image/png'", source)
        self.assertIn("payload.usageMetadata?.promptTokenCount", source)
        self.assertIn("model: deepseekModel", source)
        self.assertIn("data:image/png;base64,${imageData}", source)
        self.assertIn("payload.usage?.prompt_tokens", source)
        self.assertIn("review = await reviewWithDeepSeek", source)
        self.assertNotIn("/api/chat", source)

    def test_vision_fallback_records_deepseek_decisions_after_gemini_403(self):
        script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
let geminiRequests = 0;
let deepseekRequests = 0;
globalThis.fetch = async (url, options) => {
  const body = JSON.parse(options.body);
  if (String(url).includes('generativelanguage.googleapis.com')) {
    geminiRequests += 1;
    assert.equal(body.contents[0].parts[1].inlineData.mimeType, 'image/png');
    return new Response(JSON.stringify({ error: { message: 'key rejected' } }), { status: 403 });
  }
  deepseekRequests += 1;
  assert.equal(body.model, 'deepseek-flash');
  assert.match(body.messages[0].content[1].image_url.url, /^data:image\/png;base64,/);
  return new Response(JSON.stringify({
    choices: [{ message: { content: '{"summary":"Two panels are visible.","visual_findings":[],"functional_findings":[]}' }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 12, completion_tokens: 7 },
  }), { status: 200 });
};
const { askVisionAgent, report } = await import(__MODULE_URL__);
const image = `${process.env.VISION_AUDIT_OUTPUT_DIR}/sample.png`;
fs.writeFileSync(image, Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64'));
for (let index = 0; index < 2; index += 1) {
  const review = await askVisionAgent(image, { test_case_id: `LINK-${index + 1}` });
  assert.equal(review.provider, 'DeepSeek');
  assert.equal(review.model, 'deepseek-flash');
  assert.equal(review.fallback_reason, 'Gemini HTTP 403');
}
assert.equal(geminiRequests, 1);
assert.equal(deepseekRequests, 2);
assert.equal(report.model, 'deepseek-flash');
assert.equal(report.model_usage.calls, 2);
assert.equal(report.model_usage.prompt_tokens, 24);
assert.equal(report.model_usage.completion_tokens, 14);
assert.equal(report.model_provenance.cloud_api_requests, 3);
assert.equal(report.model_provenance.provider_calls.DeepSeek, 2);
assert.deepEqual(report.model_provenance.cloud_api_keys_used, ['GEMINI_API_KEY', 'DEEPSEEK_API_KEY']);
""".replace(
            "__MODULE_URL__",
            json.dumps((ROOT / ".github/scripts/vision_audit_agent.mjs").as_uri()),
        )
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **os.environ,
                "GEMINI_API_KEY": "dummy-gemini",
                "DEEPSEEK_API_KEY": "dummy-deepseek",
                "VISION_AUDIT_OUTPUT_DIR": directory,
            }
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_vision_delivery_rejects_empty_channel(self):
        argv = [
            "notify_vision_slack.py",
            "--report", "missing.json",
            "--channel", "   ",
            "--require-delivery",
        ]
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "test"}, clear=True):
            with mock.patch("sys.argv", argv):
                self.assertEqual(SLACK.main(), 1)

    def test_ragas_completion_requires_current_run_reports_and_eval_steps(self):
        workflow = (ROOT / ".github/workflows/ragas-nightly.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertNotIn("runs-on: [self-hosted", workflow)
        self.assertIn("DEEPSEEK_API_KEY", workflow)
        self.assertNotIn("OLLAMA_BASE_URL", workflow)
        self.assertNotIn("LOCAL_LLM_BASE_URL", workflow)
        self.assertIn("ragas-giskard-start-ns", workflow)
        self.assertIn("Stale report from an earlier run", workflow)
        self.assertIn("RAGAS_EVAL: ${{ steps.ragas_eval.outcome }}", workflow)
        self.assertIn("GISKARD_EVAL: ${{ steps.giskard_eval.outcome }}", workflow)
        self.assertIn("GISKARD_SCAN: ${{ steps.giskard_scan.outcome }}", workflow)
        self.assertIn("does not match quality verdict", workflow)
        scan = (ROOT / "eval/giskard_scan.py").read_text(encoding="utf-8")
        self.assertIn(
            "by_cat: dict[str, int] = {c: 0 for c in SCAN_CATEGORIES}\n"
            "    enumeration_complete = True\n"
            "    try:",
            scan,
        )
        rotating = (ROOT / "eval/rotating_llm.py").read_text(encoding="utf-8")
        self.assertIn('"X-QA-Run-ID"', rotating)
        self.assertIn('"https://api.deepseek.com/v1"', rotating)
        self.assertIn('"sentence-transformers/all-MiniLM-L6-v2"', rotating)
        self.assertIn("class CpuEmbedding", rotating)
        self.assertIn("default_headers=lifecycle_headers", rotating)

    def test_pipeline_summary_links_full_report_and_states_scope(self):
        payload = PIPELINE_SLACK.build_payload(
            channel="C123",
            pipeline="Daily Design Check",
            run_url="https://github.com/o/r/actions/runs/1",
            passed=9, failed=0, flaky=0, skipped=0,
            dashboard_url="https://o.github.io/r/design-check/runs/7/",
            dashboard_label="Open full report",
            scope="Layout guard only.",
            run_label="GitHub run",
        )
        blocks = payload["attachments"][0]["blocks"]
        self.assertEqual(blocks[1]["type"], "context")
        self.assertEqual(blocks[1]["elements"][0]["text"], "Layout guard only.")
        buttons = blocks[-1]["elements"]
        self.assertEqual(
            [(b["text"]["text"], b["url"]) for b in buttons],
            [
                ("Open full report", "https://o.github.io/r/design-check/runs/7/"),
                ("GitHub run", "https://github.com/o/r/actions/runs/1"),
            ],
        )

    def test_pipeline_summary_without_report_keeps_run_button_only(self):
        payload = PIPELINE_SLACK.build_payload(
            channel="C123", pipeline="unit", run_url="https://run",
            passed=1, failed=0, flaky=0, skipped=0,
        )
        blocks = payload["attachments"][0]["blocks"]
        self.assertNotIn("context", [b["type"] for b in blocks])
        self.assertEqual(
            [b["text"]["text"] for b in blocks[-1]["elements"]], ["View run"]
        )

    def test_slack_delivery_cannot_pass_without_a_token(self):
        argv = ["notify_slack.py", "--channel", "C123", "--pipeline", "unit", "--require-delivery"]
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.argv", argv):
            with self.assertRaises(SystemExit) as raised:
                PIPELINE_SLACK.main()
        self.assertEqual(raised.exception.code, 1)

    def test_observability_delivery_rejects_slack_api_failure(self):
        argv = [
            "notify_observability_slack.py",
            "--mode", "langfuse",
            "--channel", "C123",
            "--require-delivery",
        ]
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "test"}, clear=True):
            with mock.patch("sys.argv", argv), mock.patch.object(
                OBS_SLACK,
                "slack_api",
                side_effect=[{}, RuntimeError("not acknowledged")],
            ):
                self.assertEqual(OBS_SLACK.main(), 1)

    def test_bosgame_require_delivery_rejects_unacknowledged_post(self):
        argv = [
            "notify_bosgame_run_slack.py",
            "--suite", "unit",
            "--event", "result",
            "--format", "none",
            "--channel", "C123",
            "--require-delivery",
        ]
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "test"}, clear=True):
            with mock.patch("sys.argv", argv), mock.patch.object(
                BOSGAME_SLACK,
                "_post",
                return_value=None,
            ):
                self.assertEqual(BOSGAME_SLACK.main(), 1)

    def test_llm_quality_rejects_skips_and_stale_verdicts(self):
        workflow = (ROOT / ".github/workflows/llm-quality.yml").read_text(encoding="utf-8")
        self.assertIn("DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}", workflow)
        self.assertIn("python3 .github/scripts/check_cloud_judge.py", workflow)
        self.assertIn("llm-quality-start-ns", workflow)
        self.assertIn('statuses = {"expected", "unexpected", "flaky"}', workflow)
        self.assertIn("No current-run judge verdict files were created", workflow)
        self.assertIn("complete report quality verdict", workflow)

        promptfoo = (ROOT / ".github/workflows/promptfoo-basic.yml").read_text(encoding="utf-8")
        self.assertIn("PROMPTFOO_FAILED_TEST_EXIT_CODE: '0'", promptfoo)
        self.assertIn("steps.eval.outcome == 'success'", promptfoo)

    def test_cloud_eval_workflows_use_the_deepseek_secret(self):
        for name in (
            "agent-observability.yml",
            "llm-quality.yml",
            "promptfoo-basic.yml",
            "weekly-qa-report.yml",
        ):
            workflow = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            self.assertIn("DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}", workflow)
            self.assertNotIn("LOCAL_LLM_BASE_URL", workflow)

    def test_runtime_workflows_do_not_reference_openrouter(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("OPENROUTER", path.read_text(encoding="utf-8"), path.name)

    def test_weekly_report_uses_cloud_judge(self):
        workflow = (ROOT / ".github/workflows/weekly-qa-report.yml").read_text(encoding="utf-8")
        script = (ROOT / ".github/scripts/weekly_report.py").read_text(encoding="utf-8")
        combined = workflow + script
        self.assertIn("https://api.deepseek.com/v1", combined)
        self.assertIn("DEEPSEEK_API_KEY", combined)
        self.assertNotIn("LOCAL_LLM_BASE_URL", combined)

    def test_llm_judge_site_keeps_only_latest_retry_per_test(self):
        records = [
            {"testFile": "a.spec.ts", "titlePath": ["suite", "case"], "retry": 0, "prompt": "old"},
            {"testFile": "a.spec.ts", "titlePath": ["suite", "case"], "retry": 1, "prompt": "new-1"},
            {"testFile": "a.spec.ts", "titlePath": ["suite", "case"], "retry": 1, "prompt": "new-2"},
            {"testFile": "b.spec.ts", "titlePath": ["case"], "retry": 0, "prompt": "other"},
        ]
        tests = [
            {"path": ["suite"], "title": "case", "retry": 1},
            {"path": [], "title": "case", "retry": 0},
        ]
        filtered = LLM_SITE.final_attempt_verdicts(records, tests)
        self.assertEqual([record["prompt"] for record in filtered], ["new-1", "new-2", "other"])

    def test_html_documents_evidence_and_cloud_model_provenance(self):
        html = SITE.run_html(sample_report())
        self.assertIn("VISION-001", html)
        self.assertIn("Verify initial render.", html)
        self.assertIn("No concrete defect.", html)
        self.assertIn("The page rendered correctly.", html)
        self.assertIn("What does a smoke test verify?", html)
        self.assertIn("It verifies that critical functionality is available.", html)
        self.assertIn("screenshots/step-01.png", html)
        self.assertIn("https://api.deepseek.com", html)
        self.assertIn("cloud evaluator calls: <strong>1</strong>", html)
        self.assertIn("API requests: <strong>2</strong>", html)
        self.assertIn("Gemini HTTP 403", html)
        self.assertIn("DeepSeek", html)
        self.assertIn("production systems under test", html)

    def test_slack_thread_has_case_details_and_public_screenshot(self):
        step = sample_report()["steps"][0]
        blocks = SLACK.test_case_blocks(
            step,
            "https://qa-apps.github.io/PW_alexpavsky/vision-audit/runs/510/",
        )
        rendered = str(blocks)
        self.assertIn("VISION-001", rendered)
        self.assertIn("Verify initial render.", rendered)
        self.assertIn("Expected", rendered)
        self.assertIn("DeepSeek analysis", rendered)
        self.assertIn("Gemini HTTP 403", rendered)
        self.assertIn("What does a smoke test verify?", rendered)
        self.assertIn("It verifies that critical functionality is available.", rendered)
        self.assertIn("scroll:down", rendered)
        self.assertIn("runs/510/screenshots/step-01.png", rendered)

    def test_slack_upload_completion_targets_the_audit_thread(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as screenshot:
            screenshot.write(b"png")
            screenshot.flush()
            with mock.patch.object(
                SLACK,
                "slack_post",
                side_effect=[
                    {"ok": True, "upload_url": "https://upload.slack.test", "file_id": "F1"},
                    {"ok": True, "files": [{"permalink": "https://slack.test/F1"}]},
                ],
            ) as slack_post, mock.patch.object(SLACK.urllib.request, "urlopen"):
                link = SLACK.upload_file(
                    "token", "C123", screenshot.name, "VISION-001", "1717.0001"
                )

        self.assertEqual(link, "https://slack.test/F1")
        completion = slack_post.call_args_list[1].args[2]
        self.assertEqual(completion["channel_id"], "C123")
        self.assertEqual(completion["thread_ts"], "1717.0001")

    def test_dashboard_run_still_uploads_screenshot_and_video_to_slack(self):
        report = sample_report()
        report["video"] = "/runner/vision-audit/session.webm"
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            argv = [
                "notify_vision_slack.py",
                "--report", str(report_path),
                "--channel", "C123",
                "--require-delivery",
            ]
            with mock.patch.dict(os.environ, {
                "SLACK_BOT_TOKEN": "test",
                "VISION_AUDIT_DASHBOARD_URL": "https://example.test/audit/",
            }, clear=True), mock.patch("sys.argv", argv), mock.patch.object(
                SLACK,
                "slack_post",
                side_effect=[{"ok": True, "ts": "1717.0001"}, {"ok": True}],
            ), mock.patch.object(
                SLACK,
                "upload_file",
                side_effect=["https://slack.test/screenshot", "https://slack.test/video"],
            ) as upload:
                self.assertEqual(SLACK.main(), 0)

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(upload.call_args_list[0].args[-1], "1717.0001")
        self.assertEqual(upload.call_args_list[1].args[-1], "1717.0001")


if __name__ == "__main__":
    unittest.main()
