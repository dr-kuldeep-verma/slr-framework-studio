"""One test class per export dialect, plus format detection."""

from __future__ import annotations

import pytest

from slr_studio.importers.csv_import import parse_csv, parse_csv_text
from slr_studio.importers.endnote_xml import parse_endnote_xml
from slr_studio.importers.nbib import parse_nbib
from slr_studio.importers.registry import (
    UnknownFormatError,
    detect_format,
    parse_file,
)
from slr_studio.importers.ris import detect_dialect, parse_ris, parse_ris_text


def by_title(result, fragment: str):
    for record in result.records:
        if fragment.casefold() in record.title.casefold():
            return record
    raise AssertionError(f"No record whose title contains {fragment!r}")


class TestScopusRis:
    @pytest.fixture
    def result(self, fixtures):
        return parse_ris(fixtures / "scopus_sample.ris")

    def test_dialect_and_database_are_detected(self, result):
        assert result.dialect == "scopus"
        assert result.source_database == "Scopus"

    def test_every_record_is_read(self, result):
        assert len(result.records) == 6
        assert result.errors == []

    def test_core_fields(self, result):
        record = by_title(result, "coastal Kerala")
        assert record.doi == "10.1080/jsts.2021.004512"
        assert record.year == 2021
        assert record.journal == "Journal of Sustainable Tourism Studies"
        assert record.authors == ["Menon, R.", "Fernandes, L.P."]
        assert record.pages == "512-531"
        assert record.volume == "29"
        assert record.issue == "4"
        assert record.doc_type == "Journal Article"
        assert record.language == "English"

    def test_keywords_arrive_one_per_tag(self, result):
        record = by_title(result, "coastal Kerala")
        assert record.keywords == ["community participation", "coastal tourism", "planning"]

    def test_conference_paper_keeps_its_type_and_proceedings_title(self, result):
        record = by_title(result, "Measuring empowerment")
        assert record.doc_type == "Conference Paper"
        assert "Proceedings" in record.journal

    def test_record_without_an_abstract_is_flagged(self, result):
        record = by_title(result, "Does participation reduce conflict")
        assert record.has_abstract is False
        assert "missing_abstract" in record.flags()

    def test_raw_block_is_kept_verbatim(self, result):
        record = by_title(result, "coastal Kerala")
        assert record.raw.startswith("TY  - JOUR")
        assert record.raw.rstrip().endswith("ER  -")


class TestWebOfScienceRis:
    @pytest.fixture
    def result(self, fixtures):
        return parse_ris(fixtures / "wos_sample.ris")

    def test_dialect_is_detected_from_the_ut_accession(self, result):
        assert result.dialect == "wos"
        assert result.source_database == "Web of Science"

    def test_file_header_before_the_first_record_is_ignored(self, result):
        assert len(result.records) == 6

    def test_di_tag_supplies_the_doi(self, result):
        assert by_title(result, "coastal Kerala").doi == "10.1080/jsts.2021.004512"

    def test_bp_is_read_as_the_start_page(self, result):
        assert by_title(result, "coastal Kerala").pages == "512-531"

    def test_full_journal_title_beats_the_j9_abbreviation(self, result):
        record = by_title(result, "coastal Kerala")
        assert record.journal == "JOURNAL OF SUSTAINABLE TOURISM STUDIES"

    def test_accession_keeps_the_wos_prefix(self, result):
        assert by_title(result, "coastal Kerala").accession == "WOS:000654321000004"

    def test_indented_continuation_lines_are_joined(self, result):
        record = by_title(result, "coastal Kerala")
        assert record.title.endswith("Coastal Kerala")
        assert "eleven panchayats" in record.abstract


class TestEbscoRis:
    @pytest.fixture
    def result(self, fixtures):
        return parse_ris(fixtures / "ebsco_sample.ris")

    def test_content_database_name_is_used(self, result):
        assert result.dialect == "ebsco"
        assert result.source_database == "Hospitality & Tourism Complete"

    def test_accession_number_is_captured(self, result):
        assert by_title(result, "Benefit sharing").accession == "139874562"

    def test_unindented_continuation_lines_are_joined(self, result):
        abstract = by_title(result, "Benefit sharing").abstract
        assert "predicts support for further tourism development" in abstract

    def test_entities_inside_an_abstract_are_unescaped(self, result):
        assert "31% of the variance" in by_title(result, "Benefit sharing").abstract

    def test_record_without_a_doi_is_still_imported(self, result):
        assert by_title(result, "Benefit sharing").doi is None

    def test_keywords_packed_on_one_line_are_split(self, result):
        assert by_title(result, "Benefit sharing").keywords == [
            "BENEFIT sharing",
            "ECOTOURISM",
            "FAIRNESS",
        ]


class TestProQuestRis:
    @pytest.fixture
    def result(self, fixtures):
        return parse_ris(fixtures / "proquest_sample.ris")

    def test_dialect_detected_from_the_url(self, result):
        assert result.dialect == "proquest"

    def test_dissertation_keeps_its_type_and_publisher(self, result):
        record = by_title(result, "Participation ladders")
        assert record.doc_type == "Thesis"
        assert record.publisher == "University of Lagos"

    def test_wrapped_abstract_is_reassembled(self, result):
        abstract = by_title(result, "Deliberative forums").abstract
        assert "resident associations" in abstract


class TestPubMedNbib:
    @pytest.fixture
    def result(self, fixtures):
        return parse_nbib(fixtures / "pubmed_sample.nbib")

    def test_records_are_split_on_blank_lines(self, result):
        assert len(result.records) == 2
        assert result.errors == []

    def test_doi_is_taken_from_the_lid_tag(self, result):
        record = by_title(result, "Community participation and health")
        assert record.doi == "10.1016/j.healthplace.2019.04.011"

    def test_full_author_names_are_preferred_over_initials(self, result):
        record = by_title(result, "Community participation and health")
        assert record.authors == ["Okafor, Chidinma N", "Weber, Hannah"]

    def test_year_is_parsed_from_a_month_bearing_date(self, result):
        assert by_title(result, "Participatory planning").year == 2021

    def test_full_journal_title_is_preferred(self, result):
        assert by_title(result, "Community participation and health").journal == (
            "Health & place"
        )

    def test_pmid_becomes_the_accession(self, result):
        assert by_title(result, "Community participation and health").accession == (
            "31234567"
        )

    def test_language_code_is_expanded(self, result):
        assert by_title(result, "Participatory planning").language == "English"


class TestEndNoteXml:
    @pytest.fixture
    def result(self, fixtures):
        return parse_endnote_xml(fixtures / "endnote_sample.xml")

    def test_both_records_are_read(self, result):
        assert len(result.records) == 2

    def test_style_wrapped_text_is_flattened(self, result):
        record = by_title(result, "Does participation reduce conflict")
        assert record.title.endswith("protected area tourism")
        assert record.authors == ["Silva, J. R.", "Moreira, P."]

    def test_doi_from_electronic_resource_number(self, result):
        assert by_title(result, "Does participation").doi == "10.1111/csl.12345"

    def test_ref_type_name_attribute_becomes_the_document_type(self, result):
        assert by_title(result, "Co-production").doc_type == "Book Chapter"

    def test_book_chapter_takes_its_container_as_the_journal(self, result):
        assert by_title(result, "Co-production").journal == "Handbook of Tourism Governance"

    def test_invalid_xml_is_reported_not_raised(self):
        from slr_studio.importers.endnote_xml import parse_endnote_xml_text

        result = parse_endnote_xml_text("<xml><records><record>")
        assert result.records == []
        assert "Invalid EndNote XML" in result.errors[0].message


class TestCsv:
    @pytest.fixture
    def result(self, fixtures):
        return parse_csv(fixtures / "scopus_sample.csv")

    def test_rows_become_records(self, result):
        assert len(result.records) == 3

    def test_database_is_guessed_from_the_columns(self, result):
        assert result.source_database == "Scopus"

    def test_separate_page_columns_are_joined(self, result):
        assert by_title(result, "coastal Kerala").pages == "512-531"

    def test_semicolon_separated_authors_are_split(self, result):
        assert by_title(result, "coastal Kerala").authors == [
            "Menon R.",
            "Fernandes L.P.",
        ]

    def test_alternative_headings_are_recognised(self):
        text = (
            "Article Title,Publication Year,DOI,Authors,Source Title\n"
            "A study of things,2020,10.1234/x.2020.1,\"Doe, J\",Journal of Things\n"
        )
        result = parse_csv_text(text)
        assert result.records[0].year == 2020
        assert result.records[0].journal == "Journal of Things"

    def test_tab_separated_files_are_handled(self):
        text = "Title\tYear\tDOI\nA tab separated study\t2019\t10.1234/abc.2019\n"
        result = parse_csv_text(text)
        assert len(result.records) == 1
        assert result.records[0].doi == "10.1234/abc.2019"

    def test_file_without_a_title_column_reports_an_error(self):
        result = parse_csv_text("Year,DOI\n2020,10.1/x\n")
        assert result.records == []
        assert "No title column" in result.errors[0].message


class TestFormatDetection:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("scopus_sample.ris", "ris"),
            ("wos_sample.ris", "ris"),
            ("pubmed_sample.nbib", "nbib"),
            ("endnote_sample.xml", "endnote_xml"),
            ("scopus_sample.csv", "csv"),
        ],
    )
    def test_each_fixture_is_identified(self, fixtures, filename, expected):
        assert detect_format(fixtures / filename) == expected

    def test_content_beats_a_misleading_extension(self, tmp_path, fixtures):
        # Web of Science hands out RIS content in a .txt file.
        misnamed = tmp_path / "savedrecs.txt"
        misnamed.write_text((fixtures / "wos_sample.ris").read_text(), encoding="utf-8")
        assert detect_format(misnamed) == "ris"
        assert len(parse_file(misnamed).records) == 6

    def test_unrecognisable_file_raises_a_useful_error(self, tmp_path):
        path = tmp_path / "notes.doc"
        path.write_text("just some prose about tourism", encoding="utf-8")
        with pytest.raises(UnknownFormatError, match="not a format this app recognises"):
            detect_format(path)


class TestRisRobustness:
    def test_record_without_an_er_terminator_is_still_read(self):
        result = parse_ris_text("TY  - JOUR\nTI  - A truncated download\nPY  - 2020\n")
        assert len(result.records) == 1
        assert result.records[0].title == "A truncated download"

    def test_record_with_neither_title_nor_doi_is_an_error_not_a_crash(self):
        result = parse_ris_text("TY  - JOUR\nPY  - 2020\nER  - \n")
        assert result.records == []
        assert "neither a title nor a DOI" in result.errors[0].message

    def test_single_space_tag_separator_is_accepted(self):
        result = parse_ris_text("TY - JOUR\nTI - Loosely formatted\nER -\n")
        assert result.records[0].title == "Loosely formatted"

    def test_prose_lines_are_not_mistaken_for_tags(self):
        result = parse_ris_text(
            "TY  - JOUR\nTI  - A study\nAB  - In vitro findings\nIn - vivo too\nER  - \n"
        )
        assert "In - vivo too" in result.records[0].abstract

    def test_doi_is_recovered_from_a_url_when_the_do_tag_is_missing(self):
        result = parse_ris_text(
            "TY  - JOUR\nTI  - A study\nUR  - https://doi.org/10.1234/abc.2020\nER  - \n"
        )
        assert result.records[0].doi == "10.1234/abc.2020"

    def test_windows_line_endings_are_handled(self):
        result = parse_ris_text("TY  - JOUR\r\nTI  - CRLF study\r\nER  - \r\n")
        assert result.records[0].title == "CRLF study"

    def test_dialect_falls_back_to_generic(self):
        dialect, database = detect_dialect("TY  - JOUR\nTI  - Nothing identifying\nER  -")
        assert dialect == "generic"
        assert database == "Unknown"

    def test_latin1_encoded_file_is_read(self, tmp_path):
        path = tmp_path / "latin.ris"
        path.write_bytes("TY  - JOUR\nTI  - Café tourism\nER  - \n".encode("cp1252"))
        assert parse_ris(path).records[0].title == "Café tourism"
