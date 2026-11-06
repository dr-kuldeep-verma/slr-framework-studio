"""Files the review hands to other people.

Everything here is generated from the database, never typed. If a number
appears in an export, the query that produced it is in this package and the
decisions behind it are in the audit log.
"""

from .bundle import BundleOutcome, SECTION_12, export_everything
from .documents import write_synthesis_tables
from .extraction_matrix import write_extraction_matrix
from .methods import ai_use_statement, write_audit_log, write_methods_draft
from .prisma import write_prisma_checklist, write_prisma_diagram
from .refi import validate_qdpx, write_refi_project
from .ris import write_ris
from .gap_register import (
    write_future_research_draft,
    write_gap_register,
    write_research_agenda,
)
from .graded_inventory import write_graded_inventory
from .screening_log import write_screening_log

__all__ = [
    "write_screening_log",
    "write_extraction_matrix",
    "write_graded_inventory",
    "write_gap_register",
    "write_research_agenda",
    "write_future_research_draft",
    "write_synthesis_tables",
    "write_prisma_diagram",
    "write_prisma_checklist",
    "write_ris",
    "write_refi_project",
    "validate_qdpx",
    "write_methods_draft",
    "write_audit_log",
    "ai_use_statement",
    "export_everything",
    "BundleOutcome",
    "SECTION_12",
]
