"""The only way a stage talks to a model.

Everything the integrity rules demand happens here, once, for every stage:

* temperature 0 and a pydantic schema the answer must satisfy;
* retry with exponential backoff, and one repair attempt when the answer is
  not valid JSON for the schema;
* a response cache keyed by provider, model, prompt and input, so re-running a
  stage costs nothing and gives the same answers;
* a row in `ai_call` for every call -- model, prompt hash, input hash, output,
  tokens, cost, timestamp -- whether the answer came from the provider or the
  cache.

A stage therefore cannot accidentally make an unlogged call, because the only
method it has is :meth:`AIClient.ask`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.db import utc_now
from ..core.hashing import canonical_json, sha256_text
from .base import (
    TEMPERATURE_NOT_SENT,
    AIError,
    Provider,
    ProviderError,
    RateLimitError,
    ResponseFormatError,
    estimate_tokens,
)
from .pricing import Rate, estimate_cost, rate_for
from .providers import parse_json_object

__all__ = ["AIClient", "AIResult", "CostEstimate", "BudgetExceeded"]

T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(AIError):
    """The run hit the ceiling the reviewer set."""

    retryable = False


@dataclass
class AIResult:
    """One validated answer, with everything needed to justify it later."""

    value: Any
    ai_call_id: int
    cached: bool
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    attempts: int = 1


@dataclass
class CostEstimate:
    n_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    price_known: bool
    model: str
    n_cached: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def describe(self) -> str:
        """One line for the confirmation dialog before a bulk run."""
        calls = f"{self.n_calls:,} call{'s' if self.n_calls != 1 else ''}"
        if self.n_cached:
            calls += f" ({self.n_cached:,} already cached and free)"
        tokens = f"about {self.total_tokens:,} tokens"
        if not self.price_known:
            return (
                f"{calls}, {tokens}. No price is on file for {self.model}, so "
                "the cost cannot be estimated. Add a rate in Model settings."
            )
        return f"{calls}, {tokens}, roughly US${self.cost_usd:,.2f} on {self.model}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_calls": self.n_calls,
            "n_cached": self.n_cached,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "price_known": self.price_known,
            "model": self.model,
        }


class AIClient:
    def __init__(
        self,
        project,
        provider: Provider,
        *,
        stage: str,
        temperature: float = 0.0,
        use_cache: bool = True,
        max_retries: int = 4,
        backoff_base: float = 1.5,
        sleep=None,
        rate_overrides: dict[str, Rate] | None = None,
    ) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.provider = provider
        self.stage = stage
        # Temperature 0 is not a default a caller may raise: two screeners
        # must differ because their instructions differ, not because the
        # sampler did, or the review stops being reproducible.
        self.temperature = temperature
        self.use_cache = use_cache
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep if sleep is not None else time.sleep
        self.rate_overrides = rate_overrides or {}
        self.spent_usd = 0.0
        self.budget_usd: float | None = None
        self.n_calls = 0
        self.n_cached = 0

    # -- asking ------------------------------------------------------------

    def ask(
        self,
        task: str,
        *,
        system: str,
        user: str,
        schema: type[T],
        entity: str | None = None,
        entity_id: str | int | None = None,
        max_tokens: int = 800,
    ) -> AIResult:
        """One validated call. Everything else in this class serves this."""
        cache_key = self._cache_key(system, user, schema)
        cached_output = self._cache_get(cache_key) if self.use_cache else None

        if cached_output is not None:
            value = self._validate(cached_output, schema)
            call_id = self._log_call(
                task, system, user, cached_output, self.provider.model,
                0, 0, 0.0, True, entity, entity_id,
            )
            self.n_cached += 1
            return AIResult(
                value=value, ai_call_id=call_id, cached=True,
                model=self.provider.model,
            )

        self._check_budget()
        response, value, attempts = self._call_with_retries(system, user, schema, max_tokens)
        cost, _ = estimate_cost(
            response.model,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            self.rate_overrides,
        )
        self.spent_usd += cost
        self.n_calls += 1
        if self.use_cache:
            self._cache_put(cache_key, response.text)
        call_id = self._log_call(
            task, system, user, response.text, response.model,
            response.usage.prompt_tokens, response.usage.completion_tokens,
            cost, False, entity, entity_id,
        )
        return AIResult(
            value=value,
            ai_call_id=call_id,
            cached=False,
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            cost_usd=cost,
            attempts=attempts,
        )

    def _call_with_retries(self, system: str, user: str, schema: type[T], max_tokens: int):
        last_error: Exception | None = None
        prompt = user
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.provider.complete(
                    system=system,
                    user=prompt,
                    max_tokens=max_tokens,
                    temperature=self.temperature,
                )
                try:
                    value = self._validate(response.text, schema)
                except ResponseFormatError as exc:
                    if response.stop_reason in ("max_tokens", "length"):
                        raise ResponseFormatError(
                            f"The answer was cut off at the {max_tokens}-token "
                            "limit before it was complete. Raise the limit "
                            "(--max-tokens) if this keeps happening."
                        ) from exc
                    raise
                return response, value, attempt
            except AIError as exc:
                last_error = exc
                if not getattr(exc, "retryable", True) or attempt == self.max_retries:
                    raise
                if isinstance(exc, ResponseFormatError):
                    # Say what was wrong rather than repeating the same ask and
                    # hoping. The original prompt is never discarded.
                    if "cut off" in str(exc):
                        prompt = (
                            f"{user}\n\n---\nYour previous answer was cut off "
                            "at the length limit before the JSON was complete. "
                            "Reply with a single, shorter JSON object: keep the "
                            "reason to one or two sentences and the quote brief."
                        )
                    else:
                        prompt = (
                            f"{user}\n\n---\nYour previous answer could not be "
                            f"used: {exc}\nReply with a single JSON object and "
                            "nothing else."
                        )
                if isinstance(exc, RateLimitError):
                    # Per-minute limits reset on the minute, and a retry
                    # inside the same window only spends another request.
                    self._sleep(20.0 * attempt)
                elif isinstance(exc, ProviderError):
                    # The provider's own failure (a 5xx, a timeout). Usually
                    # brief, and a server in trouble is not helped by being
                    # asked again at once.
                    self._sleep(15.0 * attempt)
                else:
                    self._sleep(self.backoff_base ** attempt)
        raise last_error if last_error else AIError("The call failed for an unknown reason.")

    @staticmethod
    def _validate(text: str, schema: type[T]) -> T:
        try:
            payload = parse_json_object(text)
        except ValueError as exc:
            raise ResponseFormatError(str(exc)) from exc
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in exc.errors()[:4]
            )
            raise ResponseFormatError(problems) from exc

    # -- budget ------------------------------------------------------------

    def set_budget(self, usd: float | None) -> None:
        self.budget_usd = usd

    def _check_budget(self) -> None:
        if self.budget_usd is not None and self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(
                f"This run has spent about US${self.spent_usd:,.2f}, which "
                f"reaches the limit of US${self.budget_usd:,.2f}. Raise the "
                "limit to continue; work already done is saved."
            )

    # -- estimating --------------------------------------------------------

    def estimate(
        self,
        prompts: list[tuple[str, str]],
        *,
        expected_completion_tokens: int = 160,
        schema: type[BaseModel] | None = None,
    ) -> CostEstimate:
        """What a bulk run would cost, before it starts (integrity rule 5).

        Prompts already in the cache are counted separately and priced at
        zero, so re-running a stage after a change honestly shows as nearly
        free rather than as a second full bill.
        """
        prompt_tokens = 0
        completion_tokens = 0
        n_cached = 0
        for system, user in prompts:
            if self.use_cache and schema is not None:
                if self._cache_get(self._cache_key(system, user, schema)) is not None:
                    n_cached += 1
                    continue
            prompt_tokens += estimate_tokens(system) + estimate_tokens(user)
            completion_tokens += expected_completion_tokens
        cost, known = estimate_cost(
            self.provider.model, prompt_tokens, completion_tokens, self.rate_overrides
        )
        return CostEstimate(
            n_calls=len(prompts) - n_cached,
            n_cached=n_cached,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            price_known=known or not prompt_tokens,
            model=self.provider.model,
        )

    # -- cache -------------------------------------------------------------

    def _cache_key(self, system: str, user: str, schema: type[BaseModel]) -> str:
        return sha256_text(
            canonical_json(
                {
                    "provider": self.provider.name,
                    "model": self.provider.model,
                    "temperature": self.temperature,
                    "schema": schema.__name__,
                    "system": system,
                    "user": user,
                }
            )
        )

    def _cache_get(self, key: str) -> str | None:
        row = self.db.query_one("SELECT output FROM ai_cache WHERE cache_key = ?", (key,))
        if row is None:
            return None
        self.db.execute(
            "UPDATE ai_cache SET hits = hits + 1 WHERE cache_key = ?", (key,)
        )
        return str(row["output"])

    def _cache_put(self, key: str, output: str) -> None:
        self.db.execute(
            "INSERT INTO ai_cache(cache_key, output, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET output = excluded.output",
            (key, output, utc_now()),
        )

    def clear_cache(self) -> int:
        n = int(self.db.scalar("SELECT COUNT(*) FROM ai_cache") or 0)
        self.db.execute("DELETE FROM ai_cache")
        self.audit.log(self.stage, "ai_cache_cleared", detail={"n_entries": n})
        return n

    # -- logging -----------------------------------------------------------

    def _log_call(
        self, task: str, system: str, user: str, output: str, model: str,
        prompt_tokens: int, completion_tokens: int, cost: float, cached: bool,
        entity: str | None, entity_id: str | int | None,
    ) -> int:
        return self.db.insert(
            "ai_call",
            {
                "stage": self.stage,
                "task": task,
                "provider": self.provider.name,
                "model": model,
                "model_version": model,
                "temperature": (
                    self.temperature
                    if getattr(self.provider, "sends_temperature", True)
                    else TEMPERATURE_NOT_SENT
                ),
                "prompt_hash": sha256_text(system),
                "input_hash": sha256_text(user),
                "output": output,
                "output_hash": sha256_text(output),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost,
                "cached": 1 if cached else 0,
                "entity": entity,
                "entity_id": None if entity_id is None else str(entity_id),
                "created_at": utc_now(),
            },
        )

    # -- reporting ---------------------------------------------------------

    def usage_summary(self) -> dict[str, Any]:
        """What this project has spent, for the AI-use statement."""
        row = self.db.query_one(
            "SELECT COUNT(*) AS n_calls, "
            "       SUM(cached) AS n_cached, "
            "       SUM(prompt_tokens) AS prompt_tokens, "
            "       SUM(completion_tokens) AS completion_tokens, "
            "       SUM(cost_usd) AS cost_usd "
            "FROM ai_call"
        )
        data = dict(row) if row else {}
        return {key: (value or 0) for key, value in data.items()}

    def models_used(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT provider, model, stage, task, COUNT(*) AS n, "
                "       MIN(created_at) AS first_used, MAX(created_at) AS last_used, "
                "       SUM(cost_usd) AS cost_usd "
                "FROM ai_call GROUP BY provider, model, stage, task "
                "ORDER BY first_used"
            )
        ]
