#!/usr/bin/env python3
"""Publish an evidence-first Agentic Vision Audit report to Slack."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def slack_post(token: str, method: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def upload_file(
    token: str,
    channel: str,
    file_path: str,
    title: str,
    thread_ts: str = "",
) -> str:
    path = Path(file_path)
    if not path.is_file() or path.stat().st_size > MAX_UPLOAD_BYTES:
        return ""
    request = urllib.request.Request(
        "https://slack.com/api/files.getUploadURLExternal",
        data=urlencode({"filename": path.name, "length": path.stat().st_size}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        prepared = json.loads(response.read())
    if not prepared.get("ok"):
        print(f"Slack upload preparation failed: {prepared.get('error')}", file=sys.stderr)
        return ""
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    upload = urllib.request.Request(
        prepared["upload_url"], data=path.read_bytes(),
        headers={"Content-Type": content_type}, method="POST",
    )
    with urllib.request.urlopen(upload, timeout=60):
        pass
    completion = {
        "files": [{"id": prepared["file_id"], "title": title}],
        "channel_id": channel,
    }
    if thread_ts:
        completion["thread_ts"] = thread_ts
    completed = slack_post(token, "files.completeUploadExternal", completion)
    if not completed.get("ok"):
        print(f"Slack upload completion failed: {completed.get('error')}", file=sys.stderr)
        return ""
    files = completed.get("files") or []
    return files[0].get("permalink", "") if files else ""


def slack_text(value: object, limit: int = 900) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")[:limit]
    )


def screenshot_url(dashboard_url: str, screenshot: str) -> str:
    name = Path(str(screenshot or "")).name
    if not dashboard_url or not name:
        return ""
    return f"{dashboard_url.rstrip('/')}/screenshots/{quote(name)}"


def action_description(step: dict) -> tuple[str, str, str]:
    result = step.get("action_result") or {}
    actual = result.get("action") or result.get("rejected") or "not executed"
    checks = "; ".join(result.get("checks") or [])
    return str(actual), checks, "passed" if result.get("executed") else "failed"


def test_case_blocks(step: dict, dashboard_url: str) -> list[dict]:
    selected, checks, actual = action_description(step)
    case_id = step.get("test_case_id") or f"VISION-{int(step.get('step', 0)):03d}"
    verdict = str(step.get("verdict") or "unknown").upper()
    marker = ":white_check_mark:" if verdict == "PASSED" else ":x:"
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{case_id} - {verdict}"[:150]},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{marker} *Required browser journey + Vision review*\n"
                    f"*Case:* {slack_text(step.get('test_case_name') or case_id)}\n"
                    f"*Page:* {slack_text(step.get('title') or step.get('url'))}\n"
                    f"*URL:* {slack_text(step.get('url'))}"
                ),
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Objective*\n{slack_text(step.get('objective'))}"},
                {"type": "mrkdwn", "text": f"*Expected*\n{slack_text(step.get('expected_result'))}"},
                {"type": "mrkdwn", "text": f"*Actual*\n{slack_text(step.get('actual_result'))}"},
                {"type": "mrkdwn", "text": f"*Vision model latency*\n{step.get('model_latency_ms', 0)} ms"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Cloud Vision analysis*\n{slack_text(step.get('summary'))}\n\n"
                    f"*Browser action:* `{slack_text(selected, 250)}`\n"
                    f"*Checks:* {slack_text(checks or 'No completed assertions')}\n"
                    f"*Deterministic result:* `{slack_text(actual, 80)}`\n\n"
                    f"*Input*\n{slack_text(step.get('scenario_input') or 'No text input required.')}\n\n"
                    f"*Output*\n{slack_text(step.get('scenario_output') or 'See browser checks and screenshot.') }"
                ),
            },
        },
    ]
    image_url = screenshot_url(dashboard_url, step.get("screenshot", ""))
    if image_url:
        blocks.append({
            "type": "image",
            "image_url": image_url,
            "alt_text": f"Screenshot for {case_id}"[:200],
        })
    return blocks


def finding_lines(report: dict) -> list[str]:
    findings = list(report.get("deterministic_findings") or [])
    findings.extend(report.get("confirmed_findings") or [])
    if not findings:
        return ["No confirmed visual or functional defects."]
    lines = []
    for finding in findings[:8]:
        title = str(finding.get("title") or "Issue")[:160]
        evidence = str(finding.get("evidence") or "")[:260]
        severity = str(finding.get("severity") or "unknown").upper()
        lines.append(f"- [{severity}] {title}: {evidence}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--require-delivery", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channel = args.channel.strip()
    if not token or not channel:
        print("SLACK_BOT_TOKEN or channel is not configured; skipping Slack notification.")
        return 1 if args.require_delivery else 0

    report_path = Path(args.report)
    if not report_path.is_file():
        run_url = os.environ.get("GITHUB_RUN_URL", "")
        run_link = f"\n<{run_url}|Open GitHub run>" if run_url else ""
        result = slack_post(token, "chat.postMessage", {
            "channel": channel,
            "text": (
                ":x: *Daily Agentic Vision Audit - INFRASTRUCTURE FAILURE*\n"
                "The job stopped before the Vision report was created."
                f"{run_link}"
            ),
        })
        if not result.get("ok"):
            print(f"Slack message failed: {result.get('error')}", file=sys.stderr)
            return 1 if args.require_delivery else 0
        return 0

    report = json.loads(report_path.read_text())
    status = report.get("status", "failed")
    marker = ":white_check_mark:" if status == "passed" else ":x:"
    steps = report.get("steps") or []
    dashboard_url = os.environ.get("VISION_AUDIT_DASHBOARD_URL", "").strip()
    video = report.get("video") or ""

    run_url = os.environ.get("GITHUB_RUN_URL", "")
    usage = report.get("model_usage") or {}
    provenance = report.get("model_provenance") or {}
    run_link = f"<{run_url}|Open GitHub run>" if run_url else ""
    dashboard_link = f"<{dashboard_url}|Open full Daily Audit UI>" if dashboard_url else ""
    meaning = (
        "PASS means no calibrated defect was confirmed."
        if status == "passed"
        else "FAIL means at least one calibrated defect or infrastructure error was confirmed."
    )
    text = (
        f"{marker} *AlexPavsky Daily Audit - {status.upper()}*\n"
        f"_{meaning}_\n"
        f"*Model:* `{report.get('model', 'unknown')}`\n"
        f"*Audit evaluator:* `{provenance.get('execution', 'unknown')}` via "
        f"`{provenance.get('provider', 'unknown')}` at `{provenance.get('endpoint', 'unknown endpoint')}`\n"
        f"*Cloud evaluator calls:* *{provenance.get('cloud_llm_calls', 0)}*\n"
        "_Production AI Chat, Voice, and Challenge may use their own configured providers._\n"
        f"*Coverage:* {len(steps)} documented journeys, "
        f"{len(report.get('pages_observed') or [])} unique URLs\n"
        f"*Vision usage:* {usage.get('calls', 0)} calls, "
        f"{usage.get('prompt_tokens', 0)} input tokens, "
        f"{usage.get('completion_tokens', 0)} output tokens\n"
        f"*Candidate observations:* {len(report.get('candidate_findings') or [])} "
        f"(only calibrated findings can fail CI)\n"
        f"*Findings:*\n" + "\n".join(finding_lines(report)) + "\n"
        "*Evidence:* every screenshot is shown in this thread; the video and raw JSON are in the full report. "
        "\n"
        f"{' | '.join(link for link in (dashboard_link, run_link) if link)}\n"
        f"Every UI opening and AI input/output pair is documented in this thread."
    )
    result = slack_post(token, "chat.postMessage", {"channel": channel, "text": text})
    if not result.get("ok"):
        print(f"Slack message failed: {result.get('error')}", file=sys.stderr)
        return 1 if args.require_delivery else 0

    thread_ts = result.get("ts")
    if not thread_ts:
        return 1 if args.require_delivery else 0
    delivered = bool(dashboard_url)
    for step in steps:
        case_id = step.get("test_case_id") or f"step {step.get('step')}"
        thread_result = slack_post(token, "chat.postMessage", {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": f"{case_id}: {step.get('summary') or 'No model summary.'}",
            "blocks": test_case_blocks(step, dashboard_url),
        })
        if not thread_result.get("ok"):
            delivered = False
            print(
                f"Slack test-case message failed for {case_id}: {thread_result.get('error')}",
                file=sys.stderr,
            )

    if video and dashboard_url:
        video_url = f"{dashboard_url.rstrip('/')}/videos/{quote(Path(video).name)}"
        video_result = slack_post(token, "chat.postMessage", {
            "channel": channel,
            "thread_ts": thread_ts,
            "text": f"<{video_url}|Open Daily Audit session video>",
        })
        if not video_result.get("ok"):
            delivered = False
            print(f"Slack video link failed: {video_result.get('error')}", file=sys.stderr)
    return 0 if delivered or not args.require_delivery else 1


if __name__ == "__main__":
    raise SystemExit(main())
