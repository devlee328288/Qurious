"""LLM 서빙 백엔드 추상화.

settings.LLM_PROVIDER 에 따라 채팅(생성)에 사용할 백엔드를 고른다:
  - ollama : 로컬 Ollama (기본값)
  - vllm   : vLLM 서버 (OpenAI 호환 /v1/chat/completions)

임베딩(embed)은 이 앱의 RAG/문서 파이프라인이 전부 nomic-embed-text 차원(768)에
맞춰져 있으므로 어떤 provider를 고르든 항상 로컬 Ollama로 위임한다 — vLLM 쪽
채팅 모델은 이 앱에서 임베딩 용도로 쓰지 않는다.

※ 강사님 원본의 AWS 백엔드(Bedrock·SageMaker)는 팀 비용 정책에 따라 제거했다 (2026-09-15).
"""
from __future__ import annotations

from typing import Protocol

import httpx

from app.config import settings
from app.lib.ollama import OllamaClient


class LLMClient(Protocol):
    async def chat(self, model: str, messages: list[dict], options: dict | None = None) -> str: ...
    async def embed(self, model: str, input_text: str) -> list[float]: ...


class _EmbedViaOllamaMixin:
    """embed()는 항상 로컬 Ollama(settings.EMBED_MODEL)로 위임."""

    def __init__(self) -> None:
        self._embed_client = OllamaClient(settings.OLLAMA_BASE_URL, settings.OLLAMA_TIMEOUT)

    async def embed(self, model: str, input_text: str) -> list[float]:
        return await self._embed_client.embed(settings.EMBED_MODEL, input_text)


class VLLMClient(_EmbedViaOllamaMixin):
    """vLLM 서버 — OpenAI 호환 /v1/chat/completions."""

    def __init__(self, base_url: str, model: str, timeout: float):
        super().__init__()
        if not base_url:
            raise RuntimeError("LLM_PROVIDER=vllm 인데 VLLM_BASE_URL이 설정되지 않았습니다.")
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def chat(self, model: str, messages: list[dict], options: dict | None = None) -> str:
        opts = options or {}
        payload = {
            "model": self._model or model,
            "messages": messages,
            "max_tokens": opts.get("num_predict", 1024),
            "temperature": opts.get("temperature", 0.7),
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base}/v1/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


_client_cache: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """settings.LLM_PROVIDER에 따라 채팅 백엔드 클라이언트를 반환 (프로세스당 1회 생성 후 캐시)."""
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    provider = settings.LLM_PROVIDER.lower()
    if provider == "vllm":
        _client_cache = VLLMClient(settings.VLLM_BASE_URL, settings.VLLM_MODEL, settings.OLLAMA_TIMEOUT)
    elif provider == "ollama":
        _client_cache = OllamaClient(settings.OLLAMA_BASE_URL, settings.OLLAMA_TIMEOUT)
    else:
        raise RuntimeError(f"알 수 없는 LLM_PROVIDER: {settings.LLM_PROVIDER!r} (ollama/vllm 중 하나)")
    return _client_cache
