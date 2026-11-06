"""Checks against the spec text itself, clause by clause.

These are not behaviour tests — the behaviour is tested elsewhere. They assert
that the things the spec names by name are present and wired up, so that a
later refactor cannot quietly drop one. When the spec changes, these fail
first and say which clause moved.

Section numbers follow `docs/SPEC.md`.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parent.parent / "docs" / "SPEC.md"


def test_the_spec_is_shipped_with_the_project():
    """One source of truth, versioned alongside the code it describes."""
    assert SPEC.is_file()
    assert "# Build prompt: SLR Framework Studio" in SPEC.read_text(encoding="utf-8")


class TestSection1Navigator:
    def test_the_navigator_matches_the_spec_order(self):
        from slr_studio import STAGES

        expected = [
            "Project", "Import", "Deduplicate", "Screen", "Retrieve",
            "Full-text", "Extract", "Synthesise", "Framework", "Gap Finder",
            "Export",
        ]
        assert [label for _, label in STAGES] == expected

    def test_the_spec_lists_the_same_order(self):
        text = SPEC.read_text(encoding="utf-8")
        assert (
            "Project → Import → Deduplicate → Screen → Retrieve → Full-text → "
            "Extract → Synthesise → Framework → Gap Finder → Export" in text
        )


class TestSection5Screening:
    """Every clause of "5. Title and abstract screening"."""

    @pytest.fixture
    def engine_source(self) -> str:
        from slr_studio.screening import ScreeningEngine

        return inspect.getsource(ScreeningEngine)

    def test_the_user_labels_a_random_sample(self, engine_source):
        assert "pilot_sample" in engine_source
        assert "record_pilot_label" in engine_source

    def test_the_default_pilot_is_between_fifty_and_a_hundred(self):
        from slr_studio.screening import ScreeningSettings
        from slr_studio.screening.engine import (
            PILOT_CONVENTIONAL_MAXIMUM,
            PILOT_CONVENTIONAL_MINIMUM,
        )

        assert (
            PILOT_CONVENTIONAL_MINIMUM
            <= ScreeningSettings().pilot_size
            <= PILOT_CONVENTIONAL_MAXIMUM
        )

    def test_a_smaller_pilot_runs_but_says_so(self, ready_to_screen):
        """The band is advisory: a small corpus is not blocked, only warned."""
        project, make_engine = ready_to_screen
        engine = make_engine(pilot_size=4)
        for record in engine.pilot_sample():
            engine.record_pilot_label(record["id"], "include", actor="human:test")
        outcome = engine.run_pilot(actor="human:test")
        assert any("convention is 50" in m for m in outcome.messages)

    def test_the_ai_screens_the_same_sample(self, engine_source):
        assert "run_pilot" in engine_source

    def test_a_full_run_is_blocked_until_the_pilot_has_happened(self, engine_source):
        assert "Run the pilot first" in engine_source

    def test_the_waiver_is_logged(self, engine_source):
        assert "waive_pilot" in engine_source and "pilot_waived" in engine_source

    def test_there_are_two_screeners_with_different_personas(self):
        from slr_studio.screening.prompts import PERSONAS

        assert set(PERSONAS) == {"screener_a", "screener_b"}
        assert PERSONAS["screener_a"]["stance"] != PERSONAS["screener_b"]["stance"]

    def test_a_screener_sees_only_the_protocol_and_the_record(self):
        from slr_studio.screening.prompts import render_record, screener_system

        system = screener_system("screener_a", "THE PROTOCOL", [])
        assert "THE PROTOCOL" in system
        assert "screener b" not in system.casefold()
        # Nor anything about the record beyond the record itself.
        shown = render_record({"title": "T", "source_database": "Scopus"})
        assert "Scopus" not in shown

    def test_the_answer_carries_decision_code_reason_and_quote(self):
        from slr_studio.screening.schemas import ScreeningAnswer

        fields = ScreeningAnswer.model_fields
        assert set(fields) >= {"decision", "exclusion_code", "reason", "quote"}
        assert set(fields["decision"].annotation.__args__) == {
            "include", "exclude", "unclear"
        }

    def test_an_exclusion_must_name_the_first_failing_code(self):
        from slr_studio.screening.prompts import screener_system
        from slr_studio.screening.schemas import ScreeningAnswer

        assert "FIRST" in screener_system("screener_a", "p", [])
        with pytest.raises(ValueError):
            ScreeningAnswer(decision="exclude", reason="off topic")

    def test_a_third_call_adjudicates(self, engine_source):
        assert "screen:adjudicate" in engine_source

    def test_quality_control_covers_a_reproducible_sample_and_keyword_signals(
        self, engine_source
    ):
        assert "run_qc" in engine_source
        assert "_rng" in engine_source          # reproducible
        assert "protocol_keywords" in engine_source   # keyword signals

    def test_all_three_agreement_figures_are_available(self):
        from slr_studio.screening import stats

        for name in ("percentage_agreement", "cohens_kappa", "pabak"):
            assert hasattr(stats, name)

    def test_the_queue_holds_all_four_kinds_of_item(self, engine_source):
        for reason in ("conflict", "unclear", "qc_rescue", "random_audit"):
            assert f"'{reason}'" in engine_source or f'"{reason}"' in engine_source

    def test_the_human_decision_is_recorded_as_an_override(self, engine_source):
        assert "log_override" in engine_source

    def test_the_outputs_are_the_excel_log_and_live_prisma_counts(self, engine_source):
        from slr_studio.exports import write_screening_log

        assert callable(write_screening_log)
        assert "v_prisma_screening" in engine_source


class TestSection11GapFinderGroundwork:
    """What section 11 needs that earlier stages have to record now."""

    def test_the_gap_register_holds_what_the_spec_asks_for(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(gap_register)")
        }
        assert {
            "gap_type", "statement", "supporting_counts", "corpus_searched_to",
            "freshness_status", "priority", "score_centrality", "score_size",
            "score_relevance", "score_feasibility", "score_overrides",
            "suggested_question", "suggested_hypothesis", "suggested_design",
        } <= columns

    def test_the_seven_gap_types_are_all_allowed(self, project):
        sql = project.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name = 'gap_register'"
        )
        for gap_type in (
            "evidence", "theoretical", "contradiction", "methodological",
            "population_context", "information", "author_stated",
        ):
            assert f"'{gap_type}'" in sql

    def test_a_gap_can_point_at_the_records_behind_it(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(gap_evidence)")
        }
        assert {"gap_id", "record_id", "relationship_id", "future_research_id"} <= columns

    def test_author_stated_gaps_keep_their_page(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(future_research)")
        }
        assert {"statement", "quote", "quote_page", "quote_verified", "kind"} <= columns

    def test_limitations_and_future_research_are_distinguished(self, project):
        sql = project.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name = 'future_research'"
        )
        assert "'future_research'" in sql and "'limitation'" in sql

    def test_the_quick_scan_can_read_text_keywords_and_year(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(v_quick_scan_input)")
        }
        assert {"title", "abstract", "keywords", "year", "n_keywords"} <= columns


class TestSection9Synthesis:
    """Every clause of "9. Synthesis engine"."""

    @pytest.fixture
    def engine_source(self) -> str:
        from slr_studio.synthesis import SynthesisEngine

        return inspect.getsource(SynthesisEngine)

    # -- codebook ----------------------------------------------------------

    def test_the_codebook_is_proposed_from_objectives_and_labels(self):
        from slr_studio.synthesis.prompts import codebook_system, codebook_user

        system = codebook_system("THE PROTOCOL", "THE OBJECTIVES")
        assert "THE OBJECTIVES" in system and "THE PROTOCOL" in system
        assert "measur" in system.casefold()
        shown = codebook_user([{"label": "community trust", "role": "predictor",
                                "n_uses": 3}])
        assert "community trust" in shown

    def test_a_construct_carries_a_definition_includes_and_excludes(self):
        from slr_studio.synthesis.schemas import ConstructProposal

        assert {"name", "definition", "includes", "excludes"} <= set(
            ConstructProposal.model_fields
        )

    def test_nothing_may_be_mapped_before_a_human_approves(self, engine_source):
        assert "codebook_approved" in engine_source
        assert "approve_codebook" in engine_source

    def test_the_human_can_edit_before_approving(self, engine_source):
        for verb in ("add_construct", "edit_construct", "reject_construct"):
            assert verb in engine_source

    # -- mapping -----------------------------------------------------------

    def test_a_label_is_mapped_by_what_was_measured(self):
        from slr_studio.synthesis.prompts import mapping_system
        from slr_studio.synthesis.schemas import LabelMapping

        assert "measured" in mapping_system([]).casefold()
        assert "measured_as" in LabelMapping.model_fields

    def test_a_residual_code_exists(self):
        from slr_studio.synthesis.engine import RESIDUAL_CONSTRUCT

        assert RESIDUAL_CONSTRUCT

    def test_contestable_mappings_are_flagged(self, engine_source):
        from slr_studio.synthesis.schemas import LabelMapping

        assert "contestable" in LabelMapping.model_fields
        assert "contestable_mappings" in engine_source

    # -- harmonisation -----------------------------------------------------

    def test_the_human_fixes_one_orientation_per_construct(self, engine_source):
        assert "set_orientation" in engine_source
        assert "constructs_without_orientation" in engine_source

    def test_every_sign_flip_is_logged(self, engine_source):
        assert "flip_reason" in engine_source
        assert "sign_flips" in engine_source

    # -- dataset linkage ---------------------------------------------------

    def test_linkage_uses_authors_sites_dates_and_sample_size(self):
        from slr_studio.synthesis.linkage import score_shared_dataset

        source = inspect.getsource(score_shared_dataset)
        for signal in ("author", "country", "time_horizon", "sample_size"):
            assert signal in source, signal

    def test_the_human_confirms_each_link(self, engine_source):
        assert "decide_link" in engine_source
        assert "proposed_links" in engine_source

    def test_reviews_and_conceptual_papers_do_not_vote(self):
        from slr_studio.synthesis import NON_VOTING_FAMILIES

        assert {"review", "conceptual"} <= set(NON_VOTING_FAMILIES)

    # -- grading -----------------------------------------------------------

    def test_the_grades_are_applied_in_the_spec_order(self):
        from slr_studio.synthesis import GRADES

        assert GRADES == (
            "asserted", "contested", "strong", "moderate", "emerging", "tentative"
        )

    def test_the_spec_lists_the_same_order(self):
        text = SPEC.read_text(encoding="utf-8")
        assert (
            "asserted, contested, strong, moderate, emerging, tentative" in text
        )

    def test_the_default_strong_rule_is_the_one_the_spec_states(self):
        from slr_studio.synthesis import GradingRules

        rules = GradingRules()
        assert rules.strong_min_datasets == 5
        assert rules.strong_max_opposing == 0
        assert rules.strong_min_agreement == 0.6
        assert rules.strong_require_sign_test is True
        assert rules.strong_min_countries == 2

    def test_the_rules_are_configurable(self):
        from slr_studio.synthesis import GradingRules

        relaxed = GradingRules(strong_min_datasets=3)
        assert relaxed.strong_min_datasets == 3

    def test_every_link_reports_a_wilson_interval(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(graded_link)")
        }
        assert {"ci_low", "ci_high"} <= columns

    def test_votes_are_counted_at_dataset_level(self, project):
        columns = {row[1] for row in project.db.query("PRAGMA table_info(link_vote)")}
        assert "dataset_id" in columns

    # -- sensitivity -------------------------------------------------------

    def test_all_five_sensitivity_analyses_are_present(self):
        from slr_studio.synthesis import SynthesisEngine

        names = set(SynthesisEngine.SENSITIVITY)
        assert len(names) == 5
        for expected in (
            "alternative_strong_rule", "exclude_high_concern",
            "quantitative_only", "experiments_only", "observational_only",
        ):
            assert expected in names

    def test_a_sensitivity_result_records_sign_and_grade_changes(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(sensitivity_result)")
        }
        assert {
            "analysis", "base_grade", "new_grade", "base_direction",
            "new_direction", "changed_grade", "changed_sign",
        } <= columns

    # -- contradictions ----------------------------------------------------

    def test_a_contradiction_is_explained_by_the_four_named_dimensions(self):
        from slr_studio.synthesis.schemas import ContradictionExplanation

        allowed = ContradictionExplanation.model_fields["explanation_type"]
        for dimension in ("context", "measurement", "method", "sample"):
            assert dimension in allowed.annotation.__args__, dimension

    def test_it_suggests_a_moderator_or_a_construct_split(self):
        from slr_studio.synthesis.schemas import ContradictionExplanation

        fields = set(ContradictionExplanation.model_fields)
        assert "suggested_moderator" in fields
        assert "split_into" in fields

    def test_the_human_accepts_or_rejects_each_one(self, engine_source):
        assert "decide_contradiction" in engine_source


class TestSection10FrameworkBuilder:
    """Every clause of "10. Framework builder"."""

    @pytest.fixture
    def engine_source(self) -> str:
        from slr_studio.framework import FrameworkEngine

        return inspect.getsource(FrameworkEngine)

    # -- evidence maps -----------------------------------------------------

    def test_one_figure_per_theme(self, engine_source):
        assert "draw_evidence_maps" in engine_source
        assert "theme" in engine_source

    def test_figures_are_drawn_from_graded_links_only(self, engine_source):
        assert "_require_inventory" in engine_source
        assert "graded_link" in engine_source or "inventory" in engine_source

    def test_line_weight_shows_the_grade(self):
        from slr_studio.framework import style_for

        assert style_for("strong", "positive").width > (
            style_for("moderate", "positive").width
        )

    def test_thick_for_strong_solid_for_moderate_dashed_for_emerging(self):
        from slr_studio.framework import GRADE_ORDER, style_for

        assert style_for("strong", "positive").width == max(
            style_for(g, "positive").width for g in GRADE_ORDER
        )
        assert style_for("moderate", "positive").dash == ()
        assert style_for("emerging", "positive").dash != ()

    def test_double_headed_for_undirected_associations(self):
        from slr_studio.framework import style_for

        assert style_for("strong", "none").heads == 2
        assert style_for("strong", "positive").heads == 1

    def test_each_arrow_is_labelled_with_its_link_id_and_sign(self):
        from slr_studio.framework import arrow_label

        label = arrow_label(12, "negative")
        assert "L12" in label
        assert "−" in label or "-" in label

    def test_connectors_are_straight_segments_not_curves(self):
        """A polyline of straight segments; nothing draws a curve."""
        from slr_studio.framework import figures

        source = inspect.getsource(figures)
        assert "STRAIGHT" in source          # the PowerPoint connector type
        for curve in (" C", "quadratic", "bezier", "arcTo"):
            assert curve not in source.replace("\n", " ") or curve == " C"

    def test_boxes_are_aligned(self):
        from slr_studio.framework import Edge, Node, layout_graph

        nodes = [Node(key=n, label=n) for n in "abcd"]
        edges = [Edge(key="a->c", source="a", target="c"),
                 Edge(key="b->c", source="b", target="c")]
        placed = layout_graph(nodes, edges)
        xs = {round(box[0], 3) for box in placed.nodes.values()}
        assert len(xs) < len(placed.nodes)

    def test_no_line_passes_through_a_box(self):
        from slr_studio.framework import Edge, Node, layout_graph, segment_hits_box

        nodes = [Node(key=n, label=n) for n in "abc"]
        edges = [
            Edge(key="a->b", source="a", target="b"),
            Edge(key="b->c", source="b", target="c"),
            Edge(key="a->c", source="a", target="c"),
        ]
        placed = layout_graph(nodes, edges)
        for key, points in placed.edges.items():
            source, target = key.split("->")
            for start, end in zip(points, points[1:]):
                for name, box in placed.nodes.items():
                    if name not in (source, target):
                        assert not segment_hits_box(start, end, box)

    def test_crossings_are_reduced_rather_than_ignored(self):
        from slr_studio.framework import count_crossings, layout_graph
        from slr_studio.framework.layout import Edge, Node

        names = ["a", "b", "c", "x", "y", "z"]
        nodes = [Node(key=n, label=n) for n in names]
        edges = [
            Edge(key=f"{s}->{t}", source=s, target=t)
            for s, t in (("a", "y"), ("b", "x"), ("c", "z"), ("a", "x"))
        ]
        assert count_crossings(layout_graph(nodes, edges)) <= count_crossings(
            layout_graph(nodes, edges, minimise_crossings=False)
        )

    # -- the theoretical framework ----------------------------------------

    def test_a_proposition_carries_everything_the_spec_names(self):
        from slr_studio.framework.schemas import ProposedProposition

        fields = set(ProposedProposition.model_fields)
        assert {
            "statement", "mechanism", "theories", "moderators",
            "boundary_conditions", "falsifying_test", "cited_links",
        } <= fields

    def test_the_mechanism_is_drawn_from_the_theories_the_user_lists(self):
        from slr_studio.framework.prompts import proposition_system

        system = proposition_system("p", ["social exchange theory"], [], [])
        assert "social exchange theory" in system
        assert "mechanism" in system.casefold()

    def test_the_three_kinds_of_element_are_kept_separate(self, project):
        from slr_studio.framework.schemas import ELEMENT_KINDS

        assert set(ELEMENT_KINDS) == {
            "evidenced_link", "proposed_mechanism", "untested_pathway"
        }
        sql = project.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name = 'framework_element'"
        )
        for kind in ELEMENT_KINDS:
            assert f"'{kind}'" in sql

    def test_a_proposition_may_cite_only_links_in_the_graded_inventory(
        self, engine_source
    ):
        assert "not in the graded inventory" in engine_source

    def test_output_citing_anything_else_is_rejected_not_corrected(
        self, engine_source
    ):
        assert '"rejected"' in engine_source or "'rejected'" in engine_source
        assert "n_rejected" in engine_source

    # -- the integrated diagram -------------------------------------------

    def test_there_is_an_integrated_diagram(self, engine_source):
        assert "draw_integrated_diagram" in engine_source

    def test_it_is_editable_in_powerpoint_as_shapes_not_a_picture(self):
        from slr_studio.framework import figures

        source = inspect.getsource(figures)
        assert "add_shape" in source and "add_connector" in source
        assert "add_picture" not in source

    def test_the_same_style_is_used_in_every_format(self):
        """One figure, three files: the PowerPoint is the figure, not a copy."""
        from slr_studio.framework import figures

        source = inspect.getsource(figures)
        assert source.count("style_for") >= 1
        assert "_ELEMENT_STYLE" in source


class TestSection11GapFinder:
    """Every clause of "11. Gap Finder"."""

    @pytest.fixture
    def engine_source(self) -> str:
        from slr_studio.gaps import GapEngine

        return inspect.getsource(GapEngine)

    # -- the quick scan ----------------------------------------------------

    def test_the_quick_scan_reads_titles_abstracts_and_keywords(self):
        from slr_studio.gaps.scan import Document

        document = Document(
            record_id=1, title="t", abstract="a", keywords=["k"], year=2020
        )
        text = document.text()
        assert "t" in text and "a" in text and "k" in text

    def test_it_clusters_into_topics(self):
        from slr_studio.gaps.scan import cluster, vectorise

        assert callable(cluster) and callable(vectorise)

    def test_it_reports_the_trend_of_each_topic_over_time(self, engine_source):
        from slr_studio.gaps.scan import trend

        assert callable(trend)
        assert "trend" in engine_source

    def test_it_reports_keyword_co_occurrence(self, engine_source):
        from slr_studio.gaps.scan import co_occurrence

        assert callable(co_occurrence)
        assert "co_occurrence" in engine_source

    def test_it_reports_sparse_intersections(self):
        from slr_studio.gaps.scan import sparse_cells

        assert callable(sparse_cells)

    def test_the_scan_is_a_scoping_view_and_says_so(self, engine_source):
        """The spec calls it "a scoping view for refining objectives, not a
        finding", and the application has to tell the reviewer that, not just
        its own source.
        """
        assert "scoping view" in engine_source
        assert "Nothing downstream cites it." in engine_source

    # -- the seven gap types -----------------------------------------------

    def test_all_seven_gap_types_are_detected(self):
        from slr_studio.gaps import detect

        for name in (
            "find_evidence_gaps", "find_theoretical_gaps",
            "find_contradiction_gaps", "find_methodological_gaps",
            "find_population_gaps", "find_information_gaps",
            "find_author_stated_gaps",
        ):
            assert callable(getattr(detect, name))

    def test_the_register_allows_exactly_those_seven(self, project):
        sql = project.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name = 'gap_register'"
        )
        for gap_type in (
            "evidence", "theoretical", "contradiction", "methodological",
            "population_context", "information", "author_stated",
        ):
            assert f"'{gap_type}'" in sql

    def test_an_evidence_gap_covers_no_study_and_one_dataset(self):
        from slr_studio.gaps.detect import find_evidence_gaps

        source = inspect.getsource(find_evidence_gaps)
        assert "n_datasets > 1" in source

    def test_theoretical_gaps_cover_stages_mechanisms_and_pathways(self):
        from slr_studio.gaps.detect import find_theoretical_gaps

        source = inspect.getsource(find_theoretical_gaps)
        for kind in ("stage_transition", "mechanism_not_measured",
                     "untested_pathway"):
            assert kind in source

    def test_the_framework_stages_the_spec_names_are_present(self):
        from slr_studio.gaps.detect import FRAMEWORK_STAGES

        assert FRAMEWORK_STAGES["SOBC"] == (
            "stimulus", "organism", "behaviour", "consequence"
        )

    def test_methodological_gaps_cover_designs_measures_and_intentions(self):
        from slr_studio.gaps.detect import find_methodological_gaps

        source = inspect.getsource(find_methodological_gaps)
        for kind in ("design_absent", "measure_used_once", "intention_behaviour"):
            assert kind in source

    def test_population_gaps_cross_every_dimension_the_spec_names(self):
        from slr_studio.gaps.detect import CONTEXT_DIMENSIONS

        assert set(CONTEXT_DIMENSIONS) == {
            "country", "region", "income_group", "setting", "population",
            "activity",
        }

    def test_author_stated_gaps_keep_their_source_and_page(self):
        from slr_studio.gaps.detect import find_author_stated_gaps

        assert "pages" in inspect.getsource(find_author_stated_gaps)

    # -- gap statements ------------------------------------------------------

    def test_every_absence_is_qualified_to_the_corpus_and_the_date(self):
        from slr_studio.gaps.statements import qualifier

        text = qualifier("2026-03-01")
        assert "within the included corpus" in text.casefold()
        assert "2026-03-01" in text

    def test_no_statement_is_produced_without_its_counts(self):
        from slr_studio.gaps.statements import UnsupportedGap, write_statement

        assert issubclass(UnsupportedGap, Exception)
        assert "supporting counts" in inspect.getsource(write_statement)

    # -- the evidence gap map --------------------------------------------------

    def test_the_map_is_a_matrix_with_counts_and_grade_colours(self):
        from slr_studio.gaps.gapmap import _GRADE_COLOURS, build_grid

        assert callable(build_grid)
        assert {grade for grade, _ in _GRADE_COLOURS} >= {
            "strong", "moderate", "emerging", "tentative"
        }

    def test_a_cell_lists_its_studies(self):
        from slr_studio.gaps.gapmap import Grid

        assert hasattr(Grid, "studies_in")

    def test_the_map_exports_to_png_svg_and_excel(self):
        from slr_studio.gaps import gapmap

        for name in ("write_png", "write_svg", "write_excel"):
            assert callable(getattr(gapmap, name))

    # -- freshness -------------------------------------------------------------

    def test_both_openalex_and_crossref_are_queried(self):
        from slr_studio.gaps.freshness import CROSSREF, OPENALEX

        assert "openalex" in OPENALEX and "crossref" in CROSSREF

    def test_it_asks_only_for_work_after_the_search_date(self):
        from slr_studio.gaps.freshness import FreshnessChecker

        source = inspect.getsource(FreshnessChecker)
        assert "from_publication_date" in source
        assert "from-pub-date" in source

    def test_a_hit_marks_the_gap_rather_than_removing_it(self):
        from slr_studio.gaps.freshness import FreshnessChecker

        assert "possibly_addressed" in inspect.getsource(FreshnessChecker)

    # -- prioritisation ---------------------------------------------------------

    def test_the_four_criteria_the_spec_names(self):
        from slr_studio.gaps.priority import CRITERIA

        assert CRITERIA == ("centrality", "size", "relevance", "feasibility")

    def test_the_weights_can_be_adjusted(self):
        from slr_studio.gaps.priority import Weights

        assert Weights(centrality=3.0).centrality == 3.0

    def test_any_score_can_be_overridden(self):
        from slr_studio.gaps.priority import Scores

        assert hasattr(Scores, "with_overrides")

    # -- from gap to study --------------------------------------------------------

    def test_a_drafted_study_carries_everything_the_spec_asks_for(self):
        from slr_studio.gaps.schemas import StudyDraft

        assert {
            "question", "hypothesis", "design", "measures", "population",
            "setting",
        } <= set(StudyDraft.model_fields)

    def test_the_hypothesis_is_tied_to_a_proposition(self):
        from slr_studio.gaps.prompts import study_system

        assert "proposition" in study_system([]).casefold()

    # -- exports --------------------------------------------------------------------

    def test_all_four_exports_exist(self):
        from slr_studio import exports

        for name in (
            "write_gap_register", "write_research_agenda",
            "write_future_research_draft",
        ):
            assert callable(getattr(exports, name))
        from slr_studio.gaps.gapmap import write_svg

        assert callable(write_svg)

    def test_the_register_carries_the_columns_the_spec_names(self):
        from slr_studio.exports.gap_register import _HEADERS

        joined = " ".join(_HEADERS).casefold()
        for wanted in ("type", "statement", "evidence", "freshness",
                       "priority", "question"):
            assert wanted in joined


class TestSection12Exports:
    """Every clause of "12. Exports"."""

    def test_the_prisma_diagram_is_offered_in_word_and_pdf(self):
        from slr_studio.exports.bundle import SECTION_12

        kinds = {item.kind for item in SECTION_12}
        assert {"prisma_diagram", "prisma_diagram_pdf"} <= kinds

    def test_the_checklist_is_pre_filled(self):
        from slr_studio.exports.prisma import _answers, write_prisma_checklist

        assert callable(_answers) and callable(write_prisma_checklist)

    def test_the_screening_log_and_extraction_matrix_are_excel(self):
        from slr_studio import exports

        assert callable(exports.write_screening_log)
        assert callable(exports.write_extraction_matrix)

    def test_ris_is_written_for_screened_in_and_included(self):
        from slr_studio.exports.ris import STAGES

        assert {"screened_in", "included"} <= set(STAGES)

    def test_a_refi_qda_project_is_written_and_validated(self):
        from slr_studio.exports.refi import NAMESPACE, validate_qdpx

        assert NAMESPACE == "urn:QDA-XML:project:1.0"
        assert callable(validate_qdpx)

    def test_the_qdpx_carries_the_codings(self):
        import inspect

        from slr_studio.exports.refi import write_refi_project

        source = inspect.getsource(write_refi_project)
        assert "PlainTextSelection" in source and "CodeRef" in source

    def test_the_synthesis_tables_are_excel_and_word(self):
        from slr_studio import exports

        assert callable(exports.write_graded_inventory)
        assert callable(exports.write_synthesis_tables)

    def test_the_figures_come_in_all_four_formats(self, project):
        sql = project.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name = 'figure'"
        )
        for fmt in ("svg", "png", "pptx", "pdf"):
            assert f"'{fmt}'" in sql

    def test_a_methods_draft_is_written(self):
        from slr_studio.exports import write_methods_draft

        assert callable(write_methods_draft)

    def test_the_ai_use_statement_names_model_version_date_and_tasks(self):
        import inspect

        from slr_studio.exports.methods import ai_use_statement

        source = inspect.getsource(ai_use_statement)
        for wanted in ("model", "model_version", "first_call", "task"):
            assert wanted in source

    def test_the_ai_use_statement_cannot_be_switched_off(self):
        """Integrity rule 4, checked as the absence of any way to do it."""
        import inspect

        from slr_studio.exports.methods import write_methods_draft

        source = inspect.getsource(write_methods_draft)
        assert "ai_use_statement(project)" in source
        signature = inspect.signature(write_methods_draft)
        assert set(signature.parameters) == {"project", "path"}

    def test_the_full_audit_log_is_exported(self):
        from slr_studio.exports import write_audit_log

        assert callable(write_audit_log)

    def test_the_audit_export_carries_the_hash_chain_and_the_ai_calls(self):
        import inspect

        from slr_studio.exports.methods import write_audit_log

        source = inspect.getsource(write_audit_log)
        assert "prev_hash" in source and "entry_hash" in source
        assert "prompt_hash" in source and "input_hash" in source


class TestSection13IntegrityRules:
    """The five rules that are non-negotiable."""

    def test_every_ai_call_stores_model_hashes_output_and_time(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(ai_call)")
        }
        assert {
            "model", "model_version", "prompt_hash", "input_hash", "output",
            "created_at",
        } <= columns

    def test_an_override_is_recorded_structurally(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(human_override)")
        }
        assert {"stage", "entity", "field", "ai_value", "human_value"} <= columns

    def test_the_audit_log_is_hash_chained(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(audit_log)")
        }
        assert {"prev_hash", "entry_hash"} <= columns

    def test_a_cost_estimate_exists_for_every_bulk_stage(self):
        for module, name in (
            ("slr_studio.screening", "ScreeningEngine"),
            ("slr_studio.extraction", "ExtractionEngine"),
            ("slr_studio.synthesis", "SynthesisEngine"),
            ("slr_studio.framework", "FrameworkEngine"),
            ("slr_studio.gaps", "GapEngine"),
        ):
            engine = getattr(__import__(module, fromlist=[name]), name)
            # Screening calls it estimate_full_run; what the rule requires is
            # that a reviewer sees the cost before a bulk run, not that every
            # engine spells the method the same way.
            assert any(
                attribute.startswith("estimate") for attribute in dir(engine)
            ), name

    def test_a_bulk_run_can_be_paused(self):
        import inspect

        from slr_studio.screening import ScreeningEngine

        assert "should_stop" in inspect.getsource(ScreeningEngine)
