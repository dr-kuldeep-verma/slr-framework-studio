"""What the synthesis calls must return before anything is stored.

Three rules are enforced here rather than trusted to the prompt.

**A construct needs a definition and an exclusion.** "Trust" as a bare name
is not a construct; it is a word two papers can use for different things. A
codebook entry that cannot say what it excludes has not drawn a boundary, and
a boundary is the only thing that makes the later mapping checkable.

**A mapping must name what was measured.** The spec requires mapping by
measurement rather than vocabulary, so the answer has to carry the
measurement it inferred. A reviewer confirming the mapping reads that, not
the label.

**A mapping may not invent a construct.** The model is given the approved
codebook and must choose from it or say "other". Letting it coin a construct
here would route around the human approval that the codebook stage exists
for.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "CodebookProposal",
    "ConstructProposal",
    "ContradictionExplanation",
    "LabelMapping",
    "MappingAnswer",
    "RESIDUAL_CONSTRUCT",
    "LinkageJudgement",
]

#: The residual code the spec allows. A label that genuinely fits nothing goes
#: here, visibly, rather than being pressed into the nearest construct.
RESIDUAL_CONSTRUCT = "other"


class ConstructProposal(BaseModel):
    """One entry in the proposed codebook."""

    name: str = Field(min_length=2, max_length=80)
    definition: str = Field(min_length=10)
    includes: list[str] = Field(default_factory=list)
    excludes: list[str] = Field(default_factory=list)
    example_labels: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _tidy_name(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def _a_boundary_needs_two_sides(self) -> "ConstructProposal":
        if not self.includes:
            raise ValueError(
                f"The construct {self.name!r} says what it is but not what "
                "counts as an instance of it. A codebook entry without "
                "`includes` cannot be applied consistently."
            )
        if not self.excludes:
            raise ValueError(
                f"The construct {self.name!r} has no `excludes`. A construct "
                "that excludes nothing has no boundary, and every label will "
                "map to it."
            )
        return self


class CodebookProposal(BaseModel):
    constructs: list[ConstructProposal] = Field(min_length=1)
    note: str = ""

    @model_validator(mode="after")
    def _names_must_differ(self) -> "CodebookProposal":
        seen: dict[str, str] = {}
        for construct in self.constructs:
            key = construct.name.casefold()
            if key in seen:
                raise ValueError(
                    f"Two constructs are both called {construct.name!r}. "
                    "Merge them or give them names that say how they differ."
                )
            seen[key] = construct.name
        return self


class LabelMapping(BaseModel):
    """Where one author label belongs."""

    # Accept both the alias and the field name, so a model that answers
    # "construct_name" instead of "construct" is not rejected over wording.
    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(min_length=1)
    # Named `construct_name` because plain `construct` shadows a method on
    # pydantic's BaseModel. The model still answers under the key
    # "construct", which is the natural word for it in the prompt.
    construct_name: str = Field(min_length=1, alias="construct")
    measured_as: str = Field(min_length=3)
    rationale: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    contestable: bool = False
    contest_reason: str = ""

    @model_validator(mode="after")
    def _a_doubt_needs_a_reason(self) -> "LabelMapping":
        if self.contestable and not self.contest_reason.strip():
            raise ValueError(
                f"The mapping of {self.label!r} is flagged contestable with "
                "no reason given. A flag a reviewer cannot act on is noise."
            )
        return self


class MappingAnswer(BaseModel):
    mappings: list[LabelMapping] = Field(default_factory=list)

    def construct_names(self) -> set[str]:
        return {m.construct_name.casefold() for m in self.mappings}


class LinkageJudgement(BaseModel):
    """Whether two reports look like the same body of fieldwork."""

    same_dataset: Literal["yes", "no", "unclear"]
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = Field(min_length=5)
    shared_signals: list[str] = Field(default_factory=list)


class ContradictionExplanation(BaseModel):
    """Why the datasets behind a contested link disagree."""

    explanation_type: Literal["context", "measurement", "method", "sample", "unclear"]
    explanation: str = Field(min_length=10)
    suggestion_type: Literal["moderator", "construct_split", "none"] = "none"
    suggested_moderator: str = ""
    split_into: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_suggestion_needs_its_content(self) -> "ContradictionExplanation":
        if self.suggestion_type == "moderator" and not self.suggested_moderator.strip():
            raise ValueError(
                "A moderator is suggested but not named. The reviewer has "
                "nothing to accept or reject."
            )
        if self.suggestion_type == "construct_split" and len(self.split_into) < 2:
            raise ValueError(
                "A construct split is suggested but fewer than two "
                "constructs are named to split it into."
            )
        return self
