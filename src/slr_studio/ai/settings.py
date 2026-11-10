"""Which model a project uses, stored with the project.

The choice lives in the project's `meta` table rather than in a global
preference, because it is part of how *this review* was conducted: reopening a
finished review a year later should show the model it was screened with, not
whatever the reviewer has configured since.

The API key is never part of this. It stays in the credential store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .mock import MockProvider
from .providers import PROVIDERS, build_provider

__all__ = [
    "ModelSettings",
    "default_provider",
    "load_settings",
    "save_settings",
    "provider_from_settings",
]

_KEYS = {
    "provider": "ai.provider",
    "model": "ai.model",
    "base_url": "ai.base_url",
    "budget_usd": "ai.budget_usd",
    "use_cache": "ai.use_cache",
    "offline": "ai.offline",
}


@dataclass
class ModelSettings:
    provider: str = "anthropic"
    model: str = ""
    base_url: str = ""
    budget_usd: float | None = None
    use_cache: bool = True
    #: Run against the built-in offline screener instead of a real model.
    #: This is how the demo works without an API key, and it is recorded in
    #: `ai_call` like any other provider so nobody can mistake a demo run for
    #: a real one.
    offline: bool = False

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        spec = PROVIDERS.get(self.provider)
        return spec.default_model if spec else ""

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        spec = PROVIDERS.get(self.provider)
        return spec.base_url if spec else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.resolved_model(),
            "base_url": self.resolved_base_url(),
            "budget_usd": self.budget_usd,
            "use_cache": self.use_cache,
            "offline": self.offline,
        }


def default_provider() -> str:
    """The model a review uses before anyone has chosen one.

    A paid account the reviewer has already connected, if there is one;
    otherwise a free model on this computer through Ollama. Ollama costs
    nothing and keeps every record on the machine, which is the right
    starting point for someone who has not paid for anything.
    """
    from . import keys

    if keys.has_key("anthropic"):
        return "anthropic"
    if keys.has_key("openai_compatible"):
        return "openai_compatible"
    try:
        from .chatgpt import ChatGPTSession

        if ChatGPTSession().signed_in():
            return "chatgpt_subscription"
    except Exception:
        pass
    return "ollama"


def load_settings(project) -> ModelSettings:
    db = project.db
    budget = db.get_meta(_KEYS["budget_usd"])
    return ModelSettings(
        provider=db.get_meta(_KEYS["provider"], "") or default_provider(),
        model=db.get_meta(_KEYS["model"], "") or "",
        base_url=db.get_meta(_KEYS["base_url"], "") or "",
        budget_usd=float(budget) if budget else None,
        use_cache=(db.get_meta(_KEYS["use_cache"], "1") or "1") == "1",
        offline=(db.get_meta(_KEYS["offline"], "0") or "0") == "1",
    )


def save_settings(project, settings: ModelSettings, *, actor: str | None = None) -> None:
    db = project.db
    with db.transaction():
        db.set_meta(_KEYS["provider"], settings.provider)
        db.set_meta(_KEYS["model"], settings.model)
        db.set_meta(_KEYS["base_url"], settings.base_url)
        db.set_meta(
            _KEYS["budget_usd"], "" if settings.budget_usd is None else str(settings.budget_usd)
        )
        db.set_meta(_KEYS["use_cache"], "1" if settings.use_cache else "0")
        db.set_meta(_KEYS["offline"], "1" if settings.offline else "0")
        project.audit.log(
            "project",
            "model_settings_changed",
            detail=settings.as_dict(),
            actor=actor,
        )


def provider_from_settings(settings: ModelSettings):
    """Build the provider a project is configured to use."""
    if settings.offline:
        return MockProvider()
    # No network here: pages build a provider every time they refresh. A
    # ChatGPT review with no model chosen gets a provider that says so when
    # it is first asked something; Model settings is where the model list is
    # fetched.
    model = settings.resolved_model()
    return build_provider(
        settings.provider,
        model,
        base_url=settings.resolved_base_url(),
    )
