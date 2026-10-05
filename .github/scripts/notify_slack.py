#!/usr/bin/env python3
"""
Slack CI notification with emoji donut chart.
Usage: python notify_slack.py --channel CHANNEL_ID --pipeline PIPELINE_NAME [--results-dir DIR]
"""
import argparse
import glob
import json
import os
import sys
import urllib.request
import urllib.error


def load_results(results_dir: str, pattern: str = "*.json") -> dict:
    """Parse Playwright JSON results. Falls back to zeros if none found."""
    passed = failed = flaky = skipped = 0

    json_files = glob.glob(os.path.join(results_dir, pattern), recursive=True)
    # Also check for the aggregated results file produced by --reporter=json
    for path in json_files:
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Playwright JSON reporter top-level stats
        stats = data.get("stats", {})
        if stats:
            passed  += stats.get("expected", 0)
            failed  += stats.get("unexpected", 0)
            flaky   += stats.get("flaky", 0)
            skipped += stats.get("skipped", 0)
            continue

        # Also handle array of suites (some CI setups dump per-file JSON)
        suites = data.get("suites", [])
        for suite in suites:
            for spec in suite.get("specs", []):
                for test in spec.get("tests", []):
                    status = test.get("status", "")
                    if status == "expected":
                        passed += 1
                    elif status == "unexpected":
                        failed += 1
                    elif status == "flaky":
                        flaky += 1
                    elif status == "skipped":
                        skipped += 1

    return {"passed": passed, "failed": failed, "flaky": flaky, "skipped": skipped}


def build_donut(passed: int, failed: int, flaky: int, skipped: int, width: int = 20) -> str:
    """Build an emoji donut bar proportional to counts."""
    total = passed + failed + flaky + skipped
    if total == 0:
        return "⚪" * width + " (no results)"

    n_pass  = round(passed  / total * width)
    n_fail  = round(failed  / total * width)
    n_flaky = round(flaky   / total * width)
    n_skip  = width - n_pass - n_fail - n_flaky
    n_skip  = max(0, n_skip)

    return "🟢" * n_pass + "🔴" * n_fail + "🟠" * n_flaky + "⚪" * n_skip


def build_payload(channel: str, pipeline: str, run_url: str,
                  passed: int, failed: int, flaky: int, skipped: int,
                  dashboard_url: str = "", dashboard_label: str = "Open results UI",
                  scope: str = "", run_label: str = "View run") -> dict:
    total = passed + failed + flaky + skipped

    if total == 0:
        color = "#e9a820"
        status_emoji = "🟠"
        status_text = "NO RESULTS"
    elif failed > 0:
        color = "#cc2929"   # red
        status_emoji = "🔴"
        status_text = "FAILED"
    elif flaky > 0:
        color = "#e9a820"   # orange
        status_emoji = "🟠"
        status_text = "FLAKY"
    else:
        color = "#2eb886"   # green
        status_emoji = "✅"
        status_text = "PASSED"

    donut = build_donut(passed, failed, flaky, skipped)
    pct = f"{round(passed / total * 100)}%" if total else "—"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{status_emoji}  {pipeline}  —  {status_text}",
                "emoji": True
            }
        },
    ]
    if scope:
        # One line saying what this pipeline covers, so a green design check is
        # not read as "the whole site is fine".
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": scope}],
        })
    blocks += [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": donut
            }
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*✅ Passed*\n{passed}"},
                {"type": "mrkdwn", "text": f"*🔴 Failed*\n{failed}"},
                {"type": "mrkdwn", "text": f"*🟠 Flaky*\n{flaky}"},
                {"type": "mrkdwn", "text": f"*⏭️ Skipped*\n{skipped}"},
                {"type": "mrkdwn", "text": f"*📊 Total*\n{total}"},
                {"type": "mrkdwn", "text": f"*📈 Pass rate*\n{pct}"},
            ]
        },
    ]

    action_elements = []
    if dashboard_url:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": dashboard_label, "emoji": True},
            "url": dashboard_url,
            "style": "primary" if failed == 0 else "danger",
        })
    if run_url:
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": run_label, "emoji": True},
            "url": run_url,
        })
    if action_elements:
        blocks.append({
            "type": "actions",
            "elements": action_elements,
        })

    return {
        "channel": channel,
        "attachments": [
            {
                "color": color,
                "blocks": blocks,
                "fallback": f"{pipeline}: {passed} passed, {failed} failed, {flaky} flaky, {skipped} skipped"
            }
        ]
    }


def join_channel(token: str, channel: str) -> None:
    """Best-effort: try to join the channel before posting.

    The Slack bot is expected to be invited to each notification channel
    manually (so it works for private channels and channels in other
    workspaces too). conversations.join is a fallback for newly added
    public channels — failures here are non-fatal; chat.postMessage will
    surface the real problem if the bot truly can't post.
    """
    # Benign outcomes that we don't need to log:
    #  - method_not_supported_for_channel_type: private channel / DM / mpim
    #  - missing_scope: bot lacks channels:join, but is already invited
    #  - already_in_channel: nothing to do
    #  - is_archived / channel_not_found: chat.postMessage will report it
    SILENT_ERRORS = {
        "method_not_supported_for_channel_type",
        "missing_scope",
        "already_in_channel",
    }

    body = json.dumps({"channel": channel}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/conversations.join",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            err = result.get("error")
            if not result.get("ok") and err not in SILENT_ERRORS:
                print(f"conversations.join warning: {err}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"conversations.join HTTP error: {e}", file=sys.stderr)


def post_message(token: str, payload: dict) -> bool:
    """Post to Slack and return whether Slack acknowledged the message.

    Historical: this script used to `sys.exit(1)` on `channel_not_found`,
    `not_in_channel`, etc. That meant every Slack misconfiguration (stale
    channel ID, bot removed from channel, token rotated) would redden CI
    *even when the tests themselves were green*. The actual test results
    live in the GitHub Actions UI regardless of Slack delivery, so a
    notification failure is not a CI failure — it's an ops bug to fix
    separately, surfaced clearly in the logs.
    """
    channel = payload["channel"]
    join_channel(token, channel)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(f"✅ Message posted to {channel}")
                return True
            err = result.get("error", "unknown")
            print(f"⚠️  Slack delivery failed ({err}) for channel {channel}. "
                  f"CI status reflects test results, not Slack delivery — "
                  f"check the actual run for the real result. "
                  f"Fix the Slack side at: "
                  f"https://api.slack.com/apps (re-invite bot, refresh "
                  f"channel ID, or check scopes).", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"⚠️  Slack HTTP error ({e}) — non-fatal, see note above.",
              file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Post CI results to Slack")
    parser.add_argument("--channel",  required=True, help="Slack channel ID or name")
    parser.add_argument("--pipeline", required=True, help="Pipeline display name")
    parser.add_argument("--results-dir", default="test-results", help="Dir with Playwright JSON results")
    parser.add_argument("--passed",  type=int, default=None, help="Override passed count")
    parser.add_argument("--failed",  type=int, default=None, help="Override failed count")
    parser.add_argument("--flaky",   type=int, default=None, help="Override flaky count")
    parser.add_argument("--skipped", type=int, default=None, help="Override skipped count")
    parser.add_argument("--results-pattern", default="*.json", help="Glob under --results-dir to select result JSON files")
    parser.add_argument("--dashboard-url", default="", help="Optional web dashboard URL")
    parser.add_argument("--dashboard-label", default="Open results UI", help="Slack button label for dashboard URL")
    parser.add_argument("--scope", default="", help="Optional one-line description of what the pipeline covers")
    parser.add_argument("--run-label", default="View run", help="Slack button label for the GitHub Actions run")
    parser.add_argument(
        "--require-delivery",
        action="store_true",
        help="Exit non-zero unless Slack acknowledges the message",
    )
    args = parser.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("SLACK_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    run_url = os.environ.get("GITHUB_RUN_URL", "")
    if not run_url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if repo and run_id:
            run_url = f"https://github.com/{repo}/actions/runs/{run_id}"

    if args.passed is not None:
        stats = {
            "passed": args.passed,
            "failed": args.failed or 0,
            "flaky":  args.flaky  or 0,
            "skipped": args.skipped or 0,
        }
    else:
        stats = load_results(args.results_dir, args.results_pattern)

    payload = build_payload(
        channel=args.channel,
        pipeline=args.pipeline,
        run_url=run_url,
        dashboard_url=args.dashboard_url,
        dashboard_label=args.dashboard_label,
        scope=args.scope,
        run_label=args.run_label,
        **stats
    )
    delivered = post_message(token, payload)
    if args.require_delivery and not delivered:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
