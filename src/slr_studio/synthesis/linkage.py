"""Scoring whether two reports draw on the same fieldwork.

Offline and cheap, so that it can run over every pair before any model is
asked. A review of eighty reports has 3,160 pairs; asking about all of them
would be slow and expensive, and almost all of them are plainly unrelated.

The score is deliberately not a verdict. It decides which pairs are worth a
closer look, and a person decides every merge. The asymmetry matters: a
wrong merge silently deletes a vote, and a wrong split silently doubles one,
so neither mistake announces itself in the output.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["score_shared_dataset", "author_surnames", "overlap"]

#: How much each signal contributes. Authors are weighted highest because a
#: shared sample almost always means a shared team, and sample size because
#: two independent surveys rarely land on exactly the same n.
_WEIGHTS = {
    "authors": 0.35,
    "sample_size": 0.25,
    "setting": 0.20,
    "country": 0.10,
    "time_horizon": 0.10,
}


def author_surnames(authors: Any) -> set[str]:
    """Surnames from however the importers stored the author list."""
    if not authors:
        return set()
    if isinstance(authors, (list, tuple)):
        parts = [str(a) for a in authors]
    else:
        text = str(authors)
        if text.startswith("["):
            import json

            try:
                parts = [str(a) for a in json.loads(text)]
            except ValueError:
                parts = re.split(r"[;|]", text)
        else:
            parts = re.split(r"[;|]", text)
    surnames = set()
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        # "Smith, J." and "J. Smith" both reduce to "smith".
        surname = cleaned.split(",")[0] if "," in cleaned else cleaned.split()[-1]
        surname = re.sub(r"[^a-z]", "", surname.casefold())
        if len(surname) > 1:
            surnames.add(surname)
    return surnames


def overlap(a: set[str], b: set[str]) -> float:
    """Jaccard overlap, 0 when either side is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _words(value: Any) -> set[str]:
    text = re.sub(r"[^a-z0-9 ]", " ", str(value or "").casefold())
    return {w for w in text.split() if len(w) > 2}


def score_shared_dataset(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[float, dict[str, Any]]:
    """How much two reports look like one body of fieldwork.

    Returns the score and the signals behind it, so that what the reviewer
    sees is the evidence rather than a bare number.
    """
    signals: dict[str, Any] = {}
    score = 0.0

    surnames_a, surnames_b = author_surnames(a.get("authors")), author_surnames(
        b.get("authors")
    )
    shared_authors = sorted(surnames_a & surnames_b)
    if shared_authors:
        author_score = overlap(surnames_a, surnames_b)
        score += _WEIGHTS["authors"] * author_score
        signals["authors"] = shared_authors

    size_a, size_b = a.get("sample_size"), b.get("sample_size")
    if size_a and size_b:
        if size_a == size_b:
            score += _WEIGHTS["sample_size"]
            signals["sample_size"] = f"both n = {size_a}"
        else:
            # Near-identical sizes still suggest one sample with a few cases
            # dropped for one analysis.
            ratio = min(size_a, size_b) / max(size_a, size_b)
            if ratio > 0.95:
                score += _WEIGHTS["sample_size"] * 0.6
                signals["sample_size"] = f"n = {size_a} and {size_b}"

    setting_overlap = overlap(_words(a.get("setting")), _words(b.get("setting")))
    if setting_overlap:
        score += _WEIGHTS["setting"] * setting_overlap
        signals["setting"] = round(setting_overlap, 3)

    country_a = str(a.get("country") or "").strip().casefold()
    country_b = str(b.get("country") or "").strip().casefold()
    if country_a and country_a == country_b:
        score += _WEIGHTS["country"]
        signals["country"] = a.get("country")

    horizon = overlap(_words(a.get("time_horizon")), _words(b.get("time_horizon")))
    if horizon:
        score += _WEIGHTS["time_horizon"] * horizon
        signals["time_horizon"] = round(horizon, 3)

    # Shared authors are close to necessary: the same fieldwork reported by
    # two disjoint teams is rare enough that, without them, the remaining
    # signals are much weaker evidence than their sum suggests.
    if not shared_authors:
        score *= 0.5
        signals["no_shared_authors"] = True

    return round(min(1.0, score), 4), signals
