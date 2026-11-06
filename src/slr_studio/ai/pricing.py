"""Prices used for the estimate shown before a bulk run.

These numbers go stale. They are defaults for a warning, not a quotation: the
figure is always labelled as an estimate, a reviewer can edit the rate for
their model in the settings panel, and the override is stored with the
project. What a run actually cost is computed from the token counts the
provider returns and stored per call in `ai_call`.

Rates are US dollars per million tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Rate", "DEFAULT_RATES", "rate_for", "estimate_cost"]


@dataclass(frozen=True)
class Rate:
    input_per_million: float
    output_per_million: float

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.input_per_million
            + completion_tokens * self.output_per_million
        ) / 1_000_000


#: Matched by prefix, longest first, so a family rate covers its versions.
DEFAULT_RATES: dict[str, Rate] = {
    "claude-opus": Rate(15.0, 75.0),
    "claude-sonnet": Rate(3.0, 15.0),
    "claude-haiku": Rate(0.80, 4.0),
    "gpt-4o-mini": Rate(0.15, 0.60),
    "gpt-4o": Rate(2.50, 10.0),
    "gpt-4": Rate(10.0, 30.0),
    "o1-mini": Rate(1.10, 4.40),
    "gpt-3.5": Rate(0.50, 1.50),
    # Anything running through Ollama is on the reviewer's own hardware.
    "llama": Rate(0.0, 0.0),
    "mistral": Rate(0.0, 0.0),
    "qwen": Rate(0.0, 0.0),
    "gemma": Rate(0.0, 0.0),
    "phi": Rate(0.0, 0.0),
    # The offline providers used by the demo and the tests.
    "mock": Rate(0.0, 0.0),
    "scripted": Rate(0.0, 0.0),
}

ZERO = Rate(0.0, 0.0)


def rate_for(
    model: str, overrides: dict[str, Rate] | None = None
) -> tuple[Rate, bool]:
    """The rate for a model, and whether it was actually found.

    The second value matters: an unrecognised model is costed at zero, and the
    interface says "price unknown" rather than showing a confident $0.00.
    """
    model = (model or "").casefold()
    if overrides:
        for prefix in sorted(overrides, key=len, reverse=True):
            if model.startswith(prefix.casefold()):
                return overrides[prefix], True
    for prefix in sorted(DEFAULT_RATES, key=len, reverse=True):
        if model.startswith(prefix):
            return DEFAULT_RATES[prefix], True
    return ZERO, False


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    overrides: dict[str, Rate] | None = None,
) -> tuple[float, bool]:
    rate, known = rate_for(model, overrides)
    return rate.cost(prompt_tokens, completion_tokens), known
