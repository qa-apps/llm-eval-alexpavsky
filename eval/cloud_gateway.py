#!/usr/bin/env python3
"""Small OpenAI-compatible gateway for scheduled evaluations.

The QA route mode uses OpenCode Go first and direct DeepSeek second. The
default route mode retains the original free-tier ladder for other callers.
The gateway never substitutes an empty answer for a provider failure.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock


ROUTES = (
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", "gpt-oss-120b"),
    ("sambanova", "SAMBANOVA_API_KEY", "https://api.sambanova.ai/v1", "Meta-Llama-3.3-70B-Instruct"),
    ("nvidia", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3.5-lightning-30b-a3b"),
    ("opencode-go", "OPENCODE_API_KEY", "https://opencode.ai/zen/go/v1", "glm-5.3-flash"),
    ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash"),
)
VISION_ROUTES = (
    ("nvidia-vision", "NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1", "meta/llama-3.2-11b-vision-instruct"),
    ("opencode-go-vision", "OPENCODE_API_KEY", "https://opencode.ai/zen/go/v1", "glm-5.3-flash"),
    ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-flash"),
)
LEDGER = Path(os.environ.get("CLOUD_EVAL_LEDGER", "cloud-eval-ledger.jsonl"))
LEDGER_LOCK = Lock()
EMBEDDING_LOCK = Lock()
EMBEDDING_MODEL = None
ROUTE_DISABLED_UNTIL = {}


def configured_routes(vision=False):
    source = VISION_ROUTES if vision else ROUTES
    if os.environ.get("CLOUD_EVAL_ROUTE_MODE") == "go-primary":
        preferred = ("opencode-go-vision", "deepseek") if vision else ("opencode-go", "deepseek")
        source = tuple(route for route in source if route[0] in preferred)
    elif vision:
        source = tuple(route for route in source if route[0] != "opencode-go-vision")
    return [(name, os.environ[key], url, model) for name, key, url, model in source if os.environ.get(key)]


def has_image(payload):
    return any(
        isinstance(message.get("content"), list)
        and any(part.get("type") == "image_url" for part in message["content"] if isinstance(part, dict))
        for message in payload.get("messages", [])
    )


def chat(payload: dict, routes=None) -> dict:
    routes = configured_routes(has_image(payload)) if routes is None else routes
    if not routes:
        raise RuntimeError("No cloud evaluation provider key is configured")
    errors = []
    for name, key, base_url, model in routes:
        if ROUTE_DISABLED_UNTIL.get(name, 0) > time.time():
            errors.append(f"{name}: cooldown")
            continue
        started = time.monotonic()
        request_payload = {**payload, "model": model, "stream": False}
        request_payload.pop("reasoning_effort", None)
        request_payload.pop("think", None)
        request_payload.pop("keep_alive", None)
        request_payload.pop("num_predict", None)
        if name.startswith("opencode-go") or name == "deepseek":
            request_payload.pop("temperature", None)
        if name == "nvidia":
            request_payload["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if name.startswith("opencode-go"):
            headers["x-opencode-session"] = f"cloud-eval-{os.environ.get('GITHUB_RUN_ID', os.getpid())}"
            headers["User-Agent"] = "PW-cloud-eval/1.0"
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(request_payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.load(response)
            if not result.get("choices"):
                raise ValueError("response has no choices")
            message = result["choices"][0].get("message") or {}
            if not message.get("content") and not message.get("tool_calls"):
                raise ValueError("response has no content or tool calls")
            if (payload.get("response_format") or {}).get("type") == "json_object":
                json.loads(message.get("content") or "")
            usage = result.get("usage") or {}
            entry = {
                "provider": name, "model": model,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "status": "ok",
            }
            with LEDGER_LOCK:
                with LEDGER.open("a", encoding="utf-8") as ledger:
                    ledger.write(json.dumps(entry) + "\n")
            print(f"cloud-eval: {name}/{model} {entry['latency_ms']}ms", flush=True)
            result["model"] = f"{name}/{model}"
            return result
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            if code in (401, 402, 403, 404, 410, 429):
                ROUTE_DISABLED_UNTIL[name] = time.time() + (60 if code == 429 else 86400)
            errors.append(f"{name}: {code}")
            print(f"cloud-eval: {name} unavailable ({code}); rotating", flush=True)
    raise RuntimeError("All configured cloud providers failed: " + ", ".join(errors))


class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/v1/models"):
            if self.path == "/health":
                self.reply(200, {"ready": bool(configured_routes()), "providers": [route[0] for route in configured_routes()]})
            else:
                self.reply(200, {"object": "list", "data": [{"id": "cloud-eval", "object": "model"}]})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/api/embed"):
            self.reply(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 48_000_000:
                raise ValueError("request is too large")
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/embed":
                global EMBEDDING_MODEL
                with EMBEDDING_LOCK:
                    if EMBEDDING_MODEL is None:
                        from sentence_transformers import SentenceTransformer
                        EMBEDDING_MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
                texts = payload.get("input", [])
                if isinstance(texts, str):
                    texts = [texts]
                result = {"embeddings": EMBEDDING_MODEL.encode(texts).tolist()}
            else:
                result = chat(payload)
            self.reply(200, result)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            self.reply(502, {"error": {"message": str(exc), "type": "provider_error"}})


if __name__ == "__main__":
    if "--probe-all" in sys.argv:
        routes = configured_routes()
        if not routes:
            raise SystemExit("No cloud provider keys configured")
        for route in routes:
            try:
                chat({"messages": [{"role": "user", "content": "Reply OK"}], "max_tokens": 128}, [route])
            except RuntimeError as exc:
                print(f"probe failed: {exc}", flush=True)
        sys.exit(0)
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("CLOUD_EVAL_PORT", "18765"))), Handler)
    print(f"cloud-eval: listening on {server.server_address[1]}", flush=True)
    server.serve_forever()
