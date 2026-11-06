"""Full-text retrieval and eligibility.

Retrieval gathers links and takes in the PDFs the reviewer saves; eligibility
reads each report against the protocol's ordered exclusion codes and must
point at the page its answer rests on. The quotation check is what separates
an assessment from an assertion.
"""

from .engine import EligibilityEngine, EligibilitySettings, FullTextOutcome
from .pdf import PdfDocument, PdfError, PdfPage, extract_pdf, find_quote, ocr_available
from .retrieval import AttachOutcome, MatchResult, RetrievalError, RetrievalService
from .unpaywall import OaLocation, OaResult, UnpaywallClient, UnpaywallError

__all__ = [
    "EligibilityEngine",
    "EligibilitySettings",
    "FullTextOutcome",
    "RetrievalService",
    "MatchResult",
    "AttachOutcome",
    "RetrievalError",
    "UnpaywallClient",
    "OaResult",
    "OaLocation",
    "UnpaywallError",
    "extract_pdf",
    "find_quote",
    "ocr_available",
    "PdfDocument",
    "PdfPage",
    "PdfError",
]
