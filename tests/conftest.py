from __future__ import annotations

from pathlib import Path

import pytest

from slr_studio.core.project import Project

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


def migrations_from(version: int) -> list[int]:
    """Every migration a project at `version` should run to catch up.

    Tests say this rather than pinning a literal list, which went stale at
    every stage and taught nothing when it did: the claim worth making is
    that an old project is brought all the way forward, not that the current
    schema happens to be version six.
    """
    from slr_studio import SCHEMA_VERSION

    return list(range(version + 1, SCHEMA_VERSION + 1))


@pytest.fixture
def project(tmp_path: Path):
    """An empty project in a temporary folder, closed afterwards."""
    proj = Project.create(
        tmp_path / "review",
        "Test review",
        actor="human:test",
        contact_email="tester@example.org",
    )
    yield proj
    proj.close()


@pytest.fixture
def ready_to_screen(imported_project):
    """A deduplicated project with a locked protocol, plus an engine factory."""
    from slr_studio.ai import AIClient, MockProvider
    from slr_studio.dedup import DedupEngine
    from slr_studio.screening import ScreeningEngine, ScreeningSettings

    DedupEngine(imported_project).run()
    imported_project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    imported_project.protocol.lock(actor="human:test")

    def make_engine(**settings):
        client = AIClient(
            imported_project, MockProvider(), stage="screen", sleep=lambda _: None
        )
        return ScreeningEngine(imported_project, client, ScreeningSettings(**settings))

    return imported_project, make_engine


@pytest.fixture
def imported_project(project):
    """A project with the Scopus and Web of Science fixtures imported."""
    from slr_studio.importers.service import ImportService

    service = ImportService(project)
    service.import_file(FIXTURES / "scopus_sample.ris")
    service.import_file(FIXTURES / "wos_sample.ris")
    return project
