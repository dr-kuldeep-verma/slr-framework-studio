"""SLR Framework Studio.

A PRISMA 2020 systematic-review workbench that takes a researcher from
database exports to an evidence-graded theoretical framework.

Design rule for the whole package: AI proposes, a human disposes, and every
decision of either kind is written to an append-only audit log.
"""

__version__ = "0.1.0"
__app_name__ = "SLR Framework Studio"
__author__ = "Kuldeep Verma"
__source_url__ = "https://github.com/dr-kuldeep-verma/slr-framework-studio"

#: SPDX identifier. Named here as well as in `pyproject.toml` so that the
#: About dialog, which is this program's "Appropriate Legal Notices" under
#: section 5(d) of the licence, cannot drift away from what the project
#: actually ships under. A test asserts the two agree.
#:
#: AGPL rather than something permissive because PyMuPDF, which reads every
#: PDF, is AGPL-or-commercial from Artifex, and a distributed work that
#: bundles it must be AGPL too. THIRD-PARTY-NOTICES.md says what that does
#: and does not require of the people who use this.
__license__ = "AGPL-3.0-or-later"

#: Bumped whenever the SQLite schema changes; see slr_studio.core.db.
#: v1 protocol, import, deduplication. v2 adds screening. v3 adds full-text
#: retrieval and eligibility. v4 adds data extraction. v5 adds the Gap
#: Finder's groundwork: the gap register, author-stated future-research
#: statements, and a keyword index that survives deduplication. v6 adds the
#: synthesis engine: the construct codebook, label mappings, datasets, and
#: the harmonised relationships and graded inventory built from them. v7 adds
#: the framework builder: themes, propositions with their grounding links, the
#: three kinds of framework element kept apart, and the figures drawn from
#: them. v8 adds the Gap Finder's own tables and the search date every
#: claim of absence is qualified against. v9 lets a figure be a PDF.
#: v10 records whether the framework is built bottom-up or top-down and
#: the constructs a top-down review declares in advance. v11 adds
#: reviewer-defined question frameworks with their stages, and the
#: theory those a priori constructs are drawn from.
SCHEMA_VERSION = 11

#: The ordered workflow. The UI navigator and the audit log both use these keys.
STAGES = (
    ("project", "Project"),
    ("import", "Import"),
    ("dedup", "Deduplicate"),
    ("screen", "Screen"),
    ("retrieve", "Retrieve"),
    ("fulltext", "Full-text"),
    ("extract", "Extract"),
    ("synthesise", "Synthesise"),
    ("framework", "Framework"),
    ("gaps", "Gap Finder"),
    ("export", "Export"),
)
