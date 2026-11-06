"""The language-model layer.

Three things are kept apart on purpose:

* **Providers** (`providers.py`) know how to talk to one API and nothing else.
* **The client** (`client.py`) owns everything the review depends on:
  temperature 0, JSON validated against a pydantic schema, retry with backoff,
  a response cache keyed by prompt and input, and a row in `ai_call` for every
  call. No stage ever talks to a provider directly.
* **Keys** (`keys.py`) live in the operating system's credential store.

That split is why integrity rules 2 to 5 hold for every stage at once: a new
stage gets logging, caching and cost estimates by using the client.
"""

from .base import (
    AIError,
    AuthenticationError,
    LLMResponse,
    Provider,
    ProviderError,
    RateLimitError,
    Usage,
)
from .client import AIClient, CostEstimate
from .providers import PROVIDERS, available_providers, build_provider
from .mock import MockProvider, ScriptedProvider
from .settings import (
    ModelSettings,
    load_settings,
    provider_from_settings,
    save_settings,
)

__all__ = [
    "AIClient",
    "CostEstimate",
    "LLMResponse",
    "Provider",
    "Usage",
    "AIError",
    "ProviderError",
    "AuthenticationError",
    "RateLimitError",
    "PROVIDERS",
    "build_provider",
    "available_providers",
    "MockProvider",
    "ScriptedProvider",
    "ModelSettings",
    "load_settings",
    "save_settings",
    "provider_from_settings",
]
