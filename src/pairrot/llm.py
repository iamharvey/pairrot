"""OpenAI-compatible chat client and an in-process fake for tests.

The only network implementation is Chat Completions. Tests use FakeModel and
never open a socket.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ModelError(RuntimeError):
    pass


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class Completion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: ChatMessage
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


class LLM(Protocol):
    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
    ) -> Completion: ...


def _parse_arguments(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"_raw": raw}
    return {"_raw": str(raw)}


def _message_to_api(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content or "",
        }
    body: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.tool_calls:
        body["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in message.tool_calls
        ]
    return body


def _message_from_api(payload: dict[str, Any]) -> ChatMessage:
    calls = []
    for raw in payload.get("tool_calls") or []:
        function = raw.get("function") or {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=_parse_arguments(function.get("arguments")),
            )
        )
    content = payload.get("content")
    if not isinstance(content, str):
        content = None if calls else ""
    return ChatMessage(
        role=str(payload.get("role") or "assistant"),
        content=content,
        tool_calls=calls or None,
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

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [_message_to_api(item) for item in messages],
            "temperature": 0,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
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
    """Scripted model. `handler(messages, tools, model) -> Completion`."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
    ) -> Completion:
        self.calls.append({"messages": list(messages), "tools": tools, "model": model})
        result = self.handler(messages, tools, model)
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

