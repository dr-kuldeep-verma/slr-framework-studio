"""One page per stage of the review."""

from .base import StagePage
from .dedup_page import DedupPage
from .extract_page import ExtractPage
from .fulltext_page import FullTextPage
from .import_page import ImportPage
from .placeholder import PlaceholderPage
from .export_page import ExportPage
from .framework_page import FrameworkPage
from .gap_finder_page import GapFinderPage
from .synthesise_page import SynthesisePage
from .project_page import ProjectPage
from .retrieve_page import RetrievePage
from .screen_page import ScreenPage

__all__ = [
    "StagePage",
    "ProjectPage",
    "ImportPage",
    "DedupPage",
    "ScreenPage",
    "RetrievePage",
    "ExtractPage",
    "FullTextPage",
    "PlaceholderPage",
    "ExportPage",
    "FrameworkPage",
    "GapFinderPage",
    "SynthesisePage",
]
