"""Checking whether a gap is already being closed.

Before reporting a gap the application asks OpenAlex and Crossref whether
anything has appeared since the search date that matches the gap's
constructs. A hit does not make the gap wrong -- the review's corpus still
has the hole -- so the gap is reported either way, marked for the reviewer to
check. Suppressing it would hide a real property of the corpus; ignoring the
hit would let a reviewer claim nobody has looked at something published last
month.

No test touches the network: the fetcher is injected, exactly as the
Unpaywall client's is.
"""

from __future__ import annotations

import pytest

from slr_studio.gaps.freshness import FreshnessChecker, FreshnessResult


def openalex(titles, *, year=2026):
    return {
        "results": [
            {
                "id": f"https://openalex.org/W{i}",
                "title": title,
                "publication_year": year,
                "publication_date": f"{year}-06-01",
                "doi": f"https://doi.org/10.1000/w{i}",
            }
            for i, title in enumerate(titles, start=1)
        ]
    }


def crossref(titles, *, year=2026):
    return {
        "message": {
            "items": [
                {
                    "title": [title],
                    "DOI": f"10.1000/c{i}",
                    "issued": {"date-parts": [[year, 6, 1]]},
                }
                for i, title in enumerate(titles, start=1)
            ]
        }
    }


class TestTheQuery:
    def test_it_asks_only_for_work_after_the_search_date(self):
        seen = []

        def fetch(url, **kwargs):
            seen.append(url)
            return openalex([])

        FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["efficacy", "climate concern"], searched_to="2026-03-01"
        )
        assert any("2026-03-01" in url for url in seen)

    def test_the_constructs_are_in_the_query(self):
        seen = []

        def fetch(url, **kwargs):
            seen.append(url)
            return openalex([])

        FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["efficacy", "climate concern"], searched_to="2026-03-01"
        )
        joined = " ".join(seen).casefold()
        assert "efficacy" in joined

    def test_the_email_goes_in_the_polite_pool(self):
        seen = []

        def fetch(url, **kwargs):
            seen.append(url)
            return openalex([])

        FreshnessChecker(email="tester@example.org", fetch=fetch).check(
            ["trust"], searched_to="2026-03-01"
        )
        from urllib.parse import unquote_plus

        # Percent-encoded in the query string, as it must be.
        assert any("tester@example.org" in unquote_plus(url) for url in seen)


class TestTheVerdict:
    def test_nothing_found_is_reported_as_nothing_found(self):
        result = FreshnessChecker(
            email="a@b.org", fetch=lambda url, **k: openalex([])
        ).check(["trust"], searched_to="2026-03-01")
        assert result.status == "none_found"
        assert result.candidates == ()

    def test_a_matching_paper_marks_the_gap_as_possibly_addressed(self):
        result = FreshnessChecker(
            email="a@b.org",
            fetch=lambda url, **k: openalex(
                ["Trust and participation in coastal planning"]
            ),
        ).check(["trust", "participation"], searched_to="2026-03-01")
        assert result.status == "possibly_addressed"
        assert result.candidates

    def test_a_candidate_keeps_its_title_year_and_identifier(self):
        result = FreshnessChecker(
            email="a@b.org",
            fetch=lambda url, **k: openalex(["Trust and participation"]),
        ).check(["trust", "participation"], searched_to="2026-03-01")
        first = result.candidates[0]
        assert first.title and first.year == 2026 and first.identifier

    def test_a_paper_matching_only_one_construct_is_not_a_match(self):
        """Both ends have to appear, or every gap looks addressed."""
        result = FreshnessChecker(
            email="a@b.org",
            fetch=lambda url, **k: openalex(["Trust in local government"]),
        ).check(["trust", "participation"], searched_to="2026-03-01")
        assert result.status == "none_found"

    def test_a_network_failure_is_reported_not_raised(self):
        """A failed check must not stop the gap being reported."""

        def fetch(url, **kwargs):
            raise RuntimeError("connection reset")

        result = FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["trust"], searched_to="2026-03-01"
        )
        assert result.status == "check_failed"
        assert "connection reset" in result.detail

    def test_a_missing_search_date_is_refused(self):
        result = FreshnessChecker(
            email="a@b.org", fetch=lambda url, **k: openalex([])
        ).check(["trust"], searched_to="")
        assert result.status == "check_failed"
        assert "search" in result.detail.casefold()

    def test_both_sources_are_consulted(self):
        seen = []

        def fetch(url, **kwargs):
            seen.append(url)
            if "openalex" in url:
                return openalex([])
            return crossref([])

        FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["trust"], searched_to="2026-03-01"
        )
        assert any("openalex" in url for url in seen)
        assert any("crossref" in url for url in seen)

    def test_a_crossref_hit_counts_too(self):
        def fetch(url, **kwargs):
            if "openalex" in url:
                return openalex([])
            return crossref(["Trust and participation in planning"])

        result = FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["trust", "participation"], searched_to="2026-03-01"
        )
        assert result.status == "possibly_addressed"
        assert result.candidates[0].source == "crossref"

    def test_one_source_failing_does_not_lose_the_other(self):
        def fetch(url, **kwargs):
            if "openalex" in url:
                raise RuntimeError("openalex is down")
            return crossref(["Trust and participation"])

        result = FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["trust", "participation"], searched_to="2026-03-01"
        )
        assert result.status == "possibly_addressed"
        assert "openalex is down" in result.detail

    def test_the_same_paper_from_both_sources_is_listed_once(self):
        def fetch(url, **kwargs):
            if "openalex" in url:
                return {
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "title": "Trust and participation",
                            "publication_year": 2026,
                            "doi": "https://doi.org/10.1000/same",
                        }
                    ]
                }
            return {
                "message": {
                    "items": [
                        {
                            "title": ["Trust and participation"],
                            "DOI": "10.1000/same",
                            "issued": {"date-parts": [[2026, 6, 1]]},
                        }
                    ]
                }
            }

        result = FreshnessChecker(email="a@b.org", fetch=fetch).check(
            ["trust", "participation"], searched_to="2026-03-01"
        )
        assert len(result.candidates) == 1


class TestItDoesNotSuppressTheGap:
    def test_the_result_never_says_the_gap_is_closed(self):
        """Only the reviewer can decide that, after reading the paper."""
        result = FreshnessChecker(
            email="a@b.org",
            fetch=lambda url, **k: openalex(["Trust and participation"]),
        ).check(["trust", "participation"], searched_to="2026-03-01")
        assert result.status in {
            "none_found", "possibly_addressed", "check_failed"
        }
        assert result.status != "closed"
