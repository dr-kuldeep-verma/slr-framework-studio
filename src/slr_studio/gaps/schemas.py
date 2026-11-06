"""What a drafted study must contain before it is stored.

The hypothesis has to name the proposition it tests. A hypothesis that tests
nothing in the framework is a research idea, not a way of closing this
review's gap, and the whole point of drafting from the register is that the
study would answer something the review itself raised.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["StudyDraft"]


class StudyDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=10, max_length=400)
    hypothesis: str = Field(min_length=10, max_length=400)
    design: str = Field(min_length=3, max_length=400)
    measures: list[str] = Field(default_factory=list)
    population: str = Field(min_length=2, max_length=200)
    setting: str = ""
    #: The proposition the hypothesis tests, as "P3", where one applies.
    proposition: str = ""

    @field_validator("question", "hypothesis", "design", "population", "setting")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return " ".join(value.split())
