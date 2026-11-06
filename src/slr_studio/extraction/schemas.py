"""What an extraction must look like before it is stored.

Two rules are enforced here rather than left to the prompt, because a prompt
asks and a validator insists.

**The experiment rule.** A study is an experiment only when the report shows a
manipulated treatment compared against a control. Papers describe themselves
as experiments when they are nothing of the kind, and a review that counts
those as experiments will report a sensitivity analysis on "experiments only"
that means nothing. A claim that fails the rule is downgraded to
quasi-experimental, and the downgrade is recorded so a human can see it
happened.

**A quotation needs a page.** Same rule as full-text eligibility: a quotation
nobody can locate is an assertion.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "ReportExtraction",
    "Relationship",
    "ExtractionAnswer",
    "DirectionCheck",
    "Theory",
    "DESIGN_FAMILIES",
    "APPRAISAL_INSTRUMENTS",
    "instrument_for",
    "DIRECTIONS",
]

DesignFamily = Literal[
    "randomised_experiment",
    "quasi_experiment",
    "cross_sectional",
    "longitudinal",
    "case_study",
    "qualitative",
    "mixed_methods",
    "review",
    "conceptual",
    "unclear",
]

DESIGN_FAMILIES = (
    "randomised_experiment",
    "quasi_experiment",
    "cross_sectional",
    "longitudinal",
    "case_study",
    "qualitative",
    "mixed_methods",
    "review",
    "conceptual",
    "unclear",
)

APPRAISAL_INSTRUMENTS = ("RoB 2", "AXIS", "CASP", "MMAT", "none")

DIRECTIONS = ("positive", "negative", "none", "mixed", "unclear")

#: Which appraisal instrument suits which design. The engine compares the
#: instrument the extraction chose against this and flags a mismatch rather
#: than overruling it: an unusual design may genuinely warrant another tool,
#: and that is a judgement for the reviewer.
_INSTRUMENT_BY_DESIGN: dict[str, str] = {
    "randomised_experiment": "RoB 2",
    "quasi_experiment": "MMAT",
    "cross_sectional": "AXIS",
    "longitudinal": "AXIS",
    "case_study": "CASP",
    "qualitative": "CASP",
    "mixed_methods": "MMAT",
    "review": "CASP",
    "conceptual": "none",
    "unclear": "none",
}


def instrument_for(design_family: str) -> str:
    """The appraisal instrument that normally fits a design."""
    return _INSTRUMENT_BY_DESIGN.get(design_family, "none")


def _parse_page(value: object) -> int | None:
    """Read a page number out of whatever shape it arrived in.

    Models write "p. 7", "page 7", "7", 7 and occasionally "7-8"; a range
    takes its first page.
    """
    if value is None or isinstance(value, int):
        return value
    digits = ""
    for char in str(value).strip():
        if char.isdigit():
            digits += char
        elif digits:
            break
    return int(digits) if digits else None


class Theory(BaseModel):
    name: str
    role: str = Field(
        default="",
        description="How the report uses it: framing, hypotheses, interpretation",
    )

    model_config = {"extra": "ignore"}

    @field_validator("name", "role", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()


class Relationship(BaseModel):
    """One relationship a report states between two things it measured."""

    predictor_label: str = Field(description="Exactly as the authors label it")
    outcome_label: str = Field(description="Exactly as the authors label it")
    relationship_type: Literal[
        "association", "prediction", "causal_effect",
        "mediation", "moderation", "qualitative_link",
    ] = "association"
    mediator_label: str = ""
    moderator_label: str = ""
    direction: Literal["positive", "negative", "none", "mixed", "unclear"] = "unclear"
    strength: str = ""
    statistic: str = ""
    significance: Literal[
        "significant", "not_significant", "not_reported", "not_applicable"
    ] = "not_reported"
    evidence_basis: Literal["statistical", "thematic", "asserted"] = "asserted"
    quote: str = ""
    quote_page: int | None = None

    model_config = {"extra": "ignore"}

    @field_validator(
        "predictor_label", "outcome_label", "mediator_label", "moderator_label",
        "strength", "statistic", "quote", mode="before",
    )
    @classmethod
    def _as_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("quote_page", mode="before")
    @classmethod
    def _as_page(cls, value: object) -> object:
        return _parse_page(value)

    @field_validator("direction", "relationship_type", "significance",
                     "evidence_basis", mode="before")
    @classmethod
    def _normalise_enum(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().casefold().replace(" ", "_").replace("-", "_")
        return value

    @model_validator(mode="after")
    def _check(self) -> Relationship:
        if not self.predictor_label or not self.outcome_label:
            raise ValueError(
                "a relationship needs both a predictor and an outcome, labelled "
                "as the authors labelled them"
            )
        if self.quote and self.quote_page is None:
            raise ValueError(
                "a quotation must say which page it is on, so it can be checked"
            )
        if self.quote_page is not None and not self.quote:
            object.__setattr__(self, "quote_page", None)
        # A statistical claim without a statistic is an assertion dressed up
        # as one; say so rather than letting the synthesis weigh it as data.
        if self.evidence_basis == "statistical" and not (
            self.statistic or self.strength
        ):
            object.__setattr__(self, "evidence_basis", "asserted")
        if self.relationship_type == "mediation" and not self.mediator_label:
            raise ValueError("a mediation needs the mediator the authors name")
        if self.relationship_type == "moderation" and not self.moderator_label:
            raise ValueError("a moderation needs the moderator the authors name")
        return self


class ReportExtraction(BaseModel):
    """The study description, one per report."""

    country: str = ""
    setting: str = ""
    population: str = ""

    design: str = Field(default="", description="As the authors describe it")
    design_family: DesignFamily = "unclear"
    manipulated_treatment: bool = False
    control_group: bool = False
    design_note: str = ""

    sampling: str = ""
    sample_size: int | None = None
    sample_size_text: str = ""
    time_horizon: str = ""

    theories: list[Theory] = Field(default_factory=list)
    framework_stages: list[str] = Field(default_factory=list)
    key_findings: str = ""
    limitations: str = ""

    appraisal_instrument: Literal["RoB 2", "AXIS", "CASP", "MMAT", "none"] = "none"
    appraisal_note: str = ""
    appraisal_concern: Literal["low", "moderate", "high", "unclear"] = "unclear"

    quote: str = ""
    quote_page: int | None = None

    #: Set by the validator when the experiment rule was applied.
    design_downgraded: bool = False

    model_config = {"extra": "ignore"}

    @field_validator(
        "country", "setting", "population", "design", "design_note", "sampling",
        "sample_size_text", "time_horizon", "key_findings", "limitations",
        "appraisal_note", "quote", mode="before",
    )
    @classmethod
    def _as_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("design_family", "appraisal_concern", mode="before")
    @classmethod
    def _normalise_enum(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().casefold().replace(" ", "_").replace("-", "_")
        return value

    @field_validator("appraisal_instrument", mode="before")
    @classmethod
    def _normalise_instrument(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip()
        lookup = {
            "rob2": "RoB 2", "rob 2": "RoB 2", "rob-2": "RoB 2",
            "cochrane rob 2": "RoB 2",
            "axis": "AXIS", "casp": "CASP", "mmat": "MMAT",
            "": "none", "n/a": "none", "na": "none", "none": "none",
        }
        return lookup.get(text.casefold(), text)

    @field_validator("sample_size", mode="before")
    @classmethod
    def _as_size(cls, value: object) -> object:
        if value is None or isinstance(value, int):
            return value
        text = str(value).replace(",", "").strip()
        digits = ""
        for char in text:
            if char.isdigit():
                digits += char
            elif digits:
                break
        return int(digits) if digits else None

    @field_validator("quote_page", mode="before")
    @classmethod
    def _as_page(cls, value: object) -> object:
        return _parse_page(value)

    @field_validator("framework_stages", mode="before")
    @classmethod
    def _as_list(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(";") if part.strip()]
        return value

    @model_validator(mode="after")
    def _apply_rules(self) -> ReportExtraction:
        # The experiment rule. Nothing is called an experiment without both a
        # manipulated treatment and something to compare it against.
        if self.design_family == "randomised_experiment" and not (
            self.manipulated_treatment and self.control_group
        ):
            missing = []
            if not self.manipulated_treatment:
                missing.append("no manipulated treatment is reported")
            if not self.control_group:
                missing.append("no control or comparison group is reported")
            object.__setattr__(self, "design_family", "quasi_experiment")
            object.__setattr__(self, "design_downgraded", True)
            note = (
                "Recorded as an experiment but " + " and ".join(missing)
                + "; treated as quasi-experimental."
            )
            object.__setattr__(
                self, "design_note", (self.design_note + " " + note).strip()
            )
        if self.quote and self.quote_page is None:
            raise ValueError(
                "a quotation must say which page it is on, so it can be checked"
            )
        if self.quote_page is not None and not self.quote:
            object.__setattr__(self, "quote_page", None)
        return self

    @property
    def expected_instrument(self) -> str:
        return instrument_for(self.design_family)

    @property
    def instrument_matches_design(self) -> bool:
        return self.appraisal_instrument == self.expected_instrument


class ExtractionAnswer(BaseModel):
    """What one extraction call returns: the report, and its relationships."""

    report: ReportExtraction
    relationships: list[Relationship] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class DirectionCheck(BaseModel):
    """The verification pass: a direction re-derived from a quotation alone."""

    direction: Literal["positive", "negative", "none", "mixed", "unclear"]
    note: str = ""

    model_config = {"extra": "ignore"}

    @field_validator("direction", mode="before")
    @classmethod
    def _normalise(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().casefold()
            return {
                "no relationship": "none", "null": "none", "no association": "none",
                "cannot tell": "unclear", "not stated": "unclear",
            }.get(text, text)
        return value

    @field_validator("note", mode="before")
    @classmethod
    def _as_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()
