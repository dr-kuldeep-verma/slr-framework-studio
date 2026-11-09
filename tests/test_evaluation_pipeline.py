"""The pipeline benchmark: scoring rules and an end-to-end run on the mock."""

from __future__ import annotations

import csv
import json
import shutil

import pytest

from slr_studio.ai.client import AIClient
from slr_studio.ai.mock import MockProvider
from slr_studio.demo import demo_pdfs
from slr_studio.evaluation.pipeline import (
    PipelineCancelled,
    PipelineError,
    PipelineRecord,
    find_arms,
    find_outcomes,
    load_records,
    match_findings,
    render_pipeline_report,
    run_pipeline_benchmark,
    score_codebook,
    score_dataset_links,
    score_extraction,
    score_fulltext,
)

GOLD = {
    "studies": [
        {"study": "A 2001", "records": [1, 5], "design": "retrospective_observational",
         "arms": ["dpen", "zinc"], "outcomes": ["mortality", "side_effects"], "n": 40, "n_original": 44},
        {"study": "B 2006", "records": [2], "design": "randomised_controlled_trial",
         "arms": ["trientine", "ttm"], "outcomes": ["neuro_deterioration"], "n": 48, "n_original": 48},
    ],
    "synthesis": {"findings": [
        {"comparison": "dpen vs zinc", "outcome": "discontinuation", "verdict": "dpen higher", "clear": True},
    ]},
}


# -- matching ----------------------------------------------------------------


def test_arm_and_outcome_keywords():
    assert find_arms("D-penicillamine versus zinc sulphate") == {"dpen", "zinc"}
    assert find_arms("Ammonium tetrathiomolybdate (TTM)") == {"ttm"}
    assert find_arms("untreated siblings") == {"no_treatment"}
    assert "neuro_deterioration" in find_outcomes("early neurological worsening")
    assert "olt" in find_outcomes("need for liver transplantation")
    assert find_outcomes("copper excretion") == set()


# -- scoring -------------------------------------------------------------------


def _row(sid, label, decision, retrieved=True, found="cited", queued=False):
    return {"source_id": sid, "label": label, "retrieved": retrieved,
            "ai_decision": decision, "quote_found_on": found, "queued": queued}


def test_score_fulltext_counts_unclear_as_kept_and_skips_unretrieved():
    rows = [
        _row(1, 1, "include"), _row(2, 1, "unclear", queued=True),
        _row(3, 1, "exclude", found="nowhere"), _row(4, 0, "exclude"),
        _row(5, 0, "include", found="elsewhere"), _row(6, 1, None, retrieved=False),
    ]
    s = score_fulltext(rows)
    assert s["sought"] == 6 and s["retrieved"] == 5 and s["assessed"] == 5
    assert (s["recall"]["k"], s["recall"]["n"]) == (2, 3)
    assert (s["strict_recall"]["k"], s["strict_recall"]["n"]) == (1, 3)
    assert (s["specificity"]["k"], s["specificity"]["n"]) == (1, 2)
    assert s["lost"] == [3]
    assert s["quotes"] == {"checked": 5, "cited_page": 3, "elsewhere": 1, "nowhere": 1}


def test_score_extraction_counts_a_study_once_across_its_reports():
    extractions = {
        1: {"design_family": "longitudinal", "sample_size": 44, "design": "cohort"},
        5: {"design_family": "cross_sectional", "sample_size": None},
        2: {"design_family": "quasi_experiment", "sample_size": 50},
    }
    rels = {
        1: [{"predictor_label": "D-penicillamine", "outcome_label": "death", "quote": "x",
             "quote_found_on": "cited", "verify_agrees": 1, "needs_review": 0}],
        5: [{"predictor_label": "zinc", "outcome_label": "adverse events", "quote": "",
             "verify_agrees": None, "needs_review": 1}],
        2: [{"predictor_label": "TTM", "outcome_label": "neurological deterioration", "quote": "y",
             "quote_found_on": "nowhere", "verify_agrees": 0, "needs_review": 1}],
    }
    s = score_extraction(GOLD, extractions, rels)
    assert s["studies_covered"] == 2
    a = s["per_study"][0]
    assert a["design_correct"] and a["size_correct"]
    assert a["arms_found"] == ["dpen", "zinc"] and a["outcomes_found"] == ["mortality", "side_effects"]
    b = s["per_study"][1]
    assert not b["design_correct"] and not b["size_correct"]
    assert b["arms_found"] == ["ttm"]
    assert (s["arms"]["k"], s["arms"]["n"]) == (3, 4)
    assert (s["outcomes"]["k"], s["outcomes"]["n"]) == (3, 3)
    assert (s["direction_check"]["k"], s["direction_check"]["n"]) == (1, 2)


def test_score_codebook_only_counts_covered_studies():
    constructs = [{"name": "Penicillamine therapy", "definition": ""},
                  {"name": "Mortality", "definition": "death from any cause"}]
    s = score_codebook(GOLD, constructs, covered=["A 2001"])
    assert (s["arms"]["k"], s["arms"]["n"]) == (1, 2)
    assert (s["outcomes"]["k"], s["outcomes"]["n"]) == (1, 2)


def test_score_dataset_links_against_same_study_pairs():
    s = score_dataset_links(GOLD, proposed=[(5, 1), (1, 2)], extracted=[1, 2, 5])
    assert s["true_pairs"] == [(1, 5)]
    assert (s["precision"]["k"], s["precision"]["n"]) == (1, 2)
    assert (s["recall"]["k"], s["recall"]["n"]) == (1, 1)


def test_match_findings_lists_candidate_links():
    inv = [{"predictor": "Penicillamine", "outcome": "Treatment discontinuation",
            "direction": "positive", "grade": "tentative", "n_datasets": 2},
           {"predictor": "Zinc", "outcome": "Mortality", "direction": "none", "grade": "tentative"}]
    out = match_findings(GOLD, inv)
    assert len(out) == 1 and len(out[0]["links"]) == 1
    assert out[0]["links"][0]["predictor"] == "Penicillamine"


# -- loading -------------------------------------------------------------------


def test_load_records_joins_abstracts(tmp_path):
    sought = tmp_path / "s.csv"
    sought.write_text("record_id,openalex_id,doi,title,label\n7,https://openalex.org/W1,https://doi.org/10.1/x,T,1\n",
                      encoding="utf-8")
    corpus = tmp_path / "c.csv"
    corpus.write_text("openalex_id,title,abstract_original\nhttps://openalex.org/w1,T,An abstract\n",
                      encoding="utf-8")
    [r] = load_records(sought, corpus)
    assert (r.source_id, r.doi, r.abstract, r.label) == (7, "10.1/x", "An abstract", 1)


# -- the run -------------------------------------------------------------------


def _setup(tmp_path):
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    files = demo_pdfs()
    for sid, path in zip((1, 2, 3), files):
        shutil.copy(path, pdfs / f"{sid}.pdf")
    titles = {
        1: "Resident participation in sustainable tourism planning: evidence from coastal Kerala",
        2: "Benefit sharing and perceived fairness in community-based ecotourism",
        3: "Volunteer tourism and host community voice: a scoping review",
        4: "A record with no open-access copy",
    }
    records = [PipelineRecord(sid, t, "", "", 2020, 1 if sid in (1, 2) else 0) for sid, t in titles.items()]
    return records, pdfs


def _factory(project, stage):
    return AIClient(project, MockProvider(), stage=stage)


def test_pipeline_refuses_without_protocol(tmp_path):
    records, pdfs = _setup(tmp_path)
    with pytest.raises(PipelineError):
        run_pipeline_benchmark(records, gold=GOLD, pdf_dir=pdfs, protocol=" ",
                               client_factory=_factory, workspace=tmp_path / "w")


def test_declined_estimate_makes_no_model_call(tmp_path):
    records, pdfs = _setup(tmp_path)
    seen = []
    with pytest.raises(PipelineCancelled):
        run_pipeline_benchmark(
            records, gold=GOLD, pdf_dir=pdfs, protocol="Resident participation in tourism.",
            client_factory=_factory, workspace=tmp_path / "w",
            confirm=lambda est, note: seen.append(note) or False,
        )
    assert seen and "Full text" in seen[0]
    from slr_studio.core.project import Project

    project = Project.open(tmp_path / "w")
    try:
        assert project.db.query_one("SELECT COUNT(*) AS n FROM ai_call")["n"] == 0
    finally:
        project.close()


def test_pipeline_runs_end_to_end_on_the_mock_and_resumes(tmp_path):
    records, pdfs = _setup(tmp_path)
    kwargs = dict(gold=GOLD, pdf_dir=pdfs, protocol="Resident participation in tourism planning.",
                  client_factory=_factory, workspace=tmp_path / "w", confirm=lambda e, n: True)
    report = run_pipeline_benchmark(records, **kwargs)
    ft = report["fulltext"]
    assert ft["sought"] == 4 and ft["retrieved"] == 3 and ft["assessed"] == 3
    assert report["complete"], report["stopped"]
    assert report["extraction"]["studies_covered"] >= 1
    assert report["framework"]["themes"], report["failures"]
    text = render_pipeline_report(report)
    assert "Full-text eligibility" in text and "Framework" in text

    # The script's decisions are logged under its own name, with its reason.
    from slr_studio.core.project import Project

    project = Project.open(tmp_path / "w")
    try:
        rows = project.db.query(
            "SELECT reason FROM fulltext_decision WHERE reviewer_role = 'human'"
        )
        assert rows and all("Benchmark script" in r["reason"] for r in rows)
        n_calls = project.db.query_one("SELECT COUNT(*) AS n FROM ai_call")["n"]
    finally:
        project.close()

    # Running again changes nothing and asks nothing new.
    again = run_pipeline_benchmark(records, **kwargs)
    assert again["fulltext"] == {**ft}
    project = Project.open(tmp_path / "w")
    try:
        assert project.db.query_one("SELECT COUNT(*) AS n FROM ai_call")["n"] == n_calls
    finally:
        project.close()


def test_pipeline_refuses_a_different_run_in_the_same_workspace(tmp_path):
    records, pdfs = _setup(tmp_path)
    kwargs = dict(gold=GOLD, pdf_dir=pdfs, client_factory=_factory,
                  workspace=tmp_path / "w", confirm=lambda e, n: True)
    run_pipeline_benchmark(records, protocol="One protocol.", **kwargs)
    with pytest.raises(PipelineError):
        run_pipeline_benchmark(records, protocol="Another protocol.", **kwargs)


def test_cli_pipeline_on_the_mock(tmp_path):
    from slr_studio.cli import main

    records, pdfs = _setup(tmp_path)
    sought = tmp_path / "sought.csv"
    with open(sought, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["record_id", "openalex_id", "doi", "title", "label"])
        for r in records:
            w.writerow([r.source_id, "", "", r.title, r.label])
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps(GOLD), encoding="utf-8")
    protocol = tmp_path / "p.txt"
    protocol.write_text("Resident participation in tourism planning.", encoding="utf-8")
    out = tmp_path / "out" / "report.md"
    code = main(["benchmark-pipeline", str(sought), "--gold", str(gold), "--pdfs", str(pdfs),
                 "--protocol", str(protocol), "--workspace", str(tmp_path / "w"),
                 "--out", str(out), "--mock"])
    assert code == 0
    assert out.exists() and out.with_suffix(".json").exists()
    assert "Full-text eligibility" in out.read_text(encoding="utf-8")


def test_pipeline_refuses_when_no_relevant_record_has_a_full_text(tmp_path):
    records, _ = _setup(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PipelineError, match="no full text"):
        run_pipeline_benchmark(records, gold=GOLD, pdf_dir=empty, protocol="P.",
                               client_factory=_factory, workspace=tmp_path / "w")
    assert not (tmp_path / "w" / "project.slr").exists()


# -- S-O-B-C scoring --------------------------------------------------------------

from slr_studio.evaluation.pipeline import construct_code, mmat_band, score_sobc

SOBC_GOLD = {
    "kind": "sobc",
    "same_data": [[1, 2]],
    "studies": [
        {"study": "A_2020", "records": [1], "included": 1, "design": "Quantitative",
         "codes": ["S1", "O5", "B1"], "mmat": 4, "verified": True},
        {"study": "B_2021", "records": [2], "included": 1, "design": "Qualitative",
         "codes": ["S2", "B2", "C1"], "mmat": 1, "verified": False},
        {"study": "C_2022", "records": [3], "included": 0, "design": "Qualitative",
         "codes": ["S2"], "mmat": 0, "verified": False},
    ],
}


def test_construct_code_and_mmat_band():
    assert construct_code("S1 Economic incentives") == "S1"
    assert construct_code("B6 — non-participation") == "B6"
    assert construct_code("Other") is None
    assert [mmat_band(x) for x in (5, 4, 3, 2, 1, 0, None)] == [
        "low", "low", "moderate", "moderate", "high", "high", None]


def test_score_sobc_counts_codes_design_and_appraisal():
    extractions = {
        1: {"design_family": "cross_sectional", "appraisal_concern": "low", "appraisal_instrument": "MMAT"},
        2: {"design_family": "case_study", "appraisal_concern": "moderate", "appraisal_instrument": "CASP"},
        3: {"design_family": "qualitative"},
    }
    coded = {1: {"S1", "O5", "C4"}, 2: {"S2"}, 3: {"S2"}}
    s = score_sobc(SOBC_GOLD, extractions, {}, coded)
    a = s["all"]
    assert s["studies_covered"] == 2 and a["n"] == 2          # excluded study ignored
    assert (a["recall"]["k"], a["recall"]["n"]) == (3, 6)
    assert (a["precision"]["k"], a["precision"]["n"]) == (3, 4)
    assert (a["design"]["k"], a["design"]["n"]) == (2, 2)
    assert (a["appraisal"]["k"], a["appraisal"]["n"]) == (1, 2)
    assert (a["mmat_used"]["k"], a["mmat_used"]["n"]) == (1, 2)
    assert a["per_code"]["S1"] == {"gold": 1, "app": 1, "both": 1, "kappa": 1.0}
    assert s["verified"]["n"] == 1


def test_same_data_groups_count_as_one_study_for_links():
    s = score_dataset_links(SOBC_GOLD, proposed=[(1, 2)], extracted=[1, 2, 3])
    assert s["true_pairs"] == [(1, 2)] and s["recall"]["k"] == 1


def test_pipeline_top_down_sobc_on_the_mock(tmp_path):
    records, pdfs = _setup(tmp_path)
    info = {
        "framework_type": "SOBC",
        "framework_approach": "top_down",
        "a_priori_constructs": "S1 Economic incentives — perceived economic benefits\nB2 Participation in planning — taking part in decisions",
        "a_priori_theory": "Stimulus-Organism-Behaviour-Consequence",
        "a_priori_citation": "Test citation",
    }
    gold = {"kind": "sobc", "same_data": [], "studies": [
        {"study": f"S{r.source_id}", "records": [r.source_id], "included": r.label,
         "design": "Quantitative", "codes": ["S1"], "mmat": 3, "verified": False}
        for r in records]}
    report = run_pipeline_benchmark(
        records, gold=gold, pdf_dir=pdfs, protocol="Resident participation in tourism planning.",
        client_factory=_factory, workspace=tmp_path / "w", confirm=lambda e, n: True,
        project_info=info,
    )
    assert report["extraction"]["kind"] == "sobc"
    # The mock's labels need not map onto these two constructs, so later
    # steps may find nothing to grade; the codebook itself must not fail.
    assert "codebook" not in report["failures"], report["failures"]
    assert "codebook_approved" not in report["failures"], report["failures"]
    assert report["extraction"]["studies_covered"] >= 1
    text = render_pipeline_report(report)
    assert "S-O-B-C coding" in text
    from slr_studio.core.project import Project

    project = Project.open(tmp_path / "w")
    try:
        names = [r["name"] for r in project.db.query("SELECT name FROM construct")]
        assert any(n.startswith("S1") for n in names), names
    finally:
        project.close()
