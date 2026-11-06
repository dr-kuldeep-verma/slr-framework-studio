"""Readers for reference-manager export formats.

Every reader returns :class:`~slr_studio.importers.base.ParsedRecord` objects
carrying both the normalised fields and the verbatim source block they came
from. Normalisation is lossy by design; the raw block is the thing the audit
trail points at.
"""

from .base import ParsedRecord, ParseResult
from .registry import detect_format, parse_file, SUPPORTED_SUFFIXES

__all__ = [
    "ParsedRecord",
    "ParseResult",
    "detect_format",
    "parse_file",
    "SUPPORTED_SUFFIXES",
]
