"""Reading PDFs, and checking quotations against the pages they cite."""

from __future__ import annotations

import pytest

from slr_studio.fulltext.pdf import (
    MIN_CHARS_FOR_TEXT_LAYER,
    PdfError,
    PdfPage,
    extract_pdf,
    find_quote,
    normalise_for_match,
    ocr_available,
)


@pytest.fixture
def pdfs(fixtures):
    return fixtures / "pdfs"


@pytest.fixture
def menon(pdfs):
    return extract_pdf(pdfs / "menon_2021_kerala.pdf")


class TestExtraction:
    def test_pages_are_numbered_from_one(self, menon):
        assert [page.page_no for page in menon.pages] == list(
            range(1, menon.n_pages + 1)
        )

    def test_text_is_read_from_the_text_layer(self, menon):
        assert menon.has_text_layer is True
        assert menon.n_pages_ocr == 0
        assert "panchayats" in menon.text()

    def test_the_doi_printed_in_the_file_is_found(self, menon):
        assert menon.doi_guess == "10.1080/jsts.2021.004512"

    def test_the_title_is_guessed_from_the_first_page(self, menon):
        assert menon.title_guess.startswith("Resident participation")

    def test_a_file_with_no_printed_doi_reports_none(self, pdfs):
        document = extract_pdf(pdfs / "okonkwo_2019_benefit_sharing.pdf")
        assert document.doi_guess == ""
        assert document.title_guess.startswith("Benefit sharing")

    def test_a_missing_file_is_a_clear_error(self, tmp_path):
        with pytest.raises(PdfError, match="No file at"):
            extract_pdf(tmp_path / "nothing.pdf")

    def test_a_file_that_is_not_a_pdf_is_a_clear_error(self, tmp_path):
        path = tmp_path / "notes.pdf"
        path.write_text("this is not a PDF at all", encoding="utf-8")
        with pytest.raises(PdfError, match="could not be opened"):
            extract_pdf(path)

    def test_reading_can_be_limited_to_the_first_pages(self, pdfs):
        document = extract_pdf(pdfs / "menon_2021_kerala.pdf", max_pages=1)
        assert document.n_pages == 1

    def test_a_page_can_be_fetched_by_its_number(self, menon):
        assert menon.page(1) is not None
        assert menon.page(999) is None


@pytest.mark.skipif(not ocr_available(), reason="Tesseract is not installed")
class TestOcrFallback:
    @pytest.fixture
    def with_scan(self, pdfs):
        return extract_pdf(pdfs / "pereira_2020_scoping_review.pdf")

    def test_a_page_with_no_text_layer_is_read_by_ocr(self, with_scan):
        assert with_scan.n_pages_ocr == 1
        scanned = [page for page in with_scan.pages if page.ocr]
        assert len(scanned) == 1
        assert "search" in scanned[0].text.casefold()

    def test_pages_that_have_text_are_not_sent_to_ocr(self, with_scan):
        assert all(not page.ocr for page in with_scan.pages[:2])

    def test_turning_ocr_off_leaves_the_page_empty_and_says_so(self, pdfs):
        document = extract_pdf(
            pdfs / "pereira_2020_scoping_review.pdf", use_ocr=False
        )
        assert document.n_pages_ocr == 0
        assert len(document.pages[2].text) < MIN_CHARS_FOR_TEXT_LAYER


class TestNormalisation:
    def test_case_and_punctuation_are_ignored(self):
        assert normalise_for_match("The Study, Part II.") == "the study part ii"

    def test_line_breaks_collapse(self):
        assert normalise_for_match("one\ntwo   three") == "one two three"

    def test_hyphenation_across_a_line_break_is_rejoined(self):
        assert normalise_for_match("partici-\npation") == "participation"

    def test_curly_quotes_match_straight_ones(self):
        assert normalise_for_match("the “residents’” view") == (
            normalise_for_match('the "residents\'" view')
        )

    def test_accents_are_folded(self):
        assert normalise_for_match("Résumé") == normalise_for_match("Resume")

    def test_word_order_is_not_touched(self):
        assert normalise_for_match("a b") != normalise_for_match("b a")


class TestLocatingAQuoteInThePage:
    """Highlighting the quoted words where the reviewer can see them."""

    def test_a_quotation_running_across_a_line_break_is_located(self, menon):
        from slr_studio.fulltext.pdf import locate_quote_span

        text = menon.pages[0].text
        span = locate_quote_span(text, "surveyed 814 households across eleven panchayats")
        assert span is not None
        assert "panchayats" in text[span[0]:span[1]]

    def test_hyphenation_across_a_break_is_located(self):
        from slr_studio.fulltext.pdf import locate_quote_span

        text = "the partici-\npation rate was high"
        span = locate_quote_span(text, "the participation rate")
        assert span is not None
        assert text[span[0]:span[1]] == "the partici-\npation rate"

    def test_words_that_are_not_there_locate_nowhere(self, menon):
        from slr_studio.fulltext.pdf import locate_quote_span

        assert locate_quote_span(menon.pages[0].text, "entirely invented words") is None

    def test_an_empty_quotation_locates_nowhere(self):
        from slr_studio.fulltext.pdf import locate_quote_span

        assert locate_quote_span("some text", "") is None

    def test_the_character_map_agrees_with_plain_normalisation(self, menon):
        from slr_studio.fulltext.pdf import _normalised_with_map

        text = menon.pages[0].text
        mapped, origin = _normalised_with_map(text)
        assert mapped == normalise_for_match(text)
        assert len(origin) == len(mapped)


class TestQuoteChecking:
    @pytest.fixture
    def pages(self, menon):
        return menon.pages

    def test_a_quotation_on_the_cited_page_verifies(self, pages):
        location = find_quote(
            "Participation was strongly associated with support", pages, 2
        )
        assert location.found and location.on_cited_page
        assert location.page_no == 2

    def test_case_and_spacing_differences_do_not_matter(self, pages):
        location = find_quote(
            "participation  was   STRONGLY associated with support", pages, 2
        )
        assert location.found and location.on_cited_page

    def test_a_quotation_spanning_a_line_break_is_found(self, pages):
        # The phrase runs across a wrapped line in the PDF.
        location = find_quote(
            "surveyed 814 households across eleven panchayats", pages, 1
        )
        assert location.found and location.on_cited_page

    def test_the_right_words_on_the_wrong_page_is_its_own_outcome(self, pages):
        location = find_quote(
            "Participation was strongly associated with support", pages, 1
        )
        assert location.found is True
        assert location.on_cited_page is False
        assert location.page_no == 2
        assert "not page 1 as cited" in location.detail

    def test_an_invented_quotation_is_found_nowhere(self, pages):
        location = find_quote("tourism destroys every coastal community", pages, 1)
        assert location.found is False
        assert "do not appear anywhere" in location.detail

    def test_an_empty_quotation_is_not_a_pass(self, pages):
        assert find_quote("", pages, 1).found is False

    def test_a_quotation_too_short_to_mean_anything_is_refused(self, pages):
        location = find_quote("the", pages, 1)
        assert location.found is False
        assert "too short" in location.detail

    def test_a_report_with_no_extracted_text_says_so(self):
        location = find_quote("anything at all here", [], 1)
        assert location.found is False
        assert "no text was extracted" in location.detail

    def test_without_a_cited_page_the_first_hit_is_reported(self, pages):
        location = find_quote(
            "Participation was strongly associated with support", pages, None
        )
        assert location.found and location.page_no == 2

    def test_plain_tuples_work_as_well_as_page_objects(self):
        pages = [(1, "the quick brown fox jumps over the lazy dog")]
        assert find_quote("quick brown fox jumps", pages, 1).on_cited_page is True

    def test_a_page_object_list_is_accepted(self):
        pages = [PdfPage(page_no=3, text="residents were consulted at every stage")]
        assert find_quote("residents were consulted at every", pages, 3).found is True
