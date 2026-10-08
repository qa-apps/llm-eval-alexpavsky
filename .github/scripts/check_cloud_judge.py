#!/usr/bin/env python3
"""Fail a QA job clearly if its DeepSeek judge key or API is unavailable."""

import json
import os
import sys
import time
import urllib.error
import urllib.request


def main() -> int:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("::error::DEEPSEEK_API_KEY repository secret is required")
        return 1
    body = json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "Reply only OK."}],
        "max_tokens": 128,
        "reasoning_effort": "low",
        "stream": False,
    }).encode()
    request = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        content = str(data["choices"][0]["message"].get("content") or "").strip()
        if not content:
            raise ValueError("empty judge response")
        usage = data.get("usage") or {}
        print(
            "DeepSeek judge ready: model=deepseek-v4-pro "
            f"latency_s={time.monotonic() - started:.2f} "
            f"input_tokens={usage.get('prompt_tokens', '?')} "
            f"output_tokens={usage.get('completion_tokens', '?')}"
        )
        return 0
    except urllib.error.HTTPError as error:
        print(f"::error::DeepSeek judge HTTP {error.code}")
    except Exception as error:  # noqa: BLE001 — CLI preflight reports a useful failure
        print(f"::error::DeepSeek judge unavailable: {type(error).__name__}: {error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
