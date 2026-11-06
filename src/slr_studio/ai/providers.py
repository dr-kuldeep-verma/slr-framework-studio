"""The three backends, over plain HTTP.

Raw requests rather than each vendor's SDK: three small adapters are easier to
reason about than three dependency trees that drift apart, and the packaged
Windows build stays smaller. Each adapter does one thing -- turn a system and
user prompt into text and a token count -- and maps transport failures onto
the shared error types so the client's retry logic does not care which
backend is in use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .base import (
    AuthenticationError,
    LLMResponse,
    Provider,
    ProviderError,
    RateLimitError,
    Usage,
)
from .keys import get_key

__all__ = ["PROVIDERS", "ProviderSpec", "build_provider", "available_providers"]

ANTHROPIC_VERSION = "2023-06-01"


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - bundled in the app
        raise ProviderError(
            "The httpx package is required to reach a language model."
        ) from exc
    return httpx


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float):
    httpx = _httpx()
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise ProviderError(f"The request timed out after {timeout:.0f}s.") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach the model: {exc}") from exc

    if response.status_code in (401, 403):
        raise AuthenticationError(
            "The API key was rejected. Check it under Tools → Model settings."
        )
    if response.status_code == 429:
        raise RateLimitError("Rate limited by the provider.")
    if response.status_code >= 500:
        raise ProviderError(f"The provider returned {response.status_code}.")
    if response.status_code >= 400:
        detail = response.text[:300]
        error = ProviderError(f"The provider rejected the request: {detail}")
        error.retryable = False
        raise error
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError("The provider returned a response that was not JSON.") from exc


class AnthropicProvider(Provider):
    """Claude via the Anthropic Messages API. The default."""

    name = "anthropic"
    sends_data_off_device = True

    def __init__(self, model: str, *, api_key: str | None = None,
                 base_url: str = "https://api.anthropic.com", **options: Any) -> None:
        super().__init__(model, **options)
        self.api_key = api_key or get_key("anthropic")
        self.base_url = base_url.rstrip("/")

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.0, timeout: float = 120.0) -> LLMResponse:
        if not self.api_key:
            raise AuthenticationError(
                "No Anthropic API key is stored. Add one under "
                "Tools → Model settings, or set ANTHROPIC_API_KEY."
            )
        data = _post(
            f"{self.base_url}/v1/messages",
            {
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout,
        )
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model", self.model),
            model_version=data.get("model", self.model),
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
            ),
            stop_reason=data.get("stop_reason", ""),
            raw=data,
        )


class OpenAICompatibleProvider(Provider):
    """Any endpoint that speaks the OpenAI chat-completions API.

    That covers OpenAI itself, Azure-hosted deployments, and the several
    gateways a university might put in front of a model, which is why the base
    URL is a setting rather than a constant.
    """

    name = "openai_compatible"
    sends_data_off_device = True

    def __init__(self, model: str, *, api_key: str | None = None,
                 base_url: str = "https://api.openai.com/v1",
                 json_mode: bool = True, **options: Any) -> None:
        super().__init__(model, **options)
        self.api_key = api_key or get_key("openai_compatible")
        self.base_url = base_url.rstrip("/")
        self.json_mode = json_mode

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.0, timeout: float = 120.0) -> LLMResponse:
        if not self.api_key:
            raise AuthenticationError(
                "No API key is stored for the OpenAI-compatible endpoint. Add "
                "one under Tools → Model settings, or set OPENAI_API_KEY."
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = _post(
            f"{self.base_url}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            payload,
            timeout,
        )
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=data.get("model", self.model),
            model_version=data.get("model", self.model),
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            ),
            stop_reason=choices[0].get("finish_reason", ""),
            raw=data,
        )


class OllamaProvider(Provider):
    """A model running locally through Ollama.

    The option for reviewers whose ethics approval or data agreement forbids
    sending records to a third party. Nothing leaves the machine.
    """

    name = "ollama"
    sends_data_off_device = False

    def __init__(self, model: str, *, base_url: str = "http://localhost:11434",
                 **options: Any) -> None:
        super().__init__(model, **options)
        self.base_url = base_url.rstrip("/")

    def complete(self, *, system: str, user: str, max_tokens: int = 1024,
                 temperature: float = 0.0, timeout: float = 600.0) -> LLMResponse:
        # Local models are slower, so the default timeout is longer.
        data = _post(
            f"{self.base_url}/api/chat",
            {"content-type": "application/json"},
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
            timeout,
        )
        text = (data.get("message") or {}).get("content") or ""
        return LLMResponse(
            text=text,
            model=data.get("model", self.model),
            model_version=data.get("model", self.model),
            usage=Usage(
                prompt_tokens=int(data.get("prompt_eval_count", 0)),
                completion_tokens=int(data.get("eval_count", 0)),
            ),
            stop_reason=data.get("done_reason", ""),
            raw=data,
        )


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    label: str
    factory: type[Provider]
    default_model: str
    needs_key: bool
    base_url: str
    description: str

    @property
    def sends_data_off_device(self) -> bool:
        return self.factory.sends_data_off_device


# The default models below are starting points, not recommendations. Model
# names change often, so the model string is an editable field in the settings
# panel and whatever was actually used is recorded in `ai_call` and reported in
# the AI-use statement.
PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic (Claude)",
        factory=AnthropicProvider,
        default_model="claude-sonnet-5-5",
        needs_key=True,
        base_url="https://api.anthropic.com",
        description="The default. Records are sent to Anthropic's API.",
    ),
    "openai_compatible": ProviderSpec(
        key="openai_compatible",
        label="OpenAI-compatible endpoint",
        factory=OpenAICompatibleProvider,
        default_model="gpt-4o-mini",
        needs_key=True,
        base_url="https://api.openai.com/v1",
        description=(
            "OpenAI, an Azure deployment, or any gateway speaking the same "
            "chat-completions API. Set the base URL to match."
        ),
    ),
    "ollama": ProviderSpec(
        key="ollama",
        label="Local model (Ollama)",
        factory=OllamaProvider,
        default_model="llama3.1:8b",
        needs_key=False,
        base_url="http://localhost:11434",
        description=(
            "Runs on this computer. Nothing is sent anywhere. Choose this "
            "when records may not leave the machine."
        ),
    ),
}


def build_provider(key: str, model: str | None = None, **options: Any) -> Provider:
    """Create a provider by its registry key."""
    spec = PROVIDERS.get(key)
    if spec is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ProviderError(f"Unknown provider {key!r}. Available: {known}.")
    options.setdefault("base_url", spec.base_url)
    return spec.factory(model or spec.default_model, **options)


def available_providers() -> list[dict[str, Any]]:
    """Every provider with whether it is ready to use, for the settings panel."""
    from .keys import has_key

    out: list[dict[str, Any]] = []
    for spec in PROVIDERS.values():
        ready = (not spec.needs_key) or has_key(spec.key)
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "default_model": spec.default_model,
                "needs_key": spec.needs_key,
                "has_key": has_key(spec.key) if spec.needs_key else True,
                "ready": ready,
                "base_url": spec.base_url,
                "description": spec.description,
                "sends_data_off_device": spec.sends_data_off_device,
            }
        )
    return out


def parse_json_object(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model's answer.

    Models asked for JSON sometimes wrap it in a fenced code block or add a
    sentence of commentary. Rather than fail the whole record, take the
    outermost braces. Anything beyond that is a genuine format error and the
    client retries it.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
        text = text.strip("`").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("No JSON object found in the response.")
