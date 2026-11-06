"""What the Gap Finder asks the model.

Only one step calls out: drafting a study from a gap. Everything else -- the
clustering, the seven detectors, the counts, the ranking -- is arithmetic over
the coded evidence, and a model would only add a way for it to be wrong.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

__all__ = ["PROMPT_VERSION", "study_system", "study_user"]

PROMPT_VERSION = "gaps-1"


def study_system(propositions: Sequence[dict[str, Any]]) -> str:
    listed = (
        "\n".join(
            f"- P{p['number']}: {p['statement']}"
            for p in propositions
            if p.get("number")
        )
        or "- (none; say so rather than inventing one)"
    )
    return f"""You draft the study that would close one gap in a systematic
review. You are given the gap, the counts behind it, and the review's
propositions.

Each draft must have:
- a research question, in one sentence;
- a testable hypothesis, naming the proposition it tests where one applies;
- a design that could actually answer it;
- the measures it would need;
- the population and the setting that would close this particular gap.

Hard rules:
- The gap statement tells you what is missing from *this corpus*. Do not
  claim the gap exists in the literature at large.
- Cite a proposition only from the list below, by its number.
- Do not invent findings, studies or citations. You are proposing work, not
  reporting it.
- The population and setting must be the ones the gap names. A gap about
  low-income countries is not closed by another study in Europe.

The review's propositions:
{listed}

Answer as JSON: {{"question": ..., "hypothesis": ..., "design": ...,
"measures": [...], "population": ..., "setting": ..., "proposition": "P1"}}"""


def study_user(gap: dict[str, Any]) -> str:
    counts = gap.get("supporting_counts") or "{}"
    try:
        parsed = json.dumps(json.loads(counts), indent=1, sort_keys=True)
    except (TypeError, ValueError):
        parsed = str(counts)
    return f"""The gap ({gap.get('gap_type')}):

{gap.get('statement')}

The counts behind it:
{parsed}

Priority {gap.get('priority')} (centrality {gap.get('score_centrality')}, size
{gap.get('score_size')}, relevance {gap.get('score_relevance')}, feasibility
{gap.get('score_feasibility')})."""
