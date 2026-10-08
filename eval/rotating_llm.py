"""Cloud LangChain judge for Ragas and Giskard evaluations."""
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
    """Tag requests with the QA run for provider-side diagnostics."""
    return {
        "X-QA-Run-ID": os.environ.get("GITHUB_RUN_ID", "local-rag-eval"),
    }


def build_provider_list() -> list[dict[str, str]]:
    """Prefer DeepSeek; Together is an optional independent cloud fallback."""
    providers: list[dict[str, str]] = []
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if deepseek_key:
        providers.append({
            "name": "deepseek", "api_key": deepseek_key,
            "base_url": "https://api.deepseek.com/v1",
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        })
    together_key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if together_key:
        providers.append({
            "name": "together", "api_key": together_key,
            "base_url": "https://api.together.xyz/v1",
            "model": os.environ.get("TOGETHER_MODEL", "deepseek-ai/DeepSeek-V4-Flash-0731"),
        })
    return providers


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
    timeout: int = int(os.environ.get("JUDGE_TIMEOUT_SEC", "120"))
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
        max_tokens = max(1, int(os.environ.get("JUDGE_MAX_TOKENS", "4096")))
        return ChatOpenAI(
            model=provider["model"],
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            default_headers=lifecycle_headers(provider["model"]),
            temperature=self.temperature,
            max_tokens=max_tokens,
            extra_body={"reasoning_effort": "low"} if provider["name"] == "deepseek" else {},
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
            priority_wait = max(0, int(os.environ.get("JUDGE_RETRY_WAIT_SEC", "0")))
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
                    if interrupted and time.monotonic() < deadline:
                        log.info(
                            "Judge provider is temporarily unavailable; "
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


def configure_giskard(providers: list[dict[str, str]], log_fn=print) -> str:
    """Use the cloud judge and a small CPU embedding model on the CI runner."""
    import giskard
    import numpy as np
    import openai
    from giskard.llm.client.openai import OpenAIClient
    from giskard.llm.embeddings import BaseEmbedding, set_default_embedding
    from sentence_transformers import SentenceTransformer

    provider = providers[0]
    embedding_model = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    embedder = SentenceTransformer(embedding_model)
    openai_client = openai.OpenAI(
        base_url=provider["base_url"],
        api_key=provider["api_key"],
        default_headers=lifecycle_headers(provider["model"]),
        timeout=int(os.environ.get("JUDGE_TIMEOUT_SEC", "120")),
        max_retries=2,
    )
    giskard.llm.set_default_client(
        OpenAIClient(model=provider["model"], client=openai_client, json_mode=True)
    )
    log_fn(f"  Judge: {provider['model']} via {provider['base_url']}")

    class CpuEmbedding(BaseEmbedding):
        def embed(self, texts):
            vectors = embedder.encode(list(texts), normalize_embeddings=True)
            return np.asarray(vectors, dtype=np.float32)

    # Giskard 2.16 resets a custom default when no model name is registered.
    # Register a marker first, then install the direct adapter it will reuse.
    giskard.llm.set_embedding_model(f"cpu/{embedding_model}")
    set_default_embedding(CpuEmbedding())
    log_fn(f"  Embeddings: {embedding_model} on CI CPU")

    return f"{provider['name']}/{provider['model']}"
