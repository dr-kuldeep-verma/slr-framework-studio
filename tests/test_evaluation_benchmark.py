"""The benchmark harness, end to end against the mock model.

No test here reaches the network or a real provider. What is being checked
is that the harness drives the real screening engine, lines the decisions
up with the gold labels correctly, and reports what it cannot do -- not
that any particular model scores well, which is not a property of this
code.
"""

from __future__ import annotations

import csv

import pytest

from slr_studio.ai import AIClient, MockProvider
from slr_studio.evaluation.benchmark import BenchmarkError, run_benchmark
from slr_studio.evaluation.datasets import (
    DatasetError,
    load_csv,
    stratified_sample,
)

PROTOCOL = (
    "Studies of resident participation in sustainable tourism planning. "
    "Include empirical studies of residents in tourism destinations that "
    "report participation in planning or decision-making. Exclude studies "
    "of tourists, and studies with no empirical data."
)


def _write_csv(path, rows, *, columns=None):
    columns = columns or [
        "record_id", "title", "abstract", "year", "doi", "keywords",
        "label_abstract_screening", "label_included",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})
    return path


@pytest.fixture
def tiny_dataset(tmp_path):
    """Six records: three the humans kept, three they did not."""
    rows = [
        {"record_id": 1, "title": "Resident participation in tourism planning in Kerala",
         "abstract": "A survey of residents and their participation in planning.",
         "year": 2021, "label_abstract_screening": 1, "label_included": 1},
        {"record_id": 2, "title": "Community involvement in sustainable tourism decisions",
         "abstract": "Empirical study of resident participation.",
         "year": 2020, "label_abstract_screening": 1, "label_included": 0},
        {"record_id": 3, "title": "Residents and tourism planning: a case study",
         "abstract": "", "year": 2019, "label_abstract_screening": 1,
         "label_included": 1},
        {"record_id": 4, "title": "Soil chemistry of upland pasture",
         "abstract": "Nitrogen levels across three sites.",
         "year": 2018, "label_abstract_screening": 0, "label_included": 0},
        {"record_id": 5, "title": "A review of turbine blade fatigue",
         "abstract": "Metallurgical analysis.", "year": 2017,
         "label_abstract_screening": 0, "label_included": 0},
        {"record_id": 6, "title": "Tourist satisfaction with hotel breakfasts",
         "abstract": "Survey of visitors, not residents.", "year": 2022,
         "label_abstract_screening": 0, "label_included": 0},
    ]
    path = _write_csv(tmp_path / "tiny.csv", rows)
    return load_csv(path, name="tiny", licence="CC-BY 4.0")


class TestTheLoader:
    def test_it_prefers_the_abstract_screening_label(self, tiny_dataset):
        """The fairer standard for a title/abstract screener.

        In this fixture the two labels differ, as they do in the real
        datasets: scoring against the post-full-text label would mark a
        correct abstract-stage decision as a false positive.
        """
        assert tiny_dataset.label_column == "label_abstract_screening"
        assert tiny_dataset.n_relevant == 3

    def test_the_post_full_text_label_can_be_asked_for_and_is_flagged(self, tmp_path):
        rows = [
            {"record_id": 1, "title": "A", "label_abstract_screening": 1,
             "label_included": 0},
            {"record_id": 2, "title": "B", "label_abstract_screening": 0,
             "label_included": 0},
        ]
        path = _write_csv(tmp_path / "x.csv", rows)
        dataset = load_csv(path, label_column="label_included")
        assert dataset.n_relevant == 0
        assert any("post-full-text" in n for n in dataset.notes)

    def test_it_counts_the_records_with_no_abstract(self, tiny_dataset):
        """A third of a real dataset is title-only; that bounds any tool."""
        assert tiny_dataset.n_without_abstract == 1
        assert any("no abstract" in n for n in tiny_dataset.notes)

    def test_an_unlabelled_row_is_dropped_not_called_irrelevant(self, tmp_path):
        """Treating 'unknown' as 'not relevant' would inflate specificity."""
        rows = [
            {"record_id": 1, "title": "A", "label_abstract_screening": 1},
            {"record_id": 2, "title": "B", "label_abstract_screening": ""},
            {"record_id": 3, "title": "C", "label_abstract_screening": 0},
        ]
        path = _write_csv(tmp_path / "x.csv", rows)
        dataset = load_csv(path)
        assert len(dataset.records) == 2
        assert any("no usable label" in n for n in dataset.notes)

    def test_a_file_with_no_labels_is_refused(self, tmp_path):
        path = _write_csv(
            tmp_path / "x.csv", [{"title": "A"}], columns=["title", "abstract"]
        )
        with pytest.raises(DatasetError, match="no label column"):
            load_csv(path)

    def test_characteristics_travel_with_the_data(self, tiny_dataset):
        facts = tiny_dataset.characteristics()
        assert facts["n_relevant"] == 3
        assert facts["label_column"] == "label_abstract_screening"
        assert facts["licence"] == "CC-BY 4.0"


class TestStratifiedSampling:
    def test_every_relevant_record_is_kept(self, tiny_dataset):
        """Recall must be exact over the corpus, so no positive is dropped."""
        sampled = stratified_sample(tiny_dataset, n_irrelevant=1)
        assert sampled.n_relevant == tiny_dataset.n_relevant
        assert len(sampled.records) == tiny_dataset.n_relevant + 1

    def test_it_says_what_it_did_to_the_prevalence(self, tiny_dataset):
        sampled = stratified_sample(tiny_dataset, n_irrelevant=1)
        assert any("not the prevalence of the corpus" in n for n in sampled.notes)

    def test_it_is_deterministic(self, tiny_dataset):
        a = stratified_sample(tiny_dataset, n_irrelevant=2, seed=7)
        b = stratified_sample(tiny_dataset, n_irrelevant=2, seed=7)
        assert [r.source_row for r in a.records] == [r.source_row for r in b.records]

    def test_asking_for_more_than_there_are_returns_everything(self, tiny_dataset):
        sampled = stratified_sample(tiny_dataset, n_irrelevant=10_000)
        assert len(sampled.records) == len(tiny_dataset.records)


class TestTheRunner:
    def _run(self, dataset, tmp_path, **kwargs):
        return run_benchmark(
            dataset,
            protocol=kwargs.pop("protocol", PROTOCOL),
            client_factory=lambda project: AIClient(
                project, MockProvider(), stage="screen", sleep=lambda _: None
            ),
            workspace=tmp_path / "bench",
            **kwargs,
        )

    def test_it_screens_and_scores(self, tiny_dataset, tmp_path):
        result = self._run(tiny_dataset, tmp_path)
        assert result.score.n_records == 6
        assert result.score.n_relevant == 3
        assert result.n_ai_calls > 0
        # Every record got one of the three decisions and was scored.
        assert sum(result.score.counts.values()) == 6

    def test_the_decisions_line_up_with_the_labels(self, tiny_dataset, tmp_path):
        """The failure that would quietly invalidate every number.

        The mock model keys off protocol words, so the tourism records
        should fare differently from the soil chemistry one. What is being
        asserted is alignment, not accuracy: if the order slipped, the
        irrelevant records would score like the relevant ones.
        """
        result = self._run(tiny_dataset, tmp_path)
        relevant_seen = sum(
            result.score.counts[f"relevant_{d}"]
            for d in ("include", "exclude", "unclear")
        )
        irrelevant_seen = sum(
            result.score.counts[f"irrelevant_{d}"]
            for d in ("include", "exclude", "unclear")
        )
        assert relevant_seen == 3
        assert irrelevant_seen == 3

    def test_a_lost_record_is_named_not_just_counted(self, tiny_dataset, tmp_path):
        result = self._run(tiny_dataset, tmp_path)
        assert len(result.lost_records) == result.score.n_lost
        for lost in result.lost_records:
            assert lost["title"]
            assert "had_abstract" in lost

    def test_the_result_records_what_produced_it(self, tiny_dataset, tmp_path):
        """A number nobody can trace to a model and a protocol is not evidence."""
        result = self._run(tiny_dataset, tmp_path)
        assert result.protocol == PROTOCOL
        assert result.provider == "MockProvider"
        assert result.dataset["label_column"] == "label_abstract_screening"
        assert result.started_at and result.finished_at
        assert "licence" in result.dataset

    def test_it_refuses_to_invent_a_protocol(self, tiny_dataset, tmp_path):
        """The eligibility wording drives the result; it must come from the
        published review, not from this harness."""
        with pytest.raises(BenchmarkError, match="will not write one"):
            self._run(tiny_dataset, tmp_path, protocol="   ")

    def test_the_dataset_notes_reach_the_result(self, tiny_dataset, tmp_path):
        """So nobody reads a score without the caveats attached to it."""
        result = self._run(tiny_dataset, tmp_path)
        assert any("no abstract" in n for n in result.notes)

    def test_a_sampled_dataset_carries_its_warning_into_the_result(
        self, tiny_dataset, tmp_path
    ):
        sampled = stratified_sample(tiny_dataset, n_irrelevant=1)
        result = self._run(sampled, tmp_path)
        assert any("not the prevalence of the corpus" in n for n in result.notes)

    def test_nothing_is_left_in_the_workspace_that_claims_to_be_a_review(
        self, tiny_dataset, tmp_path
    ):
        """The benchmark project is a test artefact, not somebody's review.

        It is a real .slr file, so its title says what it is -- otherwise
        it could be opened a year later and mistaken for evidence.
        """
        result = self._run(tiny_dataset, tmp_path)
        assert result.dataset["name"] == "tiny"
        from slr_studio.core.project import Project

        project = Project.open(tmp_path / "bench" / "project.slr", actor="human:test")
        try:
            assert project.info()["title"].startswith("Benchmark:")
        finally:
            project.close()


class TestTheCurrentSynergyExport:
    """`synergy get` (synergy-dataset 2.x) names the title/abstract label
    `label_abstract_included`. Missing it means falling through to
    `label_included`, the post-full-text label, and scoring a title/abstract
    screener against decisions it could not have made."""

    def test_it_picks_the_abstract_label_under_its_current_name(self, tmp_path):
        columns = ["openalex_id", "doi", "title", "abstract",
                   "label_included", "label_abstract_included"]
        rows = [
            {"openalex_id": "W1", "title": "Kept at title/abstract, dropped at full text",
             "abstract": "x", "label_included": 0, "label_abstract_included": 1},
            {"openalex_id": "W2", "title": "Kept at both",
             "abstract": "y", "label_included": 1, "label_abstract_included": 1},
            {"openalex_id": "W3", "title": "Excluded",
             "abstract": "z", "label_included": 0, "label_abstract_included": 0},
        ]
        path = _write_csv(tmp_path / "synergy.csv", rows, columns=columns)
        dataset = load_csv(path)
        assert dataset.label_column == "label_abstract_included"
        assert sum(r.relevant for r in dataset.records) == 2


class TestTheCostGate:
    """Integrity rule: a cost estimate in tokens and money is shown before
    every bulk AI run, and nothing is spent until it is accepted."""

    def _factory(self):
        return lambda project: AIClient(
            project, MockProvider(), stage="screen", sleep=lambda _: None
        )

    def test_the_estimate_is_offered_before_any_call(self, tiny_dataset, tmp_path):
        seen = []

        def confirm(estimate):
            seen.append(estimate)
            return True

        run_benchmark(
            tiny_dataset, protocol=PROTOCOL, client_factory=self._factory(),
            workspace=tmp_path / "b", confirm=confirm,
        )
        assert len(seen) == 1
        estimate = seen[0]
        assert estimate.n_calls == 12  # two screeners x six records
        assert estimate.total_tokens > 0

    def test_declining_spends_nothing(self, tiny_dataset, tmp_path):
        from slr_studio.evaluation.benchmark import BenchmarkCancelled

        provider = MockProvider()
        factory = lambda project: AIClient(  # noqa: E731
            project, provider, stage="screen", sleep=lambda _: None
        )
        with pytest.raises(BenchmarkCancelled):
            run_benchmark(
                tiny_dataset, protocol=PROTOCOL, client_factory=factory,
                workspace=tmp_path / "b", confirm=lambda estimate: False,
            )
        assert getattr(provider, "calls", []) == []
