"""Prompts for extraction and for the separate direction check.

Two of the choices here matter more than the wording.

**Labels are kept as the authors wrote them.** The extraction is told not to
tidy "perceived fairness of benefit distribution" into "fairness". Mapping
labels onto shared constructs is the synthesis stage's job, and it has to be
justified against the original wording — wording that paraphrasing here would
destroy before anyone saw it.

**The verification pass is blind.** It is given the quotation and the two
labels, and nothing else: not the direction the extraction recorded, not its
reasoning, not the rest of the paper. A check that can see the answer it is
checking is not a check.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PROMPT_VERSION",
    "extraction_system",
    "extraction_user",
    "direction_system",
    "direction_user",
]

PROMPT_VERSION = "extract-1.0"

_EXTRACTION_RULES = """
Rules you must follow:

Labels
- Copy the authors' own labels for predictors, outcomes, mediators and
  moderators, exactly as they write them. Do not shorten, tidy or standardise
  them. "Perceived fairness of benefit distribution" is not "fairness".

Design
- `design` is how the authors describe their study. `design_family` is which
  of the listed families it belongs to.
- Only answer "randomised_experiment" when the report shows BOTH a treatment
  the researchers manipulated AND a control or comparison group. Set
  `manipulated_treatment` and `control_group` to say what the report actually
  shows. A study the authors call an experiment but which has neither is not
  one.

Relationships
- Record one entry for every relationship the report states between two things
  it measured or observed. If it reports none, return an empty list rather
  than inventing one.
- `direction` is the direction the report gives: positive, negative, none,
  mixed, or unclear when it does not say.
- `evidence_basis` is "statistical" when a statistic is reported, "thematic"
  when it rests on qualitative analysis, and "asserted" when the report simply
  claims it.

Quotations
- Every relationship needs a quotation of up to twenty-five words, copied
  exactly from the report, and the PDF page it is on. Pages are labelled in
  the text below.
- Each quotation is checked against that page automatically, and a separate
  pass re-derives the direction from the quotation alone. Copy the words that
  actually state the relationship, not a nearby summary.
- Never invent a quotation. If nothing in the report states the relationship
  in words, leave the quotation empty and set `evidence_basis` to "asserted".

Appraisal
- Choose the instrument that suits the design: RoB 2 for randomised trials,
  AXIS for cross-sectional studies, CASP for qualitative work and reviews,
  MMAT for mixed-methods and quasi-experimental work, "none" when none fits.
- `appraisal_concern` is your overall concern about the study's conduct: low,
  moderate, high, or unclear.

Anything the report does not say, leave empty. Do not fill a gap with what is
usually true.
""".strip()

_SHAPE = """
Answer with a single JSON object and nothing else:
{"report": {
   "country": "", "setting": "", "population": "",
   "design": "", "design_family": "<one of the families listed above>",
   "manipulated_treatment": true|false, "control_group": true|false,
   "design_note": "",
   "sampling": "", "sample_size": <number or null>, "sample_size_text": "",
   "time_horizon": "",
   "theories": [{"name": "", "role": ""}],
   "framework_stages": [""],
   "key_findings": "", "limitations": "",
   "appraisal_instrument": "RoB 2"|"AXIS"|"CASP"|"MMAT"|"none",
   "appraisal_note": "", "appraisal_concern": "low"|"moderate"|"high"|"unclear",
   "quote": "", "quote_page": <page number or null>},
 "relationships": [
   {"predictor_label": "", "outcome_label": "",
    "relationship_type": "association"|"prediction"|"causal_effect"|
                         "mediation"|"moderation"|"qualitative_link",
    "mediator_label": "", "moderator_label": "",
    "direction": "positive"|"negative"|"none"|"mixed"|"unclear",
    "strength": "", "statistic": "",
    "significance": "significant"|"not_significant"|"not_reported"|"not_applicable",
    "evidence_basis": "statistical"|"thematic"|"asserted",
    "quote": "", "quote_page": <page number>}]}
""".strip()


def _families_block(families: tuple[str, ...]) -> str:
    return "Design families: " + ", ".join(families) + "."


def extraction_system(
    protocol_text: str,
    families: tuple[str, ...],
    *,
    framework_stages: list[str] | None = None,
) -> str:
    stages = framework_stages or []
    stage_block = (
        "Framework stages this review is organised around: "
        + ", ".join(stages)
        + ". Say which of them the report covers."
        if stages
        else "This review has no framework stages defined; leave "
        "`framework_stages` empty."
    )
    return "\n\n".join(
        [
            "You are extracting data from a report included in a systematic "
            "review that follows PRISMA 2020.",
            "Everything you record will be counted in the synthesis, so it "
            "must come from this report and nowhere else.",
            "THE PROTOCOL\n\n" + protocol_text.strip(),
            _families_block(families),
            stage_block,
            _EXTRACTION_RULES,
            _SHAPE,
        ]
    )


def extraction_user(record: dict[str, Any], report_text: str) -> str:
    head = [
        "THE RECORD AS INDEXED",
        f"Title: {record.get('title') or '(no title)'}",
        f"Year: {record.get('year') or 'not stated'}",
        f"Publication: {record.get('journal') or 'not stated'}",
    ]
    return "\n".join(head) + "\n\n" + report_text + (
        "\n\nExtract the study description and every relationship this report "
        "states."
    )


def direction_system() -> str:
    """The blind check.

    It is never told what the extraction concluded, so agreement between the
    two is evidence rather than an echo.
    """
    return "\n\n".join(
        [
            "You are checking one sentence from a research report.",
            "You will be given two things the report measured and a quotation "
            "from it. Say which direction of relationship the quotation states "
            "between them, using only the words in the quotation.",
            "- \"positive\": more of the first goes with more of the second.\n"
            "- \"negative\": more of the first goes with less of the second.\n"
            "- \"none\": the quotation states there is no relationship.\n"
            "- \"mixed\": it states the relationship differs by case or group.\n"
            "- \"unclear\": the quotation does not state a direction, or does "
            "not state a relationship between these two things at all.",
            "Do not infer from what is usually found, from the labels alone, "
            "or from what the authors probably meant. If the words do not say "
            "it, answer \"unclear\".",
            'Answer with a single JSON object and nothing else:\n'
            '{"direction": "positive"|"negative"|"none"|"mixed"|"unclear",\n'
            ' "note": "<a few words on what in the quotation settles it>"}',
        ]
    )


def direction_user(predictor: str, outcome: str, quote: str) -> str:
    return (
        f"First thing measured: {predictor}\n"
        f"Second thing measured: {outcome}\n\n"
        f"Quotation from the report:\n\"{quote}\"\n\n"
        "Which direction does the quotation state?"
    )
