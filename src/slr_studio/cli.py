"""Headless command line for the pipeline.

The acceptance test says a reviewer never has to open a terminal, and they
never do -- everything here is reachable from the window. This exists so the
test suite, continuous integration and a support engineer can drive the same
code paths without a display.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.project import Project
from .dedup import DedupEngine, DedupSettings
from .importers.service import ImportService

__all__ = ["main"]


def _open(path: str) -> Project:
    return Project.open(path, actor="human:cli")


def cmd_new(args: argparse.Namespace) -> int:
    project = Project.create(
        args.path,
        args.title,
        actor="human:cli",
        objectives=args.objectives or "",
        contact_email=args.email or "",
    )
    print(f"Created {project.root / 'project.slr'}")
    project.close()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        service = ImportService(project)
        for seed in args.seed or []:
            service.add_seed(seed)
        summaries, failures = service.import_many(
            args.files, allow_reimport=args.allow_reimport
        )
        for summary in summaries:
            print(
                f"{summary.filename}: {summary.n_records} records "
                f"({summary.source_database}, {summary.format}/{summary.dialect}), "
                f"{summary.n_missing_abstract} without an abstract, "
                f"{summary.n_errors} unreadable"
            )
        for name, error in failures:
            print(f"{name}: FAILED -- {error}", file=sys.stderr)
        seeds = service.check_seeds()
        if seeds:
            found = sum(1 for s in seeds if s["found"])
            print(f"Seed check: {found}/{len(seeds)} found")
            for seed in seeds:
                if not seed["found"]:
                    print(f"  missing: {seed['doi']}")
        return 1 if failures else 0


def cmd_dedup(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        settings = DedupSettings(
            fuzzy_threshold=args.threshold, auto_threshold=args.auto_threshold
        )
        engine = DedupEngine(project, settings)
        result = engine.run(actor="human:cli")
        print(json.dumps(result.as_dict(), indent=2))
        if result.n_uncertain:
            print(
                f"\n{result.n_uncertain} pair(s) need a human decision: "
                "run `slr pairs` to see them."
            )
        return 0


def cmd_pairs(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        engine = DedupEngine(project)
        pending = engine.pending_pairs(limit=args.limit)
        if not pending:
            print("No pairs awaiting review.")
            return 0
        for pair in pending:
            print(f"\n--- pair {pair['id']} [{pair['rule']} {pair['score']:.3f}] ---")
            for side in ("a", "b"):
                record = pair[side]
                authors = ", ".join(record.get("authors", [])[:3])
                print(
                    f"  [{side}] #{record['id']} ({record['source_database']}) "
                    f"{record['year']} {authors}\n      {record['title']}\n"
                    f"      doi={record['doi'] or '-'}"
                )
            if pair["evidence"]:
                print(f"  evidence: {json.dumps(pair['evidence'])}")
        return 0


def cmd_decide(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        engine = DedupEngine(project)
        engine.decide_pair(
            args.pair_id,
            args.verdict == "duplicate",
            actor=f"human:{args.actor}",
            reason=args.reason or "",
        )
        print(f"Pair {args.pair_id} recorded as {args.verdict}.")
        return 0


def cmd_protocol(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        protocol = project.protocol
        if args.show:
            current = protocol.current()
            print(current.content if current else protocol.draft())
            return 0
        if args.lock:
            version = protocol.lock(actor="human:cli", reason=args.reason or "")
            path = protocol.export(
                project.exports_dir / f"protocol-v{version.version}.md"
            )
            print(
                f"Locked version {version.version} ({version.short_hash}); "
                f"written to {path.name}"
            )
            return 0
        summary = protocol.summary()
        if not summary["locked"]:
            print("The protocol has not been locked yet.")
        else:
            print(
                f"Version {summary['version']} locked {summary['locked_at']} "
                f"by {summary['locked_by']}\nSHA-256 {summary['content_hash']}"
            )
            print(
                "The form has changed since it was locked."
                if summary["has_unlocked_changes"]
                else "The form matches the locked version."
            )
        for amendment in protocol.amendments():
            seen = "after data were seen" if amendment["after_data_seen"] else "before data"
            print(
                f"  v{amendment['from_number']} → v{amendment['to_number']} "
                f"{amendment['changed_at']} ({seen}): {amendment['reason']}"
            )
        return 0


def _screening_engine(project, *, offline: bool):
    from .ai.client import AIClient
    from .ai.settings import load_settings, provider_from_settings
    from .screening import ScreeningEngine, ScreeningSettings

    settings = load_settings(project)
    if offline:
        settings.offline = True
    provider = provider_from_settings(settings)
    client = AIClient(project, provider, stage="screen", use_cache=settings.use_cache)
    return ScreeningEngine(
        project, client, ScreeningSettings(budget_usd=settings.budget_usd)
    )


def cmd_screen(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        engine = _screening_engine(project, offline=args.offline)
        if args.waive_pilot:
            engine.waive_pilot(args.waive_pilot, actor="human:cli")
            print("Pilot waived; the reason has been logged.")
        if not args.yes:
            estimate = engine.estimate_full_run()
            print(estimate.describe())
            for note in estimate.notes:
                print(f"  {note}")
            if input("Proceed? [y/N] ").strip().casefold() not in ("y", "yes"):
                print("Cancelled; nothing was spent.")
                return 0
        outcome = engine.run_full(actor="human:cli")
        print(json.dumps(outcome.as_dict(), indent=2))
        if args.qc:
            qc = engine.run_qc(actor="human:cli")
            print(f"\nQuality control re-read {qc.n_records}; "
                  f"{qc.n_qc_rescued} sent back for review.")
        counts = engine.counts()
        print(
            f"\n{counts['n_included']} included, {counts['n_excluded']} excluded, "
            f"{counts['n_unclear']} unclear, "
            f"{counts['n_awaiting_review']} waiting for a human."
        )
        return 0


def cmd_queue(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        engine = _screening_engine(project, offline=args.offline)
        items = engine.review_queue(limit=args.limit)
        if not items:
            print("Nothing is waiting for review.")
            return 0
        for item in items:
            print(
                f"\n--- record {item['record_id']} [{item['reason']}] "
                f"currently {item['current_decision'] or 'undecided'} ---"
            )
            print(f"  {item['title']}")
            print(f"  {item['detail']}")
            for decision in item["decisions"]:
                print(
                    f"    {decision['reviewer_role']:<12} {decision['decision']:<8} "
                    f"{decision['exclusion_code'] or '-':<4} {decision['reason'][:70]}"
                )
        return 0


def cmd_decide_record(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        engine = _screening_engine(project, offline=args.offline)
        engine.resolve(
            args.record_id,
            args.decision,
            exclusion_code=args.code or "",
            reason=args.reason or "",
            actor=f"human:{args.actor}",
        )
        print(f"Record {args.record_id} recorded as {args.decision}.")
        return 0


def cmd_retrieve(args: argparse.Namespace) -> int:
    from .fulltext import RetrievalService

    with _open(args.path) as project:
        service = RetrievalService(project)
        if args.find_open_access:
            stats = service.find_open_access()
            print(
                f"Looked up {stats['n_looked_up']} DOI(s); "
                f"{stats['n_open_access']} have a free copy; "
                f"{stats['n_locations']} location(s); "
                f"{stats['n_errors']} could not be checked."
            )
        if args.add:
            for outcome in service.attach_many(args.add):
                print(f"{outcome.filename}: {outcome.message}")
        if args.mark:
            record_id, status = args.mark
            service.set_status(int(record_id), status, reason=args.reason or "")
            print(f"Record {record_id} marked as {status}.")

        summary = service.summary()
        print(
            f"\n{summary['n_sought']} sought · {summary['n_retrieved']} retrieved · "
            f"{summary['n_not_retrieved']} not retrieved · "
            f"{summary['n_abstract_only']} abstract only · "
            f"{summary['n_pending']} still to find · "
            f"{summary['n_unmatched_pdfs']} file(s) to match by hand"
        )
        for item in service.unmatched():
            guess = (
                f" (closest: record {item['best_guess_record_id']} at "
                f"{item['best_guess_score']:.0%})"
                if item["best_guess_record_id"] else ""
            )
            print(f"  unmatched: {item['filename']}{guess}")
        return 0


def cmd_fulltext(args: argparse.Namespace) -> int:
    from .ai.client import AIClient
    from .ai.settings import load_settings, provider_from_settings
    from .fulltext import EligibilityEngine
    from .fulltext.engine import EligibilitySettings

    with _open(args.path) as project:
        settings = load_settings(project)
        if args.offline:
            settings.offline = True
        engine = EligibilityEngine(
            project,
            AIClient(project, provider_from_settings(settings), stage="fulltext",
                     use_cache=settings.use_cache),
            EligibilitySettings(budget_usd=settings.budget_usd),
        )
        if not args.yes:
            estimate = engine.estimate()
            print(estimate.describe())
            for note in estimate.notes:
                print(f"  {note}")
            if input("Proceed? [y/N] ").strip().casefold() not in ("y", "yes"):
                print("Cancelled; nothing was spent.")
                return 0
        outcome = engine.run(actor="human:cli")
        print(json.dumps(outcome.as_dict(), indent=2))
        counts = engine.counts()
        print(
            f"\n{counts['n_included']} included, {counts['n_excluded']} excluded, "
            f"{counts['n_unclear']} unclear, "
            f"{counts['n_awaiting_review']} waiting for a human."
        )
        for item in engine.review_queue(limit=args.limit):
            print(f"  record {item['record_id']} [{item['reason']}]: {item['detail']}")
        return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from .ai.client import AIClient
    from .ai.settings import load_settings, provider_from_settings
    from .extraction import ExtractionEngine, ExtractionSettings, SecondCoder

    with _open(args.path) as project:
        settings = load_settings(project)
        if args.offline:
            settings.offline = True
        engine = ExtractionEngine(
            project,
            AIClient(project, provider_from_settings(settings), stage="extract",
                     use_cache=settings.use_cache),
            ExtractionSettings(
                budget_usd=settings.budget_usd,
                verify_directions=not args.no_verify,
            ),
        )
        if not args.yes:
            estimate = engine.estimate()
            print(estimate.describe())
            for note in estimate.notes:
                print(f"  {note}")
            if input("Proceed? [y/N] ").strip().casefold() not in ("y", "yes"):
                print("Cancelled; nothing was spent.")
                return 0
        outcome = engine.run(actor="human:cli")
        print(json.dumps(outcome.as_dict(), indent=2))
        if args.second_coder_sample:
            chosen = SecondCoder(project).choose_sample(actor="human:cli")
            print(
                f"\nSecond-coding sample: {len(chosen)} report(s) — "
                + ", ".join(str(r) for r in chosen)
            )
        for item in engine.review_queue(limit=args.limit):
            print(f"  record {item['record_id']} [{item['reason']}]: "
                  f"{item['detail'].splitlines()[0][:90]}")
        return 0


def cmd_coders(args: argparse.Namespace) -> int:
    from .extraction import SecondCoder

    with _open(args.path) as project:
        comparison = SecondCoder(project).compare(actor="human:cli")
        print(comparison.describe())
        return 0


#: The synthesis in the order the stages depend on each other. Three of them
#: are the reviewer's own decisions, and a run without a display has to say so
#: rather than quietly deciding on their behalf.
_SYNTHESIS_STEPS = (
    "codebook", "mapping", "orientation", "datasets", "grade",
    "sensitivity", "contradictions",
)


def cmd_synthesise(args: argparse.Namespace) -> int:
    from .ai.client import AIClient
    from .ai.settings import load_settings, provider_from_settings
    from .synthesis import SynthesisEngine, SynthesisError, SynthesisSettings

    steps = _SYNTHESIS_STEPS if args.step is None else (args.step,)
    with _open(args.path) as project:
        settings = load_settings(project)
        if args.offline:
            settings.offline = True
        engine = SynthesisEngine(
            project,
            AIClient(project, provider_from_settings(settings), stage="synthesise",
                     use_cache=settings.use_cache),
            SynthesisSettings(budget_usd=settings.budget_usd),
        )
        if not args.yes:
            estimate = engine.estimate(steps[0])
            print(estimate.describe())
            if input("Proceed? [y/N] ").strip().casefold() not in ("y", "yes"):
                print("Cancelled; nothing was spent.")
                return 0
        waiting = ""
        try:
            for step in steps:
                waiting = _awaiting_reviewer(engine, step, args)
                if waiting:
                    break
                _run_synthesis_step(engine, step, args)
        except SynthesisError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        counts = engine.counts()
        print(
            f"\n{counts.get('n_constructs', 0)} construct(s), "
            f"{counts.get('n_labels', 0)} label(s), "
            f"{counts.get('n_datasets', 0)} dataset(s), "
            f"{counts.get('n_graded_links', 0)} graded link(s) "
            f"({counts.get('n_strong', 0)} strong, "
            f"{counts.get('n_contested', 0)} contested)"
        )
        if waiting:
            print(f"\nStopped: {waiting}")
            return 1
        return 0


def _awaiting_reviewer(engine, step: str, args: argparse.Namespace) -> str:
    """What the reviewer must decide before this step may run, if anything.

    Stopping here rather than letting the step raise is the difference
    between "the review is waiting for you" and "the program broke". The
    exit code says which: 1 for waiting, 2 for an error.
    """
    if args.accept_proposals:
        return ""
    if step in ("mapping", "orientation") and not engine.codebook_approved():
        return (
            "the codebook is waiting to be approved. Open the project and "
            "approve it, or re-run with --accept-proposals."
        )
    if step in ("datasets", "grade", "sensitivity", "contradictions"):
        if not engine.codebook_approved():
            return (
                "the codebook is waiting to be approved. Open the project and "
                "approve it, or re-run with --accept-proposals."
            )
        pending = engine.constructs_without_orientation()
        if pending:
            return (
                f"{len(pending)} construct(s) have no reference orientation. "
                "Set them in the application, or re-run with --accept-proposals."
            )
    return ""


def _run_synthesis_step(engine, step: str, args: argparse.Namespace) -> None:
    """Run one step and print what it did.

    The three steps that are the reviewer's own -- approving the codebook,
    orienting each construct, confirming a shared sample -- are taken here
    only when `--accept-proposals` says to, and every one of them is logged
    with a reason saying it was taken without being read.
    """
    actor = "human:cli"
    taken = (
        "Accepted from the command line with --accept-proposals, without "
        "being read by a reviewer."
    )
    if step == "codebook":
        outcome = engine.propose_codebook(actor=actor)
        print(outcome.describe())
        if args.accept_proposals and not engine.codebook_approved():
            engine.approve_codebook(actor=actor, note=taken)
            print("  Codebook approved unread (--accept-proposals).")
        elif not engine.codebook_approved():
            print(
                "  Not approved. Open the project and approve the codebook, or "
                "re-run with --accept-proposals."
            )
    elif step == "mapping":
        print(engine.map_labels(actor=actor).describe())
    elif step == "orientation":
        pending = engine.constructs_without_orientation()
        if pending and not args.accept_proposals:
            print(
                f"  {len(pending)} construct(s) have no reference orientation. "
                "Set them in the application, or re-run with --accept-proposals."
            )
        for construct in pending if args.accept_proposals else []:
            engine.set_orientation(construct["id"], "aligned", actor=actor)
        print(engine.harmonise(actor=actor).describe())
    elif step == "datasets":
        print(engine.find_dataset_links(actor=actor).describe())
        proposed = engine.proposed_links()
        if proposed and not args.accept_proposals:
            print(
                f"  {len(proposed)} proposed shared sample(s) await confirmation. "
                "Confirm them in the application, or re-run with "
                "--accept-proposals."
            )
        for link in proposed if args.accept_proposals else []:
            engine.decide_link(link["id"], confirmed=True, reason=taken, actor=actor)
        print(engine.build_datasets(actor=actor).describe())
    elif step == "grade":
        print(engine.grade(actor=actor).describe())
    elif step == "sensitivity":
        print(engine.run_sensitivity(actor=actor).describe())
    elif step == "contradictions":
        print(engine.explain_contradictions(actor=actor).describe())


#: The framework builder in the order the steps depend on each other.
_FRAMEWORK_STEPS = ("themes", "propositions", "evidence_maps", "integrated")


def cmd_framework(args: argparse.Namespace) -> int:
    from .ai.client import AIClient
    from .ai.settings import load_settings, provider_from_settings
    from .framework import FrameworkEngine, FrameworkError, FrameworkSettings

    steps = _FRAMEWORK_STEPS if args.step is None else (args.step,)
    with _open(args.path) as project:
        settings = load_settings(project)
        if args.offline:
            settings.offline = True
        engine = FrameworkEngine(
            project,
            AIClient(project, provider_from_settings(settings),
                     stage="framework", use_cache=settings.use_cache),
            FrameworkSettings(budget_usd=settings.budget_usd),
        )
        if not args.yes:
            print(engine.estimate(steps[0]).describe())
            if input("Proceed? [y/N] ").strip().casefold() not in ("y", "yes"):
                print("Cancelled; nothing was spent.")
                return 0

        waiting = ""
        try:
            for step in steps:
                if step in ("evidence_maps", "integrated") and not args.accept_proposals:
                    undecided = [
                        t for t in engine.themes() if t["status"] == "proposed"
                    ]
                    if undecided:
                        waiting = (
                            f"{len(undecided)} theme(s) are waiting to be "
                            "approved or rejected. One figure is drawn per "
                            "theme, so decide them in the application, or "
                            "re-run with --accept-proposals."
                        )
                        break
                print(_run_framework_step(engine, step, args))
        except FrameworkError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        counts = engine.counts()
        print(
            f"\n{counts.get('n_themes', 0)} theme(s), "
            f"{counts.get('n_propositions', 0)} proposition(s) "
            f"({counts.get('n_rejected', 0)} rejected), "
            f"{counts.get('n_figures', 0)} figure file(s)"
        )
        if waiting:
            print(f"\nStopped: {waiting}")
            return 1
        return 0


def _run_framework_step(engine, step: str, args: argparse.Namespace) -> str:
    actor = "human:cli"
    if step == "themes":
        outcome = engine.propose_themes(actor=actor)
        if args.accept_proposals:
            for theme in engine.themes():
                if theme["status"] == "proposed":
                    engine.decide_theme(
                        theme["id"],
                        approved=True,
                        reason="Approved from the command line with "
                               "--accept-proposals, without being read.",
                        actor=actor,
                    )
        return outcome.describe()
    if step == "propositions":
        return engine.draft_propositions(actor=actor).describe()
    if step == "evidence_maps":
        return engine.draw_evidence_maps(actor=actor).describe()
    return engine.draw_integrated_diagram(actor=actor).describe()


#: The Gap Finder's steps. The quick scan stands apart: it runs on the
#: imported records, before any review, and nothing downstream cites it.
_GAP_STEPS = ("quick", "full", "freshness", "studies", "map")


def cmd_gaps(args: argparse.Namespace) -> int:
    from .ai.client import AIClient
    from .ai.settings import load_settings, provider_from_settings
    from .gaps import GapEngine, GapError, GapSettings

    steps = (
        (args.step,) if args.step else ("full", "studies", "map")
    )
    with _open(args.path) as project:
        settings = load_settings(project)
        if args.offline:
            settings.offline = True
        engine = GapEngine(
            project,
            AIClient(project, provider_from_settings(settings), stage="gaps",
                     use_cache=settings.use_cache),
            GapSettings(budget_usd=settings.budget_usd),
        )
        if not args.yes:
            print(engine.estimate(steps[0]).describe())
            if input("Proceed? [y/N] ").strip().casefold() not in ("y", "yes"):
                print("Cancelled; nothing was spent.")
                return 0
        try:
            for step in steps:
                if step == "quick":
                    print(engine.quick_scan(actor="human:cli").describe())
                elif step == "full":
                    print(engine.find_gaps(actor="human:cli").describe())
                elif step == "freshness":
                    print(
                        engine.check_freshness(
                            actor="human:cli", top=args.top
                        ).describe()
                    )
                elif step == "studies":
                    print(
                        engine.draft_studies(
                            actor="human:cli", top=args.top or 5
                        ).describe()
                    )
                else:
                    written = engine.write_gap_map(
                        actor="human:cli", dimension=args.dimension
                    )
                    print(
                        "Gap map: "
                        + ", ".join(path.name for path in written)
                    )
        except GapError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        counts = engine.counts()
        print(
            f"\n{counts['n_gaps']} gap(s), {counts['n_with_study']} with a "
            f"drafted study, {counts['n_possibly_addressed']} possibly being "
            "addressed"
        )
        return 0


#: How far the demonstration project is carried, and what each shows.
_DEMO_STAGES = {
    "import": "the imported records, before anything was decided",
    "screen": "through title and abstract screening",
    "extract": "through full text and data extraction",
    "synthesise": "through the graded evidence inventory",
    "framework": "through the propositions and figures",
    "gaps": "the whole pipeline, including the gap register",
}


def cmd_demo(args: argparse.Namespace) -> int:
    """Build the bundled demonstration project.

    The first thing somebody does with this application is open it, and
    without a corpus or an API key there is nothing to look at. This builds
    a small real review end to end with a mocked model, so the stages can be
    clicked through before any key is entered.
    """
    from .demo import build_demo_project

    target = Path(args.path)
    if target.exists() and any(target.iterdir()):
        print(
            f"error: {target} already exists and is not empty. Choose "
            "another folder; this command will not write over a project.",
            file=sys.stderr,
        )
        return 2

    stage = args.stage
    flags = {name: False for name in
             ("screen", "fulltext", "extract", "synthesise", "framework", "gaps")}
    if stage in flags:
        flags[stage] = True
    elif stage != "import":
        flags["gaps"] = True

    print(f"Building the demonstration project in {target} ...")
    project = build_demo_project(target, **flags)
    try:
        counts = project.db.query_one("SELECT * FROM v_prisma_identification")
        print(
            f"\nDone: {_DEMO_STAGES.get(stage, 'the whole pipeline')}.\n"
            f"  {counts['n_identified']} record(s) identified, "
            f"{counts['n_duplicates_removed']} duplicate(s) removed.\n"
            f"  Open it with: slr status {target}\n"
            f"  Or open the folder in the application.\n"
            "\nThis is a demonstration, not a review. Its decisions were "
            "made by a mock model with no language model behind it, its "
            "PDFs are generated, and nothing in it is a finding about "
            "anything."
        )
        return 0
    finally:
        project.close()


def cmd_export_all(args: argparse.Namespace) -> int:
    from .exports import export_everything

    with _open(args.path) as project:
        outcome = export_everything(
            project, Path(args.out) if args.out else None
        )
        print(outcome.describe())
        return 2 if outcome.failed else 0


def cmd_export_gaps(args: argparse.Namespace) -> int:
    from .exports import (
        write_future_research_draft,
        write_gap_register,
        write_research_agenda,
    )

    with _open(args.path) as project:
        folder = Path(args.out) if args.out else project.exports_dir
        folder.mkdir(parents=True, exist_ok=True)
        for writer, name in (
            (write_gap_register, "gap-register.xlsx"),
            (write_research_agenda, "research-agenda.docx"),
            (write_future_research_draft, "future-research-draft.md"),
        ):
            print(f"Wrote {writer(project, folder / name)}")
        return 0


def cmd_export_inventory(args: argparse.Namespace) -> int:
    from .exports import write_graded_inventory

    with _open(args.path) as project:
        print(f"Wrote {write_graded_inventory(project, args.out)}")
        return 0


def cmd_export_matrix(args: argparse.Namespace) -> int:
    from .exports import write_extraction_matrix

    with _open(args.path) as project:
        print(f"Wrote {write_extraction_matrix(project, args.out)}")
        return 0


def cmd_export_log(args: argparse.Namespace) -> int:
    from .exports import write_screening_log

    with _open(args.path) as project:
        path = write_screening_log(project, args.out)
        print(f"Wrote {path}")
        return 0


def cmd_status(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        info = project.info()
        counts = project.counts()
        print(f"Project: {info['title']}")
        approach = info.get("framework_approach") or "bottom_up"
        wording = "top-down (deductive)" if approach == "top_down" else "bottom-up (inductive)"
        print(f"Framework: {info['framework_type']}, built {wording}")
        if approach == "top_down":
            from .core.protocol import parse_a_priori_constructs

            declared = parse_a_priori_constructs(info.get("a_priori_constructs", ""))
            print(f"Constructs specified in advance: {len(declared)}")
        print(
            f"Records identified: {counts.get('n_identified', 0)}; "
            f"duplicates removed: {counts.get('n_duplicates_removed', 0)}; "
            f"to screen: {counts.get('n_screened', 0)}"
        )
        protocol = project.protocol.summary()
        print(
            f"Protocol: version {protocol['version']} "
            f"({protocol['content_hash'][:12]}), "
            f"{protocol['n_amendments']} amendment(s)"
            if protocol["locked"]
            else "Protocol: not locked"
        )
        screening = project.db.query_one("SELECT * FROM v_prisma_screening")
        if screening and screening["n_to_screen"]:
            print(
                f"Screening: {screening['n_included']} included, "
                f"{screening['n_excluded']} excluded, "
                f"{screening['n_unclear']} unclear, "
                f"{screening['n_not_yet_screened']} not yet screened, "
                f"{screening['n_awaiting_review']} waiting for a human"
            )
        full = project.db.query_one("SELECT * FROM v_prisma_fulltext")
        if full and full["n_sought"]:
            print(
                f"Full text: {full['n_sought']} sought, "
                f"{full['n_retrieved']} retrieved, "
                f"{full['n_not_retrieved']} not retrieved, "
                f"{full['n_assessed']} assessed, "
                f"{full['n_included']} included, {full['n_excluded']} excluded, "
                f"{full['n_awaiting_review']} waiting for a human"
            )
        extraction = project.db.query_one("SELECT * FROM v_extraction_progress")
        if extraction and extraction["n_extracted"]:
            print(
                f"Extraction: {extraction['n_extracted']} report(s), "
                f"{extraction['n_relationships']} relationship(s), "
                f"{extraction['n_direction_mismatches']} direction(s) the check "
                f"disagreed with, "
                f"{extraction['n_awaiting_review']} waiting for a human"
            )
        synthesis = project.db.query_one("SELECT * FROM v_synthesis_progress")
        if synthesis and synthesis["n_graded_links"]:
            print(
                f"Synthesis: {synthesis['n_constructs']} construct(s), "
                f"{synthesis['n_datasets']} dataset(s), "
                f"{synthesis['n_graded_links']} graded link(s) "
                f"({synthesis['n_strong']} strong, "
                f"{synthesis['n_contested']} contested)"
            )
        framework = project.db.query_one("SELECT * FROM v_framework_progress")
        if framework and (framework["n_themes"] or framework["n_propositions"]):
            print(
                f"Framework: {framework['n_themes']} theme(s), "
                f"{framework['n_propositions']} proposition(s) "
                f"({framework['n_rejected']} rejected, "
                f"{framework['n_ungrounded']} citing nothing), "
                f"{framework['n_figures']} figure file(s)"
            )
        gaps = project.db.query("SELECT * FROM v_gap_summary")
        if gaps:
            total = sum(row["n_gaps"] for row in gaps)
            addressed = sum(row["n_possibly_addressed"] for row in gaps)
            print(
                f"Gaps: {total} in the register "
                + ", ".join(
                    f"{row['n_gaps']} {row['gap_type']}" for row in gaps
                )
                + (
                    f"; {addressed} possibly being addressed"
                    if addressed else ""
                )
            )
        print("\nPer database:")
        for row in project.counts_per_database():
            print(
                f"  {row['source_database']:<20} imported {row['n_imported']:>5}  "
                f"after dedup {row['n_after_dedup']:>5}"
            )
        breaks = project.audit.verify()
        print(
            "\nAudit chain: intact"
            if not breaks
            else f"\nAudit chain: {len(breaks)} problem(s) -- {breaks[0].reason}"
        )
        return 0


def cmd_audit(args: argparse.Namespace) -> int:
    with _open(args.path) as project:
        for entry in project.audit.entries(stage=args.stage, limit=args.limit):
            detail = json.dumps(entry.detail)
            if len(detail) > 160:
                detail = detail[:157] + "..."
            print(f"{entry.ts}  {entry.stage:<10} {entry.action:<18} {entry.actor:<16} {detail}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slr",
        description=(
            "SLR Framework Studio. Run a command to drive the pipeline "
            "without a display; run the application with no arguments at "
            "all to open the window."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="create a project folder")
    p_new.add_argument("path")
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--objectives")
    p_new.add_argument("--email", help="contact email, used later for Unpaywall")
    p_new.set_defaults(func=cmd_new)

    p_import = sub.add_parser("import", help="import export files")
    p_import.add_argument("path")
    p_import.add_argument("files", nargs="+")
    p_import.add_argument("--seed", action="append", help="seed study DOI (repeatable)")
    p_import.add_argument("--allow-reimport", action="store_true")
    p_import.set_defaults(func=cmd_import)

    p_dedup = sub.add_parser("dedup", help="run deduplication")
    p_dedup.add_argument("path")
    p_dedup.add_argument("--threshold", type=float, default=0.92)
    p_dedup.add_argument("--auto-threshold", type=float, default=0.97)
    p_dedup.set_defaults(func=cmd_dedup)

    p_pairs = sub.add_parser("pairs", help="list pairs awaiting a human decision")
    p_pairs.add_argument("path")
    p_pairs.add_argument("--limit", type=int, default=25)
    p_pairs.set_defaults(func=cmd_pairs)

    p_decide = sub.add_parser("decide", help="decide one uncertain pair")
    p_decide.add_argument("path")
    p_decide.add_argument("pair_id", type=int)
    p_decide.add_argument("verdict", choices=["duplicate", "distinct"])
    p_decide.add_argument("--actor", default="cli")
    p_decide.add_argument("--reason", default="")
    p_decide.set_defaults(func=cmd_decide)

    p_protocol = sub.add_parser("protocol", help="draft, lock or show the protocol")
    p_protocol.add_argument("path")
    p_protocol.add_argument("--lock", action="store_true", help="lock the current draft")
    p_protocol.add_argument("--reason", help="required when amending a locked protocol")
    p_protocol.add_argument("--show", action="store_true", help="print the document")
    p_protocol.set_defaults(func=cmd_protocol)

    p_screen = sub.add_parser("screen", help="screen titles and abstracts")
    p_screen.add_argument("path")
    p_screen.add_argument("--waive-pilot", metavar="REASON",
                          help="skip the pilot, recording why")
    p_screen.add_argument("--qc", action="store_true",
                          help="also run the quality-control pass")
    p_screen.add_argument("--offline", action="store_true",
                          help="use the built-in offline screener, not a real model")
    p_screen.add_argument("-y", "--yes", action="store_true",
                          help="skip the cost confirmation")
    p_screen.set_defaults(func=cmd_screen)

    p_queue = sub.add_parser("queue", help="records waiting for a human decision")
    p_queue.add_argument("path")
    p_queue.add_argument("--limit", type=int, default=20)
    p_queue.add_argument("--offline", action="store_true")
    p_queue.set_defaults(func=cmd_queue)

    p_decide_record = sub.add_parser("decide-record", help="settle one screened record")
    p_decide_record.add_argument("path")
    p_decide_record.add_argument("record_id", type=int)
    p_decide_record.add_argument("decision", choices=["include", "exclude", "unclear"])
    p_decide_record.add_argument("--code", help="exclusion code, required to exclude")
    p_decide_record.add_argument("--reason", default="")
    p_decide_record.add_argument("--actor", default="cli")
    p_decide_record.add_argument("--offline", action="store_true")
    p_decide_record.set_defaults(func=cmd_decide_record)

    p_retrieve = sub.add_parser("retrieve", help="find and attach full texts")
    p_retrieve.add_argument("path")
    p_retrieve.add_argument("--find-open-access", action="store_true",
                            help="ask Unpaywall where free copies are published")
    p_retrieve.add_argument("--add", nargs="+", metavar="PDF",
                            help="PDFs to match to records and store")
    p_retrieve.add_argument("--mark", nargs=2, metavar=("RECORD_ID", "STATUS"),
                            help="record a report as not_retrieved or abstract_only")
    p_retrieve.add_argument("--reason", help="required with --mark")
    p_retrieve.set_defaults(func=cmd_retrieve)

    p_fulltext = sub.add_parser("fulltext", help="assess retrieved reports")
    p_fulltext.add_argument("path")
    p_fulltext.add_argument("--offline", action="store_true",
                            help="use the built-in offline assessor")
    p_fulltext.add_argument("-y", "--yes", action="store_true",
                            help="skip the cost confirmation")
    p_fulltext.add_argument("--limit", type=int, default=20,
                            help="how many queued reports to list afterwards")
    p_fulltext.set_defaults(func=cmd_fulltext)

    p_extract = sub.add_parser("extract", help="extract data from included reports")
    p_extract.add_argument("path")
    p_extract.add_argument("--offline", action="store_true",
                           help="use the built-in offline extractor")
    p_extract.add_argument("--no-verify", action="store_true",
                           help="skip the blind direction check")
    p_extract.add_argument("--second-coder-sample", action="store_true",
                           help="also choose the reports for a second coder")
    p_extract.add_argument("-y", "--yes", action="store_true",
                           help="skip the cost confirmation")
    p_extract.add_argument("--limit", type=int, default=20)
    p_extract.set_defaults(func=cmd_extract)

    p_coders = sub.add_parser("coders", help="report inter-coder agreement")
    p_coders.add_argument("path")
    p_coders.set_defaults(func=cmd_coders)

    p_synth = sub.add_parser("synthesise", help="build the graded evidence inventory")
    p_synth.add_argument("path")
    p_synth.add_argument(
        "--step", choices=_SYNTHESIS_STEPS,
        help="run one step only (default: all of them, in order)",
    )
    p_synth.add_argument(
        "--accept-proposals", action="store_true",
        help="take the model's codebook, orientations and dataset links "
             "unread, logging each as accepted without review",
    )
    p_synth.add_argument("--offline", action="store_true")
    p_synth.add_argument("-y", "--yes", action="store_true",
                         help="skip the cost estimate")
    p_synth.set_defaults(func=cmd_synthesise)

    p_framework = sub.add_parser(
        "framework", help="build the themes, propositions and figures"
    )
    p_framework.add_argument("path")
    p_framework.add_argument("--step", choices=_FRAMEWORK_STEPS,
                             help="run one step only (default: all, in order)")
    p_framework.add_argument(
        "--accept-proposals", action="store_true",
        help="approve the model's themes unread, logging each as accepted "
             "without review",
    )
    p_framework.add_argument("--offline", action="store_true")
    p_framework.add_argument("-y", "--yes", action="store_true",
                             help="skip the cost estimate")
    p_framework.set_defaults(func=cmd_framework)

    p_gaps = sub.add_parser("gaps", help="find, rank and evidence the gaps")
    p_gaps.add_argument("path")
    p_gaps.add_argument(
        "--step", choices=_GAP_STEPS,
        help="run one step only (default: the full analysis, the study drafts "
             "and the map; the freshness check reaches the network and is "
             "never run unless asked for)",
    )
    p_gaps.add_argument("--top", type=int, default=None,
                        help="how many of the top-ranked gaps to work on")
    p_gaps.add_argument("--dimension", default="country",
                        help="what to cross the constructs against in the map")
    p_gaps.add_argument("--offline", action="store_true")
    p_gaps.add_argument("-y", "--yes", action="store_true",
                        help="skip the cost estimate")
    p_gaps.set_defaults(func=cmd_gaps)

    p_demo = sub.add_parser(
        "demo", help="build the bundled demonstration project (no API key)"
    )
    p_demo.add_argument("path", help="a new, empty folder for it")
    p_demo.add_argument(
        "--stage", choices=sorted(_DEMO_STAGES), default="gaps",
        help="how far to carry it (default: the whole pipeline)",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_export_all = sub.add_parser(
        "export-all",
        help="write everything the review produced, in one go",
    )
    p_export_all.add_argument("path")
    p_export_all.add_argument("--out", help="the folder to write into")
    p_export_all.set_defaults(func=cmd_export_all)

    p_export_gaps = sub.add_parser(
        "export-gaps",
        help="write the gap register, the research agenda and the draft",
    )
    p_export_gaps.add_argument("path")
    p_export_gaps.add_argument("--out", help="the folder to write them into")
    p_export_gaps.set_defaults(func=cmd_export_gaps)

    p_inventory = sub.add_parser(
        "export-inventory", help="write the Excel graded inventory"
    )
    p_inventory.add_argument("path")
    p_inventory.add_argument("out", nargs="?", help="where to write it")
    p_inventory.set_defaults(func=cmd_export_inventory)

    p_matrix = sub.add_parser("export-matrix", help="write the Excel extraction matrix")
    p_matrix.add_argument("path")
    p_matrix.add_argument("--out", help="where to write it")
    p_matrix.set_defaults(func=cmd_export_matrix)

    p_export = sub.add_parser("export-log", help="write the Excel screening log")
    p_export.add_argument("path")
    p_export.add_argument("--out", help="where to write it")
    p_export.set_defaults(func=cmd_export_log)

    p_status = sub.add_parser("status", help="show counts and audit health")
    p_status.add_argument("path")
    p_status.set_defaults(func=cmd_status)

    p_audit = sub.add_parser("audit", help="print the audit log")
    p_audit.add_argument("path")
    p_audit.add_argument("--stage")
    p_audit.add_argument("--limit", type=int, default=50)
    p_audit.set_defaults(func=cmd_audit)

    p_bench = sub.add_parser(
        "benchmark",
        help="score screening against a human-labelled dataset",
        description=(
            "Screen a labelled benchmark corpus and compare the result with "
            "the decisions the original review's human screeners made. The "
            "protocol is required and must come from that published review: "
            "the eligibility wording drives the result, so a harness that "
            "wrote its own would be marking its own homework."
        ),
    )
    p_bench.add_argument("dataset", help="a labelled CSV (SYNERGY format)")
    p_bench.add_argument(
        "--protocol",
        required=True,
        help="path to a text file holding the review's eligibility criteria",
    )
    p_bench.add_argument("--out", help="where to write the report")
    p_bench.add_argument(
        "--sample-irrelevant",
        type=int,
        help=(
            "screen every relevant record but only this many irrelevant "
            "ones. Keeps recall exact and the cost affordable."
        ),
    )
    p_bench.add_argument(
        "--label-column",
        help="override the gold-standard column (default: the "
        "title/abstract screening label)",
    )
    p_bench.add_argument("--seed", type=int, default=20260101)
    p_bench.add_argument(
        "--licence", default="", help="the dataset's licence, for the report"
    )
    p_bench.add_argument(
        "--reference", default="", help="DOI or URL of the review it came from"
    )
    p_bench.add_argument(
        "--provider",
        default="anthropic",
        help="anthropic (default), openai_compatible or ollama. The API key "
        "is the one saved in the application under Tools > Model settings.",
    )
    p_bench.add_argument(
        "--model",
        default="",
        help="the model to screen with; defaults to the provider's default. "
        "Name it explicitly for anything you will report.",
    )
    p_bench.add_argument(
        "--base-url", default="", help="override the provider's endpoint"
    )
    p_bench.add_argument(
        "--requests-per-minute",
        type=float,
        help="go no faster than this. For free API tiers, set it a little "
        "under the tier's per-minute limit.",
    )
    p_bench.add_argument(
        "--workspace",
        help="keep the run in this folder. If it stops (a daily quota, a "
        "budget), the same command with the same --workspace carries on "
        "where it left off without paying again for anything already answered.",
    )
    p_bench.add_argument(
        "--max-tokens",
        type=int,
        help="longest answer allowed per call (default 700). Raise it for "
        "models that think before answering, which spend tokens doing so.",
    )
    p_bench.add_argument(
        "--budget",
        type=float,
        help="stop the run once it has cost this many US dollars",
    )
    p_bench.add_argument(
        "--mock",
        action="store_true",
        help="run against the mock model: costs nothing, proves nothing "
        "about a real model, useful for checking the harness",
    )
    p_bench.add_argument(
        "--yes", action="store_true", help="skip the cost confirmation"
    )
    p_bench.set_defaults(func=cmd_benchmark)

    return parser


def cmd_benchmark(args) -> int:
    """Score screening against a labelled dataset and write the report."""
    import tempfile

    from .ai import AIClient, MockProvider
    from .ai.settings import ModelSettings, provider_from_settings
    from .evaluation import benchmark as bench
    from .evaluation.benchmark import BenchmarkCancelled
    from .evaluation.datasets import load_csv, stratified_sample
    from .screening.engine import ScreeningSettings

    run_benchmark = bench.run_benchmark
    from .evaluation.report import as_markdown

    protocol = Path(args.protocol).read_text(encoding="utf-8").strip()
    dataset = load_csv(
        args.dataset,
        label_column=args.label_column,
        licence=args.licence,
        reference=args.reference,
    )
    if args.sample_irrelevant is not None:
        dataset = stratified_sample(
            dataset, n_irrelevant=args.sample_irrelevant, seed=args.seed
        )

    facts = dataset.characteristics()
    print(f"Dataset:   {facts['name']}")
    print(f"Records:   {facts['n_records']} ({facts['n_relevant']} relevant)")
    print(f"Label:     {facts['label_column']}")
    print(
        f"No abstract: {facts['n_without_abstract']} "
        f"({facts['share_without_abstract']:.0%}) — screened on title alone"
    )

    # Two screeners per record, plus an adjudicator wherever they disagree.
    # The floor is exact; the ceiling assumes they never agree.
    low, high = 2 * facts["n_records"], 3 * facts["n_records"]
    print(f"\nThis will make roughly {low:,} to {high:,} model calls.")

    if args.mock:
        print("Running against the mock model: no network, no cost.\n")
        factory = lambda project: AIClient(  # noqa: E731
            project, MockProvider(), stage="screen", sleep=lambda _: None
        )
        confirm = None
    else:
        from .ai.keys import has_key
        from .ai.providers import PROVIDERS

        spec = PROVIDERS.get(args.provider)
        if spec is None:
            raise ValueError(
                f"Unknown provider {args.provider!r}. "
                f"Choose one of: {', '.join(sorted(PROVIDERS))}."
            )
        if spec.needs_key and not has_key(args.provider):
            raise ValueError(
                f"No API key is saved for {spec.label}. Open the application, "
                "choose Tools > Model settings, enter the key there (it is kept "
                "in the Windows Credential Manager), then run this again."
            )
        settings = ModelSettings(
            provider=args.provider, model=args.model, base_url=args.base_url
        )
        print(f"Model: {settings.resolved_model()} ({spec.label})")
        if spec.sends_data_off_device:
            print("Titles and abstracts will be sent to this provider.")

        def factory(project):
            provider = provider_from_settings(settings)
            if args.requests_per_minute:
                from .ai.throttle import ThrottledProvider

                provider = ThrottledProvider(provider, args.requests_per_minute)
            return AIClient(project, provider, stage="screen")

        if args.requests_per_minute:
            print(
                f"Pacing: at most {args.requests_per_minute:g} requests a minute."
            )

        def confirm(estimate) -> bool:
            print(f"\nEstimate: {estimate.describe()}")
            for note in estimate.notes:
                print(f"  {note}")
            if args.budget is not None:
                print(f"  The run stops if it reaches US${args.budget:,.2f}.")
            if args.yes:
                return True
            reply = input("Go ahead? [y/N] ").strip().lower()
            return reply in ("y", "yes")

    if args.workspace:
        workspace = Path(args.workspace)
        if (workspace / "project.slr").exists():
            print(f"Resuming the run kept in {workspace}.")
    else:
        workspace = Path(tempfile.mkdtemp(prefix="slr-benchmark-"))
    screening = ScreeningSettings(budget_usd=args.budget)
    if args.max_tokens:
        screening.max_tokens = args.max_tokens
    try:
        result = run_benchmark(
            dataset,
            protocol=protocol,
            client_factory=factory,
            workspace=workspace,
            settings=screening,
            confirm=confirm,
            on_progress=lambda done, total, msg: print(
                f"\r  {done}/{total}", end="", flush=True
            ),
        )
    except BenchmarkCancelled:
        print("Nothing was run.")
        return 1
    print()

    if not result.complete:
        # A partial run is not a result. Writing it out as a report invites
        # quoting a number that describes some of the corpus as all of it.
        print("\nThe run stopped before every record was screened:")
        for note in result.notes:
            print(f"  {note}")
        print(
            f"\nNo report was written. Run the same command again, with "
            f"--workspace {workspace}, to carry on where it stopped "
            "(for a daily quota, after the quota resets)."
        )
        return 3

    report = as_markdown(result)
    out = Path(args.out) if args.out else Path.cwd() / f"benchmark-{facts['name']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps(result.as_dict(), indent=2), encoding="utf-8"
    )

    s = result.score
    print()
    print(f"Workflow recall: {s.workflow_recall:.1%}" if s.workflow_recall is not None else "no relevant records")
    print(f"Records lost:    {s.n_lost} of {s.n_relevant}")
    print(f"Burden:          {s.n_to_human} of {s.n_records} records reach a human")
    print(f"\nReport: {out}")
    print(f"Data:   {out.with_suffix('.json')}")
    print(f"Project kept at: {workspace}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
