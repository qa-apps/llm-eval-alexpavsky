#!/usr/bin/env python3
"""Post an operational failure without misreporting skipped tests as passes."""
import argparse
import json
import os
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--detail", required=True)
    args = parser.parse_args()
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token or not args.channel:
        raise SystemExit("Slack bot token and channel are required")
    run_url = os.environ.get("GITHUB_RUN_URL", "")
    text = f":red_circle: {args.suite} did not finish. {args.detail}"
    if run_url:
        text += f"\n<{run_url}|Open workflow run>"
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": args.channel, "text": text, "unfurl_links": False}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise SystemExit(f"Slack rejected operational report: {result.get('error')}")


if __name__ == "__main__":
    main()
