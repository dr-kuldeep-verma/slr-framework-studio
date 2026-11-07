"""Spacing calls out to stay inside a provider's per-minute limit.

Free API tiers allow a handful of requests a minute. Retrying after a
refusal works, but it wastes the refused request and, on some tiers, counts
it against the daily quota. Going no faster than the limit in the first
place wastes nothing.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .base import LLMResponse, Provider

__all__ = ["ThrottledProvider"]


class ThrottledProvider(Provider):
    """Wraps a provider so that calls are at least ``60 / per_minute`` apart.

    Everything else -- the model name, whether data leaves the computer --
    is the wrapped provider's, so the audit log records the real backend.
    """

    def __init__(
        self,
        inner: Provider,
        per_minute: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not per_minute or per_minute <= 0:
            raise ValueError("A request limit must be a positive number per minute.")
        self.inner = inner
        self.interval = 60.0 / float(per_minute)
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._last: float | None = None

    # Present as the wrapped provider in every respect but the pacing.
    @property
    def model(self) -> str:  # type: ignore[override]
        return self.inner.model

    @model.setter
    def model(self, value: str) -> None:
        self.inner.model = value

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.inner.name

    @property
    def sends_data_off_device(self) -> bool:  # type: ignore[override]
        return self.inner.sends_data_off_device

    def __getattr__(self, attr: str) -> Any:
        # Only reached for attributes not found normally.
        return getattr(self.__dict__["inner"], attr)

    def complete(self, **kwargs: Any) -> LLMResponse:
        if self._last is not None:
            wait = self._last + self.interval - self._clock()
            if wait > 0:
                self._sleep(round(wait, 3))
        self._last = self._clock()
        return self.inner.complete(**kwargs)
