"""A small demo review that ships with the application.

Four synthetic exports, one per database, with the overlaps a real search
produces: the same study found in three databases, a conference paper later
published as an article, and a replication that shares its title with the
original. Opening the demo is the fastest way to see what the app does, and
the end-to-end test runs it on every build.

The references are invented. They are not real studies and must never be
cited as such.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["DEMO_DIR", "demo_files", "build_demo_project", "DEMO_SEEDS"]

DEMO_DIR = Path(__file__).parent / "data"

#: Seed studies for the demo: one the searches find, one they do not.
DEMO_SEEDS = (
    "10.1080/jsts.2021.004512",
    "10.9999/never-published.2020",
)

DEMO_TITLE = "Resident participation in sustainable tourism (demo)"

#: The date the bundled exports were prepared. The Gap Finder refuses to run
#: without a search date, and a demonstration project has no reviewer to type
#: one; this is the real date for this corpus, which is what makes the
#: demonstration's claims of absence truthful about the demonstration.
DEMO_SEARCHED_TO = "2026-03-01"

DEMO_OBJECTIVES = (
    "Map what is known about how residents participate in tourism planning, "
    "and what that participation is associated with."
)


def demo_files() -> list[Path]:
    """The bundled export files, in the order a reviewer would import them."""
    order = (
        "scopus_export.ris",
        "web_of_science_export.ris",
        "ebsco_export.ris",
        "proquest_export.ris",
    )
    return [DEMO_DIR / name for name in order if (DEMO_DIR / name).is_file()]


def demo_pdfs() -> list[Path]:
    """The bundled sample reports.

    Invented papers, not real studies. They exist so the full-text stage has
    something to read: a methods section, a findings section, and one page
    with no text layer so the OCR path is exercised.
    """
    directory = Path(__file__).parent / "pdfs"
    return sorted(directory.glob("*.pdf")) if directory.is_dir() else []


def build_demo_project(
    root: str | Path,
    *,
    actor: str = "human:demo",
    screen: bool = False,
    fulltext: bool = False,
    extract: bool = False,
    synthesise: bool = False,
    framework: bool = False,
    gaps: bool = False,
):
    """Create a project folder, import the demo exports and deduplicate.

    With ``screen=True`` it goes on to lock the protocol and screen every
    record with the built-in offline screener, so the whole pipeline can be
    seen working without an API key. Those answers come from keyword rules and
    are recorded as such; nothing about them should be mistaken for a real
    screening decision.
    """
    from ..core.project import Project
    from ..dedup import DedupEngine
    from ..importers.service import ImportService

    project = Project.create(
        root,
        DEMO_TITLE,
        actor=actor,
        objectives=DEMO_OBJECTIVES,
        research_questions=[
            "How do residents participate in tourism planning?",
            "What is resident participation associated with?",
        ],
        framework_type="PCC",
        inclusion_criteria=(
            "Empirical reports of resident or community participation in tourism "
            "planning or governance, any country, 2000 onwards."
        ),
    )
    service = ImportService(project)
    for doi in DEMO_SEEDS:
        service.add_seed(doi)
    for path in demo_files():
        service.import_file(path)
    DedupEngine(project).run(actor=actor)
    if screen or fulltext or extract or synthesise or framework or gaps:
        _screen_demo(project, actor=actor)
    if fulltext or extract or synthesise or framework or gaps:
        _fulltext_demo(project, actor=actor)
    if extract or synthesise or framework or gaps:
        _extract_demo(project, actor=actor)
    if synthesise or framework or gaps:
        _synthesise_demo(project, actor=actor)
    if framework or gaps:
        _framework_demo(project, actor=actor)
    if gaps:
        _gaps_demo(project, actor=actor)
    return project


def _gaps_demo(project, *, actor: str) -> None:
    """Carry the demo through to a gap register.

    The search date is set here because the Gap Finder refuses to run without
    one, and a demonstration project has no reviewer to type it. It is the
    date the bundled exports were prepared, which is what makes the
    demonstration's absences truthful about the demonstration's own corpus.

    The freshness check is left alone: it is the one step that reaches the
    network, and a demonstration project must not.
    """
    from ..ai.client import AIClient
    from ..ai.mock import MockProvider
    from ..gaps import GapEngine

    project.update_info(
        searched_to=DEMO_SEARCHED_TO,
        stimulus_attributes="source; certainty; framing; lead time",
        theories="protection motivation theory: efficacy, perceived threat",
    )
    engine = GapEngine(
        project, AIClient(project, MockProvider(), stage="gaps")
    )
    engine.quick_scan(actor=actor)
    engine.find_gaps(actor=actor)
    engine.draft_studies(actor=actor, top=5)
    engine.write_gap_map(actor=actor, dimension="country")


def _framework_demo(project, *, actor: str) -> None:
    """Carry the demo through to figures and propositions.

    The themes and the propositions are the reviewer's to approve, and there
    is no reviewer here, so both are taken as proposed and the reason logged
    says so. The offline double groups constructs by the order they were
    listed, which is visibly arbitrary -- the demonstration shows the stage
    working, not a framework anyone should cite.
    """
    from ..ai.client import AIClient
    from ..ai.mock import MockProvider
    from ..framework import FrameworkEngine

    engine = FrameworkEngine(
        project, AIClient(project, MockProvider(), stage="framework")
    )
    engine.propose_themes(actor=actor)
    for theme in engine.themes():
        engine.decide_theme(
            theme["id"],
            approved=True,
            reason="Approved unread while building the demonstration project.",
            actor=actor,
        )
    engine.draft_propositions(actor=actor)
    engine.draw_evidence_maps(actor=actor)
    engine.draw_integrated_diagram(actor=actor)


def _synthesise_demo(project, *, actor: str) -> None:
    """Carry the demo through to a graded inventory.

    Three of the seven steps are the reviewer's alone -- approving the
    codebook, orienting each construct, confirming a shared sample -- and
    there is no reviewer here. The demonstration takes the model's proposal
    in each case and says so in the reason it logs, which is also what makes
    the resulting grades unusable as findings.
    """
    from ..ai.client import AIClient
    from ..ai.mock import MockProvider
    from ..synthesis import SynthesisEngine

    engine = SynthesisEngine(
        project, AIClient(project, MockProvider(), stage="synthesise")
    )
    engine.propose_codebook(actor=actor)
    engine.approve_codebook(
        actor=actor,
        note="Demonstration project: the proposal was accepted unread, so this "
             "codebook is not a reviewer's codebook.",
    )
    engine.map_labels(actor=actor)

    # Every construct needs a reference orientation before signs can be
    # compared. The demo keeps each as the model proposed it.
    for construct in engine.constructs_without_orientation():
        engine.set_orientation(construct["id"], "aligned", actor=actor)
    engine.harmonise(actor=actor)

    engine.find_dataset_links(actor=actor)
    for link in engine.proposed_links():
        engine.decide_link(
            link["id"],
            confirmed=True,
            reason="Confirmed unchecked while building the demonstration project.",
            actor=actor,
        )
    engine.build_datasets(actor=actor)
    engine.grade(actor=actor)
    engine.run_sensitivity(actor=actor)
    engine.explain_contradictions(actor=actor)


def _extract_demo(project, *, actor: str) -> None:
    from ..ai.client import AIClient
    from ..ai.mock import MockProvider
    from ..extraction import ExtractionEngine, SecondCoder
    from ..fulltext import EligibilityEngine

    # The full-text stage queues every inclusion for a human. In the demo the
    # reviewer is not there, so the inclusions are confirmed as-is and that
    # confirmation is logged like any other.
    eligibility = EligibilityEngine(
        project, AIClient(project, MockProvider(), stage="fulltext")
    )
    for item in eligibility.review_queue():
        if item["reason"] == "confirm_inclusion":
            eligibility.resolve(
                item["record_id"], "include",
                reason="Confirmed unchanged while building the demonstration project.",
                actor=actor,
            )
    ExtractionEngine(
        project, AIClient(project, MockProvider(), stage="extract")
    ).run(actor=actor)
    SecondCoder(project).choose_sample(actor=actor)


def _fulltext_demo(project, *, actor: str) -> None:
    from ..ai.client import AIClient
    from ..ai.mock import MockProvider
    from ..fulltext import EligibilityEngine, RetrievalService

    retrieval = RetrievalService(project)
    retrieval.attach_many(demo_pdfs(), actor=actor)
    EligibilityEngine(
        project, AIClient(project, MockProvider(), stage="fulltext")
    ).run(actor=actor)


def _screen_demo(project, *, actor: str) -> None:
    from ..ai.client import AIClient
    from ..ai.mock import MockProvider
    from ..ai.settings import ModelSettings, save_settings
    from ..screening import ScreeningEngine, ScreeningSettings

    project.protocol.lock(actor=actor)
    save_settings(project, ModelSettings(offline=True), actor=actor)
    engine = ScreeningEngine(
        project,
        AIClient(project, MockProvider(), stage="screen"),
        ScreeningSettings(),
    )
    engine.waive_pilot(
        "Demonstration project: the offline screener is not a reviewer, so a "
        "pilot against it would mean nothing.",
        actor=actor,
    )
    engine.run_full(actor=actor)
    engine.run_qc(actor=actor)
