"""What every provider must offer, and the errors they may raise."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TEMPERATURE_NOT_SENT",
    "Provider",
    "LLMResponse",
    "Usage",
    "AIError",
    "ProviderError",
    "AuthenticationError",
    "RateLimitError",
    "ResponseFormatError",
]


class AIError(RuntimeError):
    """Base for everything this package raises."""


class ProviderError(AIError):
    """The provider was reached but could not answer."""

    #: Whether retrying the same request might succeed.
    retryable = True


class AuthenticationError(ProviderError):
    """Missing or rejected credentials. Retrying will not help."""

    retryable = False


class RateLimitError(ProviderError):
    """Too many requests. Back off and try again."""

    retryable = True


class ResponseFormatError(AIError):
    """The model answered, but not in the shape the schema requires."""

    retryable = True


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    text: str
    model: str
    model_version: str = ""
    usage: Usage = field(default_factory=Usage)
    raw: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""


#: Stored in `ai_call.temperature` when no temperature was sent, because the
#: model refuses one and applies its own. Recording 0 there would be a false
#: statement in the audit trail; the exported log spells this value out.
TEMPERATURE_NOT_SENT = -1.0


class Provider(ABC):
    """One language-model backend.

    Providers are deliberately thin. They do not retry, cache, log or parse:
    those belong to :class:`~slr_studio.ai.client.AIClient`, so that every
    stage gets identical behaviour whichever backend a reviewer chooses.
    """

    #: Short identifier stored in `ai_call.provider` and the AI-use statement.
    name: str = ""
    #: Whether the backend sends data off the computer. Shown in the interface.
    sends_data_off_device: bool = True
    #: False once the backend has refused a temperature setting. Newer models
    #: do: they choose their own, and the log then records that none was sent.
    sends_temperature: bool = True

    def __init__(self, model: str, **options: Any) -> None:
        self.model = model
        self.options = options

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = 120.0,
    ) -> LLMResponse:
        """Send one request and return the model's answer verbatim."""

    def check(self) -> tuple[bool, str]:
        """A cheap round trip, for the 'test connection' button."""
        try:
            response = self.complete(
                system="Reply with the single word OK.",
                user="Connection test.",
                max_tokens=16,
            )
        except AIError as exc:
            return False, str(exc)
        return True, f"{self.name}: {response.model} answered."

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.model}>"


def estimate_tokens(text: str) -> int:
    """A rough token count used only for pre-run cost estimates.

    Deliberately crude and provider-independent: four characters per token is
    close enough for an order-of-magnitude warning before a bulk run, and the
    figure shown is labelled as an estimate. The real counts come back with
    each response and are what `ai_call` stores.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
