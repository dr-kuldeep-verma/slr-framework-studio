"""What the framework calls must return before anything is stored.

Two rules are enforced here rather than trusted to the prompt.

**A proposition needs a mechanism and a falsifying test.** The spec asks for
both, and without them what comes back is a restatement of the finding rather
than a proposition: a claim that cannot be wrong is not a contribution. A
model that omits either is answered again rather than stored.

**A citation has to be a link id.** The model is asked to cite links by id,
and a citation like "the trust paper" cannot be checked against the graded
inventory at all. Refusing it here means the check further on is a lookup
rather than a guess about what was meant.

Whether the cited links *exist* is not decided here. That is the engine's job,
because only the engine can see the inventory.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ELEMENT_KINDS",
    "ProposedProposition",
    "PropositionAnswer",
    "ProposedTheme",
    "ThemeAnswer",
]

#: The three kinds the framework keeps apart.
ELEMENT_KINDS = ("evidenced_link", "proposed_mechanism", "untested_pathway")

_LINK_ID = re.compile(r"^\s*L?(\d+)\s*$", re.IGNORECASE)


class ProposedTheme(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    #: Construct names, as they appear in the approved codebook.
    constructs: list[str] = Field(default_factory=list)

    @field_validator("name", "description")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return " ".join(value.split())


class ThemeAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    themes: list[ProposedTheme] = Field(default_factory=list)


class ProposedProposition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str = Field(min_length=10, max_length=600)
    mechanism: str = Field(min_length=1, max_length=900)
    theories: list[str] = Field(default_factory=list)
    moderators: str = ""
    boundary_conditions: str = ""
    falsifying_test: str = Field(min_length=1, max_length=900)
    element_kind: Literal[
        "evidenced_link", "proposed_mechanism", "untested_pathway"
    ] = "evidenced_link"
    #: Link ids from the graded inventory, as "L7" or "7".
    cited_links: list[str] = Field(default_factory=list)
    theme: str = ""

    @field_validator("statement", "mechanism", "falsifying_test", "moderators",
                     "boundary_conditions")
    @classmethod
    def _tidy(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("cited_links")
    @classmethod
    def _citations_are_ids(cls, value: list[str]) -> list[str]:
        for citation in value:
            if not _LINK_ID.match(str(citation)):
                raise ValueError(
                    f"{citation!r} is not a link id. Propositions cite links "
                    "from the graded inventory by id, such as 'L7', so that "
                    "every citation can be checked."
                )
        return value

    def link_ids(self) -> list[int]:
        """The cited ids as integers, in the order given, without repeats."""
        seen: list[int] = []
        for citation in self.cited_links:
            match = _LINK_ID.match(str(citation))
            if match is None:          # unreachable; the validator ran first
                continue
            number = int(match.group(1))
            if number not in seen:
                seen.append(number)
        return seen


class PropositionAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    propositions: list[ProposedProposition] = Field(default_factory=list)
