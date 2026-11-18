#!/usr/bin/env python3
"""
notify_promptfoo_slack.py — post a promptfoo Daily Evaluation summary to Slack.

Mirrors notify_k6_slack.py: reads the JSON produced by
`promptfoo eval -o <file>.json`, builds a Slack Block-Kit message with a
pass/fail donut and per-test detail, and posts it via chat.postMessage.

Usage:
  python .github/scripts/notify_promptfoo_slack.py \
    --channel CHANNEL_ID --results promptfoo-results.json

Env:
  SLACK_BOT_TOKEN     Slack bot token (xoxb-...). If unset, the script no-ops.
  GITHUB_RUN_URL      Optional link surfaced as a "View run" button.
"""
import argparse
import json
import os
import sys
import urllib.request


def load_results(path: str) -> list[dict]:
    """Normalize promptfoo JSON into a flat [{label, description, passed}] list."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # promptfoo shape: { results: { results: [ {success, provider, testCase}... ] } }
    raw = (
        data.get("results", {}).get("results")
        if isinstance(data.get("results"), dict)
        else data.get("results")
    ) or []
    flat = []
    for r in raw:
        provider = r.get("provider")
        label = (provider or {}).get("label") if isinstance(provider, dict) else provider
        desc = (r.get("testCase") or {}).get("description") or (
            (r.get("vars") or {}).get("prompt", "")[:48]
        )
        flat.append({
            "label": label or "provider",
            "description": desc or "test",
            "passed": bool(r.get("success")),
            "error": r.get("error") or (r.get("response") or {}).get("error") or "",
        })
    return flat


def build_donut(passed: int, failed: int, errors: int = 0) -> str:
    total = passed + failed + errors
    if not total:
        return "—"
    filled = round((passed / total) * 10)
    failed_width = 10 - filled
    return f"`{'🟩' * filled}{'🟥' * failed_width}`  {passed}/{total} passed"


def build_payload(
    channel: str,
    run_url: str,
    results: list[dict],
    dashboard_url: str = "",
    local_ui_url: str = "",
) -> dict:
    passed = sum(1 for r in results if r["passed"])
    errors = sum(1 for r in results if r.get("error"))
    failed = len(results) - passed - errors
    total = len(results)

    if errors:
        color, icon, status = "#cc2929", "🔴", "ERROR"
    elif failed:
        color, icon, status = "#cc2929", "🔴", "FAILED"
    elif total:
        color, icon, status = "#2eb886", "✅", "PASSED"
    else:
        color, icon, status = "#e9a820", "🟠", "NO RESULTS"

    detail = []
    for r in results[:15]:
        mark = "✅" if r["passed"] else ("⚠️" if r.get("error") else "🔴")
        detail.append(f"{mark} *{r['description']}* · {r['label']}")

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"{icon}  promptfoo Daily Eval  —  {status}",
                                    "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": build_donut(passed, failed, errors)}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*✅ Passed*\n{passed}"},
            {"type": "mrkdwn", "text": f"*🔴 Failed*\n{failed}"},
            {"type": "mrkdwn", "text": f"*⚠️ Errors*\n{errors}"},
            {"type": "mrkdwn", "text": f"*📊 Total*\n{total}"},
        ]},
    ]
    first_error = next((r.get("error", "") for r in results if r.get("error")), "")
    if first_error:
        first_line = first_error.splitlines()[0][:280]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*First error:*\n`{first_line}`"}})
    if detail:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(detail)}})
    action_elements = []
    if dashboard_url:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Open Promptfoo GitHub UI", "emoji": True},
            "url": dashboard_url,
            "style": "primary" if failed == 0 and errors == 0 else "danger",
        })
    if local_ui_url:
        # Native `promptfoo view` on the Mac (launchd com.alexp.promptfoo-local-ui),
        # which imports CI results from GitHub Pages every 30 minutes.
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "Open Promptfoo Local UI", "emoji": True},
            "url": local_ui_url,
        })
    if run_url:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "View run", "emoji": True},
            "url": run_url,
        })
    if action_elements:
        blocks.append({"type": "actions", "elements": action_elements})

    return {
        "channel": channel,
        "attachments": [{
            "color": color,
            "blocks": blocks,
            "fallback": f"promptfoo Daily Eval: {passed}/{total} passed",
        }],
    }


def _slack_post(url: str, token: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True, help="Slack channel ID or name")
    parser.add_argument("--results", required=True, help="promptfoo JSON results file")
    parser.add_argument("--dashboard-url", default="", help="Optional Promptfoo web UI URL")
    parser.add_argument(
        "--require-delivery",
        action="store_true",
        help="Exit non-zero instead of silently skipping when Slack delivery is impossible",
    )
    parser.add_argument(
        "--local-ui-url",
        default=os.environ.get("PROMPTFOO_LOCAL_UI_URL", ""),
        help="Optional local `promptfoo view` URL; empty hides the button (Pages URL is the public Open UI)",
    )
    args = parser.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("SLACK_BOT_TOKEN not set; skipping Slack notification", file=sys.stderr)
        return 1 if args.require_delivery else 0

    if not os.path.exists(args.results):
        print(f"results file {args.results} missing; skipping", file=sys.stderr)
        return 1 if args.require_delivery else 0

    results = load_results(args.results)
    payload = build_payload(
        args.channel,
        os.environ.get("GITHUB_RUN_URL", ""),
        results,
        args.dashboard_url,
        args.local_ui_url,
    )

    # Best-effort auto-join, then post.
    try:
        _slack_post("https://slack.com/api/conversations.join", token,
                    {"channel": args.channel})
    except Exception:
        pass

    resp = _slack_post("https://slack.com/api/chat.postMessage", token, payload)
    if not resp.get("ok"):
        print(f"Slack post failed: {resp.get('error')}", file=sys.stderr)
        return 1
    print(f"Message posted to {args.channel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
