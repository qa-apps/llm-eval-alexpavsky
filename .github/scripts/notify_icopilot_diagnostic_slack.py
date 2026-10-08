#!/usr/bin/env python3
"""Post the daily I-Copilot full workflow diagnostic to Slack (#ico-pilot).

The diagnostic itself runs on the i-Copilot server (systemd timer
ico-pilot-diagnostic, script .audit/i-copilot/full_workflow_diagnostic.py)
and publishes latest.json + an HTML report under
/static/eval-reports/diagnostics/. This script only reads that JSON.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

ICON = {"PASS": "✅", "WARN": "⚠️", "FAIL": "🔴", "INFO": "ℹ️"}


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "PW-alexpavsky-ico-pilot/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def slack(method: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_payload(channel: str, data: dict, index_url: str, run_url: str) -> dict:
    run = data.get("run", {})
    counts = data.get("counts", {})
    started = dt.datetime.fromisoformat(run.get("started_at", "1970-01-01T00:00:00+00:00"))
    age_h = (dt.datetime.now(dt.timezone.utc) - started).total_seconds() / 3600
    stale = age_h > 30
    failed = counts.get("FAIL", 0)
    status = "🔴 FAILED" if failed or stale else ("⚠️ PASSED with warnings" if counts.get("WARN") else "✅ PASSED")
    title = f"I-Copilot full diagnostic — {status}"
    summary = (
        f"*{counts.get('PASS', 0)}* passed · *{counts.get('WARN', 0)}* warnings · *{failed}* failed · "
        f"run {started.strftime('%Y-%m-%d %H:%M')} UTC ({run.get('duration_s', '?')} s)"
    )
    if stale:
        summary += f"\n🔴 *The diagnostic has not run for {int(age_h)} h* — check the `ico-pilot-diagnostic.timer` on the i-Copilot server."

    devices = data.get("devices", {})
    device_lines = [
        f"• *{name}*: last connected {info.get('age', '?')}" + (f" · output `{info['output']}`" if info.get("output") else "")
        for name, info in devices.items()
    ]
    voice_lines = []
    for v in data.get("voice", []):
        if v.get("expected"):
            hint = f"hint on iPad in {v['suggestion_s']} s" if v.get("suggestion_s") is not None else "no hint"
            peak = float((v.get("levels") or {}).get("audio_peak_rms") or 0)
            voice_lines.append(
                f"• *{v['test']}*: heard {int(float(v.get('recall', 0)) * 100)}% of words · level {peak:.3f} · {hint}"
            )
        else:
            voice_lines.append(f"• *{v['test']}*: {'quiet ✅' if not v.get('transcript') else 'text appeared 🔴'}")

    problems = [c for c in data.get("checks", []) if c.get("status") in ("FAIL", "WARN")]
    problem_lines = [f"{ICON[c['status']]} *{c['group']} / {c['name']}* — {c['detail']}" for c in problems[:12]]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "section", "text": {"type": "mrkdwn",
                                      "text": "*Devices (real)*\n" + ("\n".join(device_lines) or "no data")}},
        {"type": "section", "text": {"type": "mrkdwn",
                                      "text": "*Voice ping (simulated MacBook → server → iPad)*\n" + ("\n".join(voice_lines) or "skipped")}},
    ]
    groups: dict[str, list[str]] = {}
    for c in data.get("checks", []):
        groups.setdefault(c["group"], []).append(ICON.get(c["status"], "•"))
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*All checks*\n" + "\n".join(
        f"{''.join(marks)}  {group}" for group, marks in groups.items())}})
    if problem_lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                    "text": "*Needs attention*\n" + "\n".join(problem_lines)[:2900]}})
    buttons = []
    if run.get("report_url"):
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Open full report", "emoji": True},
                        "url": run["report_url"], "style": "danger" if failed or stale else "primary"})
    buttons.append({"type": "button", "text": {"type": "plain_text", "text": "All diagnostics", "emoji": True},
                    "url": index_url})
    if run_url:
        buttons.append({"type": "button", "text": {"type": "plain_text", "text": "View run", "emoji": True},
                        "url": run_url})
    blocks.append({"type": "actions", "elements": buttons})
    return {
        "channel": channel,
        "text": f"{title}: {counts.get('PASS', 0)} passed, {counts.get('WARN', 0)} warnings, {failed} failed",
        "blocks": blocks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--base-url", default=os.environ.get("ICO_PILOT_DIAGNOSTICS_REPORT_URL", ""))
    args = parser.parse_args()
    if not args.base_url:
        raise SystemExit("ICO_PILOT_DIAGNOSTICS_REPORT_URL or --base-url is required")
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    base = args.base_url.rstrip("/")
    try:
        data = fetch(f"{base}/latest.json")
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        data = {"run": {"started_at": "1970-01-01T00:00:00+00:00"}, "counts": {"FAIL": 1},
                "checks": [{"group": "Diagnostic", "name": "report download", "status": "FAIL",
                            "detail": f"could not read {base}/latest.json: {exc}"}]}
    payload = build_payload(args.channel, data, f"{base}/index.html", os.environ.get("GITHUB_RUN_URL", ""))
    if not token:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print("SLACK_BOT_TOKEN not set; printed the payload instead", file=sys.stderr)
        return 0
    try:
        slack("conversations.join", token, {"channel": args.channel})
    except Exception:
        pass
    resp = slack("chat.postMessage", token, payload)
    if not resp.get("ok"):
        print(f"Slack error: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"Posted I-Copilot diagnostic to {args.channel}")
    return 1 if data.get("counts", {}).get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
