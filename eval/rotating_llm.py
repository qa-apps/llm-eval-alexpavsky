"""LangChain judge adapter for the shared free-tier cloud gateway."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI
from pydantic import ConfigDict, Field

log = logging.getLogger("rotating-llm")


def lifecycle_headers(model: str) -> dict[str, str]:
    return {"X-Eval-Run-ID": os.environ.get("GITHUB_RUN_ID", "local-rag-eval")}


def build_provider_list() -> list[dict[str, str]]:
    """Build the gateway provider; rotation happens per request in the gateway."""
    return [{
        "name": "cloud-eval",
        "api_key": os.environ.get("CLOUD_EVAL_API_KEY", "cloud-eval"),
        "base_url": os.environ.get("CLOUD_EVAL_BASE_URL", "http://127.0.0.1:18765/v1").rstrip("/"),
        "model": "cloud-eval",
    }]


# Errors that should trigger rotation (rate limit, quota, auth, etc.)
ROTATE_ON_PATTERNS = (
    "rate", "429", "quota", "limit", "credit", "exhaust",
    "402", "401", "insufficient", "out of tokens", "tpd", "rpd", "tpm",
)

PRIORITY_INTERRUPTION_PATTERNS = (
    "503", "service unavailable", "connection reset", "peer closed",
    "remote protocol", "connection refused",
)


class RotatingJudgeLLM(BaseChatModel):
    """LangChain ChatModel that rotates through providers on failure.

    Used as a drop-in replacement for ChatOpenAI in Ragas. Each `_generate`
    call tries providers in `providers` order, switching on rate-limit /
    quota errors, until one succeeds.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    providers: list[dict[str, str]] = Field(default_factory=list)
    temperature: float = 0.0
    timeout: int = int(os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600"))
    max_retries: int = 1
    _last_used_idx: int = 0

    @property
    def _llm_type(self) -> str:
        return "rotating-judge"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "providers": [f"{p['name']}/{p['model']}" for p in self.providers],
            "temperature": self.temperature,
        }

    def _make_client(self, provider: dict[str, str]) -> ChatOpenAI:
        """Construct a fresh ChatOpenAI client for a provider."""
        max_tokens = max(1, int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "2048")))
        return ChatOpenAI(
            model=provider["model"],
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            default_headers=lifecycle_headers(provider["model"]),
            temperature=self.temperature,
            max_tokens=max_tokens,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def _should_rotate(self, err: Exception) -> bool:
        msg = str(err).lower()
        return any(pat in msg for pat in ROTATE_ON_PATTERNS)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        if not self.providers:
            raise RuntimeError("RotatingJudgeLLM has no providers configured")

        errors: list[str] = []
        n = len(self.providers)
        # Start from the last successful provider to minimize switching cost.
        for offset in range(n):
            idx = (self._last_used_idx + offset) % n
            provider = self.providers[idx]
            priority_wait = max(0, int(os.environ.get("LOCAL_LLM_PRIORITY_MAX_WAIT_SEC", "0")))
            deadline = time.monotonic() + priority_wait
            while True:
                try:
                    client = self._make_client(provider)
                    result = client._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
                    self._last_used_idx = idx
                    return result
                except Exception as e:
                    text = str(e).lower()
                    interrupted = any(pattern in text for pattern in PRIORITY_INTERRUPTION_PATTERNS)
                    local_busy = n == 1 and self._should_rotate(e)
                    if (interrupted or local_busy) and time.monotonic() < deadline:
                        log.info(
                            "Local judge is busy or still finishing an earlier request; "
                            "retrying in 10 seconds"
                        )
                        time.sleep(min(10.0, max(0.0, deadline - time.monotonic())))
                        continue
                    err_msg = f"{provider['name']}/{provider['model']}: {type(e).__name__}: {str(e)[:140]}"
                    errors.append(err_msg)
                    if self._should_rotate(e):
                        log.warning("Rotating away from %s — %s", provider["name"], type(e).__name__)
                        break
                    log.warning("Hard error from %s: %s", provider["name"], type(e).__name__)
                    break

        raise RuntimeError(
            f"All {n} judge providers failed:\n  " + "\n  ".join(errors)
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Sync fallback — Ragas's metrics call async paths, this keeps things simple.
        import asyncio
        return await asyncio.to_thread(self._generate, messages, stop, None, **kwargs)


# Map our provider name to LiteLLM's Ollama model id format.
def _litellm_model_id(provider: dict[str, str]) -> Optional[str]:
    name = provider["name"]
    model = provider["model"]
    if name == "cloud-eval":
        return f"openai/{model}"
    return None


def configure_giskard(providers: list[dict[str, str]], log_fn=print) -> str:
    """Point Giskard's text judge and CPU embeddings at the gateway."""
    import giskard
    import numpy as np
    import openai
    import requests
    from giskard.llm.client.openai import OpenAIClient
    from giskard.llm.embeddings import BaseEmbedding, set_default_embedding

    provider = providers[0]
    embedding_root = provider["base_url"].removesuffix("/v1")
    os.environ["LITELLM_REQUEST_TIMEOUT"] = os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600")

    litellm_ids = [m for m in (_litellm_model_id(p) for p in providers) if m]
    if not litellm_ids:
        raise RuntimeError("No provider in list maps to a LiteLLM-supported id")

    primary = litellm_ids[0]
    # The native OpenAI-compatible client preserves JSON response mode more
    # reliably than Giskard's LiteLLM adapter for GPT-OSS on Ollama.
    openai_client = openai.OpenAI(
        base_url=provider["base_url"],
        api_key=provider["api_key"],
        default_headers=lifecycle_headers(provider["model"]),
        timeout=int(os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600")),
        max_retries=8,
    )
    giskard.llm.set_default_client(
        OpenAIClient(model=provider["model"], client=openai_client, json_mode=True)
    )
    log_fn(f"  Judge: {provider['model']} via {provider['base_url']}")

    embedding_model = "sentence-transformers/all-MiniLM-L6-v2"

    class OllamaOpenAIEmbedding(BaseEmbedding):
        def embed(self, texts):
            response = requests.post(
                f"{embedding_root}/api/embed",
                headers={
                    "Authorization": f"Bearer {provider['api_key']}",
                    **lifecycle_headers(embedding_model),
                },
                json={"model": embedding_model, "input": list(texts), "keep_alive": -1},
                timeout=int(os.environ.get("LOCAL_LLM_TIMEOUT_SEC", "600")),
            )
            response.raise_for_status()
            return np.asarray(response.json()["embeddings"], dtype=np.float32)

    # Giskard 2.16 resets a custom default when no model name is registered.
    # Register a marker first, then install the direct adapter it will reuse.
    giskard.llm.set_embedding_model(f"local/{embedding_model}")
    set_default_embedding(OllamaOpenAIEmbedding())
    log_fn(f"  Embeddings: {embedding_model} via {embedding_root}/api/embed")

    return f"cloud/{provider['model']}"
