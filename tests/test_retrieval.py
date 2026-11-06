"""Retrieval: open-access lookups, matching PDFs to records, and tracking."""

from __future__ import annotations

import pytest

from slr_studio.ai import AIClient, MockProvider
from slr_studio.dedup import DedupEngine
from slr_studio.fulltext.retrieval import (
    AUTO_MATCH_TITLE_SCORE,
    RetrievalError,
    RetrievalService,
)
from slr_studio.fulltext.unpaywall import UnpaywallClient, UnpaywallError
from slr_studio.screening import ScreeningEngine, ScreeningSettings


# -- Unpaywall -------------------------------------------------------------


def payload(**overrides):
    base = {
        "doi": "10.1080/jsts.2021.004512",
        "is_oa": True,
        "oa_status": "green",
        "title": "Resident participation",
        "best_oa_location": {
            "url": "https://repo.example.org/best.pdf",
            "host_type": "repository",
            "version": "publishedVersion",
            "license": "cc-by",
        },
        "oa_locations": [
            {
                "url": "https://repo.example.org/best.pdf",
                "host_type": "repository",
                "version": "publishedVersion",
                "license": "cc-by",
            },
            {
                "url": "https://publisher.example.com/article.pdf",
                "host_type": "publisher",
                "version": "acceptedVersion",
                "license": "",
            },
        ],
    }
    base.update(overrides)
    return base


class TestUnpaywallClient:
    def test_an_email_address_is_required(self):
        with pytest.raises(UnpaywallError, match="email address"):
            UnpaywallClient("")

    def test_a_malformed_email_is_refused(self):
        with pytest.raises(UnpaywallError, match="email address"):
            UnpaywallClient("not-an-email")

    def test_the_email_is_sent_with_the_request(self):
        seen = {}

        def fetch(url, *, timeout):
            seen["url"] = url
            return payload()

        UnpaywallClient("me@example.org", fetch=fetch).lookup("10.1/x")
        assert "email=me@example.org" in seen["url"]
        assert "10.1/x" in seen["url"]

    def test_locations_are_parsed_with_the_best_marked(self):
        result = UnpaywallClient("me@example.org", fetch=lambda u, timeout: payload()).lookup(
            "10.1080/jsts.2021.004512"
        )
        assert result.is_oa and result.oa_status == "green"
        assert len(result.locations) == 2
        assert result.best.url == "https://repo.example.org/best.pdf"
        assert result.best.license == "cc-by"

    def test_a_record_with_no_free_copy_is_not_an_error(self):
        result = UnpaywallClient(
            "me@example.org",
            fetch=lambda u, timeout: payload(is_oa=False, oa_locations=[],
                                             best_oa_location=None),
        ).lookup("10.1/x")
        assert result.is_oa is False
        assert result.locations == []
        assert result.error == ""

    def test_an_unknown_doi_is_reported_not_raised(self):
        result = UnpaywallClient(
            "me@example.org",
            fetch=lambda u, timeout: {"_not_found": True},
        ).lookup("10.1/nope")
        assert "no entry" in result.error

    def test_a_network_failure_is_returned_not_raised(self):
        def fetch(url, *, timeout):
            raise UnpaywallError("Could not reach Unpaywall: timed out")

        result = UnpaywallClient("me@example.org", fetch=fetch).lookup("10.1/x")
        assert "timed out" in result.error
        assert result.locations == []

    def test_a_record_without_a_doi_says_so(self):
        result = UnpaywallClient("me@example.org", fetch=lambda u, timeout: {}).lookup("")
        assert result.error == "the record has no DOI"

    def test_duplicate_urls_are_collapsed(self):
        data = payload()
        data["oa_locations"].append(dict(data["oa_locations"][0]))
        result = UnpaywallClient("me@example.org", fetch=lambda u, timeout: data).lookup("10.1/x")
        assert len(result.locations) == 2


# -- the retrieval service -------------------------------------------------


@pytest.fixture
def screened(imported_project, fixtures):
    """A project screened offline, so some records are sought at full text."""
    DedupEngine(imported_project).run()
    imported_project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    imported_project.protocol.lock(actor="human:test")
    engine = ScreeningEngine(
        imported_project,
        AIClient(imported_project, MockProvider(), stage="screen"),
        ScreeningSettings(),
    )
    engine.waive_pilot("test fixture")
    engine.run_full(actor="human:test")
    return imported_project


@pytest.fixture
def service(screened):
    return RetrievalService(screened)


@pytest.fixture
def pdfs(fixtures):
    return fixtures / "pdfs"


class TestWhatIsSought:
    def test_only_records_that_passed_screening_are_sought(self, service, screened):
        sought = {r["id"] for r in service.sought_records()}
        expected = {
            r["record_id"]
            for r in screened.db.query(
                "SELECT record_id FROM screening_status "
                "WHERE decision IN ('include', 'unclear')"
            )
        }
        assert sought == expected

    def test_records_excluded_at_screening_are_not_sought(self, service, screened):
        excluded = screened.db.query(
            "SELECT record_id FROM screening_status WHERE decision = 'exclude'"
        )
        sought = {r["id"] for r in service.sought_records()}
        assert all(row["record_id"] not in sought for row in excluded)

    def test_the_retrieval_list_carries_a_resolver_link(self, service):
        rows = [r for r in service.retrieval_list() if r["doi"]]
        assert rows and rows[0]["doi_link"].startswith("https://doi.org/")


class TestOpenAccessLookup:
    def test_results_are_stored_against_the_record(self, service, screened):
        client = UnpaywallClient("me@example.org", fetch=lambda u, timeout: payload())
        stats = service.find_open_access(client=client)
        assert stats["n_looked_up"] > 0
        assert stats["n_open_access"] == stats["n_looked_up"]
        assert screened.db.scalar("SELECT COUNT(*) FROM oa_location") > 0

    def test_a_second_pass_does_not_re_ask(self, service):
        calls = {"n": 0}

        def fetch(url, *, timeout):
            calls["n"] += 1
            return payload()

        client = UnpaywallClient("me@example.org", fetch=fetch)
        service.find_open_access(client=client)
        first = calls["n"]
        service.find_open_access(client=client)
        assert calls["n"] == first

    def test_lookups_are_logged(self, service, screened):
        client = UnpaywallClient("me@example.org", fetch=lambda u, timeout: payload())
        service.find_open_access(client=client)
        assert any(
            e.action == "open_access_lookup" for e in screened.audit.entries()
        )
        assert screened.audit.verify() == []

    def test_a_failing_lookup_does_not_stop_the_pass(self, service, screened):
        def fetch(url, *, timeout):
            raise UnpaywallError("service unavailable")

        client = UnpaywallClient("me@example.org", fetch=fetch)
        stats = service.find_open_access(client=client)
        assert stats["n_errors"] == stats["n_looked_up"] > 0
        assert screened.db.scalar("SELECT COUNT(*) FROM oa_lookup") > 0


class TestMatching:
    def test_a_printed_doi_matches_its_record(self, service, screened, pdfs):
        outcome = service.attach_pdf(pdfs / "menon_2021_kerala.pdf")
        assert outcome.matched_by == "doi"
        assert outcome.score == 1.0
        title = screened.db.scalar(
            "SELECT title FROM record WHERE id = ?", (outcome.record_id,)
        )
        assert "Kerala" in title

    def test_a_file_without_a_doi_matches_on_title(self, service, pdfs):
        outcome = service.attach_pdf(pdfs / "okonkwo_2019_benefit_sharing.pdf")
        assert outcome.matched_by == "title"
        assert outcome.score >= AUTO_MATCH_TITLE_SCORE

    def test_a_file_matching_nothing_goes_to_the_manual_screen(self, service, pdfs):
        # This report's record was excluded at screening, so there is no
        # record for it to attach to.
        outcome = service.attach_pdf(pdfs / "pereira_2020_scoping_review.pdf")
        assert outcome.matched is False
        assert outcome.unmatched_id is not None
        assert len(service.unmatched()) == 1

    def test_a_doi_beats_a_better_title_match(self, service, screened):
        # A DOI printed in a file identifies the work; no title score should
        # be able to override it.
        result = service.match("10.1080/jsts.2021.004512", "Something else entirely")
        assert result.matched_by == "doi"

    def test_a_file_with_neither_is_unmatched(self, service):
        result = service.match("", "")
        assert result.record_id is None
        assert "no DOI or title" in result.detail

    def test_the_closest_record_is_remembered_for_the_manual_screen(self, service):
        result = service.match("", "Resident participation in sustainable tourism")
        assert result.best_candidate_id is not None


class TestAttaching:
    def test_pages_are_stored_with_the_report(self, service, screened, pdfs):
        outcome = service.attach_pdf(pdfs / "menon_2021_kerala.pdf")
        pages = service.pages(outcome.record_id)
        assert len(pages) == outcome.n_pages
        assert pages[0]["page_no"] == 1 and pages[0]["n_chars"] > 0

    def test_the_file_is_copied_into_the_project(self, service, screened, pdfs):
        outcome = service.attach_pdf(pdfs / "menon_2021_kerala.pdf")
        report = service.report(outcome.record_id)
        assert (screened.root / report["stored_path"]).is_file()
        assert report["status"] == "retrieved"
        assert report["file_sha256"]

    def test_attaching_is_logged(self, service, screened, pdfs):
        service.attach_pdf(pdfs / "menon_2021_kerala.pdf")
        entry = [
            e for e in screened.audit.entries(stage="retrieve")
            if e.action == "report_attached"
        ][-1]
        assert entry.detail["matched_by"] == "doi"
        assert entry.detail["file_sha256"]
        assert screened.audit.verify() == []

    def test_the_same_file_is_not_attached_to_two_records(self, service, screened, pdfs):
        first = service.attach_pdf(pdfs / "menon_2021_kerala.pdf")
        other = next(
            r["id"] for r in service.sought_records() if r["id"] != first.record_id
        )
        second = service.attach_pdf(pdfs / "menon_2021_kerala.pdf", record_id=other)
        assert second.matched is False
        assert "already attached" in second.message

    def test_a_file_that_is_not_a_pdf_is_reported_not_raised(self, service, tmp_path):
        broken = tmp_path / "notes.pdf"
        broken.write_text("not a pdf", encoding="utf-8")
        outcome = service.attach_pdf(broken)
        assert outcome.matched is False
        assert "could not be opened" in outcome.message

    def test_attaching_many_collects_failures(self, service, pdfs, tmp_path):
        broken = tmp_path / "broken.pdf"
        broken.write_text("nope", encoding="utf-8")
        outcomes = service.attach_many(
            [pdfs / "menon_2021_kerala.pdf", broken]
        )
        assert len(outcomes) == 2
        assert sum(1 for o in outcomes if o.matched) == 1

    def test_ocr_counts_are_recorded(self, service, screened, pdfs):
        outcome = service.attach_pdf(
            pdfs / "pereira_2020_scoping_review.pdf",
            record_id=service.sought_records()[0]["id"],
        )
        report = service.report(outcome.record_id)
        assert report["n_pages"] == 3
        from slr_studio.fulltext.pdf import ocr_available

        if ocr_available():
            assert report["n_pages_ocr"] == 1


class TestManualMatching:
    def test_a_file_can_be_attached_by_hand(self, service, screened, pdfs):
        service.attach_pdf(pdfs / "pereira_2020_scoping_review.pdf")
        item = service.unmatched()[0]
        target = service.sought_records()[0]["id"]
        outcome = service.resolve_unmatched(item["id"], target)
        assert outcome.record_id == target
        assert outcome.matched_by == "manual"
        assert service.unmatched() == []

    def test_matching_by_hand_is_logged(self, service, screened, pdfs):
        service.attach_pdf(pdfs / "pereira_2020_scoping_review.pdf")
        item = service.unmatched()[0]
        service.resolve_unmatched(item["id"], service.sought_records()[0]["id"])
        assert any(
            e.action == "pdf_matched_by_hand" for e in screened.audit.entries()
        )

    def test_a_file_can_be_set_aside(self, service, screened, pdfs):
        service.attach_pdf(pdfs / "pereira_2020_scoping_review.pdf")
        item = service.unmatched()[0]
        service.discard_unmatched(item["id"], "belongs to a different review")
        assert service.unmatched() == []
        assert any(e.action == "pdf_discarded" for e in screened.audit.entries())

    def test_resolving_something_that_is_not_there_is_an_error(self, service):
        with pytest.raises(RetrievalError, match="No unmatched file"):
            service.resolve_unmatched(999, 1)


class TestTracking:
    def test_not_retrieved_needs_a_reason(self, service):
        record_id = service.sought_records()[0]["id"]
        with pytest.raises(RetrievalError, match="Say why"):
            service.set_status(record_id, "not_retrieved")

    def test_not_retrieved_with_a_reason_is_recorded(self, service, screened):
        record_id = service.sought_records()[0]["id"]
        service.set_status(
            record_id, "not_retrieved",
            reason="No copy held by the library and the author did not reply",
        )
        report = service.report(record_id)
        assert report["status"] == "not_retrieved"
        assert "did not reply" in report["reason"]
        assert service.counts()["n_not_retrieved"] == 1

    def test_abstract_only_is_its_own_outcome(self, service):
        record_id = service.sought_records()[0]["id"]
        service.set_status(
            record_id, "abstract_only", reason="Conference abstract; no full paper exists"
        )
        assert service.counts()["n_abstract_only"] == 1

    def test_an_unknown_status_is_refused(self, service):
        with pytest.raises(RetrievalError, match="is not one of"):
            service.set_status(service.sought_records()[0]["id"], "lost")

    def test_status_changes_are_logged(self, service, screened):
        record_id = service.sought_records()[0]["id"]
        service.set_status(record_id, "not_retrieved", reason="paywalled, no copy held")
        entry = [
            e for e in screened.audit.entries(stage="retrieve")
            if e.action == "report_status"
        ][-1]
        assert entry.detail["status"] == "not_retrieved"

    def test_the_prisma_numbers_follow_from_the_statuses(self, service, pdfs):
        service.attach_pdf(pdfs / "menon_2021_kerala.pdf")
        other = next(
            r["id"] for r in service.sought_records()
            if (r.get("status") or "pending") == "pending"
        )
        service.set_status(other, "not_retrieved", reason="no copy found")
        counts = service.counts()
        assert counts["n_retrieved"] == 1
        assert counts["n_not_retrieved"] == 1
        assert counts["n_sought"] >= counts["n_retrieved"] + counts["n_not_retrieved"]
