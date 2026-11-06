"""Normalisation is where most real-world import bugs live, so it is tested hard."""

from __future__ import annotations

import pytest

from slr_studio.importers.normalise import (
    clean_text,
    first_author_key,
    normalise_author,
    normalise_doc_type,
    normalise_doi,
    normalise_language,
    normalise_title,
    parse_year,
    split_keywords,
    title_key,
)


class TestDoi:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("10.1080/jsts.2021.004512", "10.1080/jsts.2021.004512"),
            ("https://doi.org/10.1080/JSTS.2021.004512", "10.1080/jsts.2021.004512"),
            ("http://dx.doi.org/10.1016/j.tgr.2019.02.003", "10.1016/j.tgr.2019.02.003"),
            ("doi:10.1111/csl.12345", "10.1111/csl.12345"),
            ("DOI 10.1111/csl.12345.", "10.1111/csl.12345"),
            ("10.1371/journal.pone.0245512 [doi]", "10.1371/journal.pone.0245512"),
            ("  10.5281/zenodo.2020201  ", "10.5281/zenodo.2020201"),
        ],
    )
    def test_extracts_bare_lowercase_doi(self, raw, expected):
        assert normalise_doi(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "no identifier here", "PMID: 31234567"])
    def test_returns_none_without_a_doi(self, raw):
        assert normalise_doi(raw) is None

    def test_malformed_doi_is_rejected_rather_than_half_read(self):
        # A registrant code is at least four digits; `10.1/abc` is not a DOI,
        # and quietly accepting it would create false matches at dedup.
        assert normalise_doi("10.1/abc") is None

    def test_strips_trailing_punctuation_from_a_sentence(self):
        assert normalise_doi("See 10.1111/csl.12345, which reports") == "10.1111/csl.12345"


class TestTitle:
    def test_case_punctuation_and_accents_are_removed(self):
        assert normalise_title("Résumé: A Study (Part II)!") == "resume a study part ii"

    def test_hyphens_become_spaces_so_databases_agree(self):
        assert normalise_title("community-based tourism") == "community based tourism"

    def test_html_entities_and_markup_are_unwrapped(self):
        assert normalise_title("CO<sub>2</sub> &amp; travel") == "co2 travel"

    def test_wrapped_whitespace_collapses(self):
        assert normalise_title("Evidence  from\n  coastal Kerala") == (
            "evidence from coastal kerala"
        )

    def test_empty_input_gives_empty_string(self):
        assert normalise_title(None) == ""

    def test_blocking_key_drops_stopwords_and_keeps_order(self):
        assert title_key("The role of trust in the planning of tourism") == (
            "role trust planning tourism"
        )

    def test_blocking_key_survives_a_title_of_only_stopwords(self):
        assert title_key("Of the and") != ""


class TestAuthors:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Menon, R.", "menon, r"),
            ("Menon, Rajesh", "menon, r"),
            ("MENON R", "menon, r"),
            ("Rajesh Menon", "menon, r"),
            ("Fernandes, L.P.", "fernandes, l"),
            ("van der Berg, Johan", "vanderberg, j"),
        ],
    )
    def test_names_collapse_to_surname_and_initial(self, raw, expected):
        assert normalise_author(raw) == expected

    def test_first_author_key_is_the_surname_only(self):
        assert first_author_key(["Menon, R.", "Fernandes, L.P."]) == "menon"

    def test_first_author_key_of_no_authors_is_empty(self):
        assert first_author_key([]) == ""


class TestYear:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("2019", 2019),
            ("2019///", 2019),
            ("2019/05/12/", 2019),
            ("2021 Feb 14", 2021),
            ("c2018", 2018),
            ("", None),
            ("no date", None),
            ("volume 29", None),
        ],
    )
    def test_year_is_pulled_from_the_many_date_shapes(self, raw, expected):
        assert parse_year(raw) == expected

    def test_first_value_with_a_year_wins(self):
        assert parse_year("", "n.d.", "2020 Jun") == 2020

    def test_implausible_years_are_rejected(self):
        assert parse_year("3025") is None


class TestOtherFields:
    def test_language_codes_map_to_names(self):
        assert normalise_language("eng") == "English"
        assert normalise_language("ENGLISH") == "English"
        assert normalise_language("fre") == "French"

    def test_unknown_language_is_left_alone(self):
        assert normalise_language("Esperanto") == "Esperanto"

    def test_document_types_map_to_a_common_vocabulary(self):
        assert normalise_doc_type("JOUR") == "Journal Article"
        assert normalise_doc_type("Article in Press") == "Journal Article"
        assert normalise_doc_type("Editorial Material") == "Editorial"

    def test_keywords_split_on_semicolons_and_deduplicate(self):
        assert split_keywords(["Trust; trust; Participation"]) == ["Trust", "Participation"]

    def test_keywords_accept_one_per_line_exports(self):
        assert split_keywords(["Trust", "Participation"]) == ["Trust", "Participation"]

    def test_clean_text_handles_none(self):
        assert clean_text(None) == ""
