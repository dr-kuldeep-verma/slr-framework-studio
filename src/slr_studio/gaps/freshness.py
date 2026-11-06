"""Asking whether a gap is already being closed.

A systematic review is out of date the day its search runs. Before reporting
that nothing studies X together with Y, the application asks OpenAlex and
Crossref whether anything matching both has appeared since the search date.

What it does with the answer matters. A hit does not cancel the gap: the
review's corpus still has the hole, and the hole is what the review is
entitled to report. Nor is a hit ignored, because a reviewer who claims
nobody has looked at something published two months ago will hear about it
from a referee. So the gap is reported either way and marked "possibly being
addressed" for the reviewer to check, which is the only honest handling of
evidence the review has not read.

Both services are queried through an injected fetcher, so no test touches the
network, and a failure of either is reported rather than raised: a freshness
check that cannot reach the internet must not stop a gap being reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence
from urllib.parse import quote_plus

__all__ = ["Candidate", "FreshnessChecker", "FreshnessResult"]

OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"

#: How many results to ask each service for. Enough to see whether anything
#: is happening, few enough to stay inside the polite pool.
_ROWS = 20

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class Candidate:
    """A paper that may already be addressing the gap."""

    title: str
    year: int | None
    identifier: str
    source: str
    doi: str = ""


@dataclass(frozen=True)
class FreshnessResult:
    status: str = "not_checked"
    candidates: tuple[Candidate, ...] = ()
    detail: str = ""
    queried: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "queried": list(self.queried),
            "candidates": [
                {
                    "title": c.title,
                    "year": c.year,
                    "identifier": c.identifier,
                    "source": c.source,
                    "doi": c.doi,
                }
                for c in self.candidates
            ],
        }


def _httpx_fetch(url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    import httpx

    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.json()


class FreshnessChecker:
    def __init__(
        self,
        email: str,
        *,
        fetch: Callable[..., dict[str, Any]] | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.email = (email or "").strip()
        self._fetch = fetch or _httpx_fetch
        self.timeout = timeout

    def check(
        self, constructs: Sequence[str], *, searched_to: str
    ) -> FreshnessResult:
        """Look for work since the search date matching all the constructs."""
        terms = [t.strip() for t in constructs if t and t.strip()]
        if not searched_to.strip():
            return FreshnessResult(
                status="check_failed",
                detail=(
                    "The project does not record the date the searches were "
                    "run, so there is no date to look for newer work after."
                ),
            )
        if not terms:
            return FreshnessResult(
                status="check_failed",
                detail="The gap names no constructs to search for.",
            )

        candidates: list[Candidate] = []
        problems: list[str] = []
        queried: list[str] = []

        for source, url, parse in (
            ("openalex", self._openalex_url(terms, searched_to), self._parse_openalex),
            ("crossref", self._crossref_url(terms, searched_to), self._parse_crossref),
        ):
            queried.append(url)
            try:
                payload = self._fetch(url, timeout=self.timeout)
            except Exception as exc:
                problems.append(f"{source}: {exc}")
                continue
            try:
                candidates.extend(parse(payload))
            except Exception as exc:  # pragma: no cover - malformed payload
                problems.append(f"{source}: could not read the reply ({exc})")

        matching = [c for c in candidates if _mentions_all(c.title, terms)]
        unique = _deduplicate(matching)

        if unique:
            return FreshnessResult(
                status="possibly_addressed",
                candidates=tuple(unique),
                detail="; ".join(problems),
                queried=tuple(queried),
            )
        if problems and not candidates:
            return FreshnessResult(
                status="check_failed",
                detail="; ".join(problems),
                queried=tuple(queried),
            )
        return FreshnessResult(
            status="none_found",
            detail="; ".join(problems),
            queried=tuple(queried),
        )

    # -- queries -----------------------------------------------------------

    def _openalex_url(self, terms: Sequence[str], searched_to: str) -> str:
        search = quote_plus(" ".join(terms))
        parts = [
            f"search={search}",
            f"filter=from_publication_date:{searched_to}",
            f"per-page={_ROWS}",
        ]
        if self.email:
            parts.append(f"mailto={quote_plus(self.email)}")
        return f"{OPENALEX}?" + "&".join(parts)

    def _crossref_url(self, terms: Sequence[str], searched_to: str) -> str:
        parts = [
            f"query.bibliographic={quote_plus(' '.join(terms))}",
            f"filter=from-pub-date:{searched_to}",
            f"rows={_ROWS}",
        ]
        if self.email:
            parts.append(f"mailto={quote_plus(self.email)}")
        return f"{CROSSREF}?" + "&".join(parts)

    # -- replies -----------------------------------------------------------

    @staticmethod
    def _parse_openalex(payload: dict[str, Any]) -> list[Candidate]:
        out = []
        for work in payload.get("results") or []:
            doi = str(work.get("doi") or "").replace("https://doi.org/", "")
            out.append(
                Candidate(
                    title=str(work.get("title") or "").strip(),
                    year=work.get("publication_year"),
                    identifier=str(work.get("id") or doi),
                    source="openalex",
                    doi=doi,
                )
            )
        return out

    @staticmethod
    def _parse_crossref(payload: dict[str, Any]) -> list[Candidate]:
        out = []
        for item in (payload.get("message") or {}).get("items") or []:
            titles = item.get("title") or []
            parts = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
            doi = str(item.get("DOI") or "")
            out.append(
                Candidate(
                    title=str(titles[0] if titles else "").strip(),
                    year=parts[0] if parts else None,
                    identifier=doi,
                    source="crossref",
                    doi=doi,
                )
            )
        return out


def _mentions_all(title: str, terms: Sequence[str]) -> bool:
    """Whether the title covers every construct the gap is about.

    Both ends, not either: a gap about efficacy *and* climate concern is not
    addressed by another paper about efficacy, and matching on either would
    mark every gap in the review as possibly addressed.
    """
    words = {m.group(0).casefold() for m in _WORD.finditer(title or "")}
    for term in terms:
        needed = {m.group(0).casefold() for m in _WORD.finditer(term)}
        if not needed:
            continue
        if not needed & words:
            return False
    return True


def _deduplicate(candidates: Sequence[Candidate]) -> list[Candidate]:
    """One row per paper, however many services returned it."""
    seen: dict[str, Candidate] = {}
    for candidate in candidates:
        key = (candidate.doi or candidate.title).casefold().strip()
        if key and key not in seen:
            seen[key] = candidate
    return list(seen.values())
