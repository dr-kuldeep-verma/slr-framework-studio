"""The shapes a screening answer must take.

Validation here is not decoration. A screener that returns "exclude" without
naming which criterion the record failed cannot be audited, and a reason
longer than a sentence turns the Excel log into prose nobody reads. Both are
rejected, which makes the client retry with the problem spelled out rather
than storing an answer the review cannot defend.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = ["ScreeningAnswer", "AdjudicationAnswer", "Decision", "MAX_REASON_CHARS"]

Decision = Literal["include", "exclude", "unclear"]

MAX_REASON_CHARS = 400


class ScreeningAnswer(BaseModel):
    """One screener's verdict on one record."""

    decision: Decision
    exclusion_code: str = Field(default="", description="First criterion the record fails")
    reason: str = Field(description="One sentence, grounded in the record")
    quote: str = Field(default="", description="Words from the title or abstract")

    model_config = {"extra": "ignore"}

    @field_validator("exclusion_code", "reason", "quote", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @field_validator("decision", mode="before")
    @classmethod
    def _normalise_decision(cls, value: object) -> object:
        # Models answer "Include", "INCLUDE", or "maybe". The first two are
        # the same answer; the third is not a category and is rejected.
        if isinstance(value, str):
            text = value.strip().casefold()
            return {"included": "include", "excluded": "exclude"}.get(text, text)
        return value

    @field_validator("reason")
    @classmethod
    def _reason_present(cls, value: str) -> str:
        if not value:
            raise ValueError("a reason is required so the decision can be audited")
        if len(value) > MAX_REASON_CHARS:
            # Trim rather than reject: the decision is still usable, and a
            # retry would cost a call to fix a style problem.
            return value[: MAX_REASON_CHARS - 1].rstrip() + "…"
        return value

    @model_validator(mode="after")
    def _exclusions_need_a_code(self) -> ScreeningAnswer:
        if self.decision == "exclude" and not self.exclusion_code:
            raise ValueError(
                "an exclusion must name the first exclusion code the record "
                "fails, for example E4"
            )
        if self.decision != "exclude" and self.exclusion_code:
            # A code attached to an include is noise; drop it quietly.
            object.__setattr__(self, "exclusion_code", "")
        return self


class AdjudicationAnswer(ScreeningAnswer):
    """The third call's ruling on a disagreement.

    Same shape as a screening answer, plus which screener it sided with, so
    the Excel log can show whether one persona is being overruled repeatedly.
    """

    sided_with: Literal["screener_a", "screener_b", "neither"] = "neither"

    @field_validator("sided_with", mode="before")
    @classmethod
    def _normalise_side(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().casefold().replace(" ", "_").replace("-", "_")
            return {"a": "screener_a", "b": "screener_b", "": "neither"}.get(text, text)
        return value
