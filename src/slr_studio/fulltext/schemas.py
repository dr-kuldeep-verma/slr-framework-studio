"""The shape a full-text eligibility answer must take."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = ["EligibilityAnswer", "MAX_REASON_CHARS"]

MAX_REASON_CHARS = 600


class EligibilityAnswer(BaseModel):
    """One assessment of one report.

    Unlike a screening answer this one must say *where* it is looking. A page
    number is required for every decision that has a quotation, because a
    quotation nobody can locate is an assertion.
    """

    decision: Literal["include", "exclude", "unclear"]
    exclusion_code: str = Field(default="")
    reason: str = Field(description="Two sentences at most")
    quote: str = Field(default="", description="Words copied from the report")
    quote_page: int | None = Field(
        default=None, description="The PDF page the quotation is on, counting from 1"
    )

    model_config = {"extra": "ignore"}

    @field_validator("decision", mode="before")
    @classmethod
    def _normalise_decision(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().casefold()
            return {"included": "include", "excluded": "exclude"}.get(text, text)
        return value

    @field_validator("exclusion_code", "reason", "quote", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("quote_page", mode="before")
    @classmethod
    def _as_page(cls, value: object) -> object:
        # Models write "p. 7", "page 7", "7", 7 and occasionally "7-8".
        if value is None or isinstance(value, int):
            return value
        text = str(value).strip().casefold()
        if not text:
            return None
        digits = ""
        for char in text:
            if char.isdigit():
                digits += char
            elif digits:
                break
        return int(digits) if digits else None

    @field_validator("reason")
    @classmethod
    def _reason_present(cls, value: str) -> str:
        if not value:
            raise ValueError("a reason is required so the decision can be audited")
        if len(value) > MAX_REASON_CHARS:
            return value[: MAX_REASON_CHARS - 1].rstrip() + "…"
        return value

    @field_validator("quote_page")
    @classmethod
    def _page_is_plausible(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("page numbers start at 1")
        return value

    @model_validator(mode="after")
    def _check_pairing(self) -> EligibilityAnswer:
        if self.decision == "exclude" and not self.exclusion_code:
            raise ValueError(
                "an exclusion must name the first exclusion code the report "
                "fails, for example E4"
            )
        if self.decision != "exclude" and self.exclusion_code:
            object.__setattr__(self, "exclusion_code", "")
        if self.quote and self.quote_page is None:
            raise ValueError(
                "a quotation must say which page it is on, so it can be checked"
            )
        if self.quote_page is not None and not self.quote:
            object.__setattr__(self, "quote_page", None)
        return self
