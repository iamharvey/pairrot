"""OpenAI-compatible chat client and an in-process fake for tests.

The only network implementation is Chat Completions. Lookups are asked for inside the
JSON answer, not through function calling. Tests use FakeModel and never open a socket.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict


class ModelError(RuntimeError):
    pass


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | None = None


class Completion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: ChatMessage
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


class LLM(Protocol):
    def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> Completion: ...


def _message_from_api(payload: dict[str, Any]) -> ChatMessage:
    content = payload.get("content")
    return ChatMessage(
        role=str(payload.get("role") or "assistant"),
        content=content if isinstance(content, str) else "",
    )


class OpenAICompatibleClient:
    """POST {base_url}/chat/completions. base_url, model, and key come from the caller."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.transport = transport

    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> Completion:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [{"role": item.role, "content": item.content or ""} for item in messages],
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.post(self.endpoint(), headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise ModelError(f"模型请求失败: {exc.__class__.__name__}") from exc
        except json.JSONDecodeError as exc:
            raise ModelError("模型响应不是 JSON") from exc
        try:
            message = _message_from_api(payload["choices"][0]["message"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("模型响应缺少 choices") from exc
        usage = payload.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost", usage.get("total_cost"))
        return Completion(
            message=message,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=float(cost or 0),
        )


class FakeModel:
    """Scripted model. `handler(messages, model) -> Completion`."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages: list[ChatMessage], *, model: str | None = None) -> Completion:
        self.calls.append({"messages": list(messages), "model": model})
        result = self.handler(messages, model)
        if not isinstance(result, Completion):
            raise TypeError("FakeModel handler must return Completion")
        return result


def client_from_env() -> OpenAICompatibleClient:
    base = os.environ.get("PAIRROT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("PAIRROT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("PAIRROT_MODEL") or os.environ.get("OPENAI_MODEL")
    missing = [
        name
        for name, value in (
            ("PAIRROT_BASE_URL", base),
            ("PAIRROT_API_KEY", key),
            ("PAIRROT_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise ModelError(
            "缺少模型环境变量: "
            + ", ".join(missing)
            + "（也接受 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL）"
        )
    return OpenAICompatibleClient(base_url=base or "", api_key=key or "", model=model or "")

