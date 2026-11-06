"""Reading PDFs, page by page.

Every passage the review ever quotes carries a page number, so extraction is
organised around pages rather than around one blob of text. A page with a text
layer is read directly; a page without one — a scan, or a figure-only page —
is rendered and passed to Tesseract.

The page numbers here are **positions in the file**, counted from 1. They are
not the numbers printed on the page, which often start over after front
matter. The interface says "page 7 of the PDF" for that reason: a reviewer
opening the file can find it, and nothing silently claims to know the
journal's own numbering.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "PdfDocument",
    "PdfPage",
    "extract_pdf",
    "normalise_for_match",
    "locate_quote_span",
    "find_quote",
    "QuoteLocation",
    "ocr_available",
    "PdfError",
]

#: Below this many characters a page is treated as having no usable text
#: layer. A scanned page often yields a stray ligature or a header artefact,
#: which is not text and would otherwise suppress OCR.
MIN_CHARS_FOR_TEXT_LAYER = 40

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
# Hyphenation at a line break: "partici-\npation" is one word.
_HYPHEN_BREAK_RE = re.compile(r"(\w)-\s*\n\s*(\w)")


class PdfError(RuntimeError):
    pass


def _pymupdf():
    try:
        import pymupdf  # PyMuPDF >= 1.24 exposes this name
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz  # the older name for the same library
        return fitz
    except ImportError as exc:
        raise PdfError(
            "PyMuPDF is required to read PDFs. The packaged build includes it; "
            "from source, install it with: pip install PyMuPDF"
        ) from exc


def ocr_available() -> bool:
    """Whether Tesseract can be reached.

    OCR is optional at runtime: a review of born-digital papers never needs
    it, and saying so beats failing on the one scanned thesis in the set.
    """
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


@dataclass
class PdfPage:
    page_no: int
    text: str
    ocr: bool = False

    @property
    def n_chars(self) -> int:
        return len(self.text)


@dataclass
class PdfDocument:
    path: Path
    pages: list[PdfPage] = field(default_factory=list)
    n_pages_ocr: int = 0
    has_text_layer: bool = False
    title_guess: str = ""
    doi_guess: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def n_pages(self) -> int:
        return len(self.pages)

    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    def page(self, page_no: int) -> PdfPage | None:
        for page in self.pages:
            if page.page_no == page_no:
                return page
        return None


_DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>,;]+)", re.IGNORECASE)


def _guess_doi(text: str) -> str:
    match = _DOI_IN_TEXT.search(text)
    if not match:
        return ""
    return match.group(1).rstrip(".,;:)]}>'\"").casefold()


def _guess_title(first_page_text: str, metadata_title: str = "") -> str:
    """Best guess at the article title, for matching a dropped file to a record.

    The PDF's own metadata title is often the publisher's template name
    ("untitled", "Microsoft Word - final.doc"), so it is used only when it
    looks like a real title. Otherwise the longest early line wins, which is
    usually the title set in large type at the top of page one.
    """
    candidate = (metadata_title or "").strip()
    rubbish = ("untitled", "microsoft word", "document", "print", ".doc", ".pdf", ".tex")
    if candidate and len(candidate) > 15 and not any(
        bad in candidate.casefold() for bad in rubbish
    ):
        return candidate

    lines = [line.strip() for line in first_page_text.splitlines()[:30]]
    lines = [line for line in lines if len(line) > 15]
    if not lines:
        return candidate
    skip = ("doi", "http", "www.", "@", "copyright", "©", "downloaded", "issn", "vol.")
    for line in lines:
        lowered = line.casefold()
        if any(token in lowered for token in skip):
            continue
        if sum(c.isdigit() for c in line) > len(line) / 3:
            continue
        return line
    return lines[0]


def _ocr_page(page: Any, dpi: int = 300) -> str:
    import io

    import pytesseract
    from PIL import Image

    pixmap = page.get_pixmap(dpi=dpi)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    return pytesseract.image_to_string(image)


def extract_pdf(
    path: str | Path,
    *,
    use_ocr: bool = True,
    ocr_dpi: int = 300,
    max_pages: int | None = None,
) -> PdfDocument:
    """Read a PDF into pages, falling back to OCR where there is no text.

    Raises :class:`PdfError` for a file that is not a readable PDF; a password
    on the file is reported as such rather than as a parse failure, because
    the two call for different things from the reviewer.
    """
    path = Path(path)
    if not path.is_file():
        raise PdfError(f"No file at {path}")
    pymupdf = _pymupdf()

    try:
        document = pymupdf.open(str(path))
    except Exception as exc:
        raise PdfError(f"{path.name} could not be opened as a PDF: {exc}") from exc

    result = PdfDocument(path=path)
    try:
        if getattr(document, "needs_pass", False):
            raise PdfError(
                f"{path.name} is password-protected. Save an unlocked copy and "
                "add that instead."
            )
        ocr_ready = use_ocr and ocr_available()
        limit = len(document) if max_pages is None else min(len(document), max_pages)

        for index in range(limit):
            page = document[index]
            try:
                text = page.get_text("text") or ""
            except Exception as exc:
                text = ""
                result.warnings.append(f"page {index + 1}: {exc}")
            used_ocr = False
            if len(text.strip()) < MIN_CHARS_FOR_TEXT_LAYER:
                if ocr_ready:
                    try:
                        ocr_text = _ocr_page(page, dpi=ocr_dpi)
                    except Exception as exc:
                        result.warnings.append(f"page {index + 1}: OCR failed ({exc})")
                        ocr_text = ""
                    if len(ocr_text.strip()) > len(text.strip()):
                        text, used_ocr = ocr_text, True
                elif use_ocr:
                    result.warnings.append(
                        f"page {index + 1} has no text layer and Tesseract is not "
                        "available, so it could not be read"
                    )
            if used_ocr:
                result.n_pages_ocr += 1
            else:
                result.has_text_layer = result.has_text_layer or bool(text.strip())
            result.pages.append(
                PdfPage(page_no=index + 1, text=text.strip(), ocr=used_ocr)
            )

        metadata = dict(document.metadata or {})
    finally:
        document.close()

    if result.pages:
        head = "\n".join(page.text for page in result.pages[:2])
        result.doi_guess = _guess_doi(head)
        result.title_guess = _guess_title(
            result.pages[0].text, str(metadata.get("title") or "")
        )
    if not any(page.text for page in result.pages):
        result.warnings.append(
            "No text could be read from this file at all. If it is a scan, "
            "OCR may not be installed."
        )
    return result


# -- quotation checking ----------------------------------------------------


def normalise_for_match(text: str) -> str:
    """The form two strings are compared in when checking a quotation.

    Line breaks, hyphenation across them, ligatures, curly quotes and
    punctuation all differ between how a PDF stores a sentence and how a model
    repeats it. Normalising those away stops honest quotations being called
    fabrications. Word order and the words themselves are untouched, so a
    quotation that is actually invented still fails.
    """
    if not text:
        return ""
    text = _HYPHEN_BREAK_RE.sub(r"\1\2", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[-‐-―]", " ", text)
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _normalised_with_map(text: str) -> tuple[str, list[int]]:
    """Normalise text, keeping where each character came from.

    The same transformations as :func:`normalise_for_match`, applied one
    character at a time so that a match in the normalised form can be pointed
    back at the original. Used to highlight a quotation on the page a reviewer
    is looking at, where the words almost always run across a line break.
    """
    out: list[str] = []
    origin: list[int] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        # Hyphen at a line break joins the word: drop both.
        if char in "-‐‑‒–—―":
            ahead = index + 1
            while ahead < length and text[ahead] in " \t":
                ahead += 1
            if ahead < length and text[ahead] == "\n":
                ahead += 1
                while ahead < length and text[ahead] in " \t":
                    ahead += 1
                if ahead < length and text[ahead].isalnum():
                    index = ahead
                    continue
            # An ordinary hyphen reads as a space.
            if out and out[-1] != " ":
                out.append(" ")
                origin.append(index)
            index += 1
            continue
        if char.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                origin.append(index)
            index += 1
            continue

        folded = unicodedata.normalize("NFKD", char)
        folded = "".join(c for c in folded if not unicodedata.combining(c))
        folded = folded.casefold()
        folded = folded.replace("’", "'").replace("‘", "'")
        folded = folded.replace("“", '"').replace("”", '"')
        folded = _PUNCT_RE.sub(" ", folded)
        for piece in folded:
            if piece == " ":
                if out and out[-1] != " ":
                    out.append(" ")
                    origin.append(index)
            else:
                out.append(piece)
                origin.append(index)
        index += 1

    # Trim a trailing space so the map lines up with normalise_for_match.
    while out and out[-1] == " ":
        out.pop()
        origin.pop()
    start = 0
    while start < len(out) and out[start] == " ":
        start += 1
    return "".join(out[start:]), origin[start:]


def locate_quote_span(text: str, quote: str) -> tuple[int, int] | None:
    """Where a quotation sits in the original text, ignoring layout.

    Returns character offsets into ``text``, or None when the words are not
    there. Highlighting uses this so the reviewer sees the quoted passage in
    place, line breaks, hyphenation and all.
    """
    needle = normalise_for_match(quote)
    if not needle:
        return None
    haystack, origin = _normalised_with_map(text)
    position = haystack.find(needle)
    if position == -1 or not origin:
        return None
    start = origin[position]
    last = min(position + len(needle) - 1, len(origin) - 1)
    return start, origin[last] + 1


@dataclass
class QuoteLocation:
    found: bool
    page_no: int | None = None
    on_cited_page: bool = False
    detail: str = ""


def find_quote(
    quote: str,
    pages: list[PdfPage] | list[tuple[int, str]],
    cited_page: int | None = None,
    *,
    min_words: int = 4,
) -> QuoteLocation:
    """Check a quotation against the extracted pages.

    Returns where the words actually are. The distinction that matters is
    between a quotation that is *somewhere else in the document* — a page
    number off by one, which a reviewer can correct — and one that is
    **nowhere**, which means the assessment rests on words the paper does not
    contain.
    """
    normalised = normalise_for_match(quote)
    if not normalised:
        return QuoteLocation(False, detail="no quotation was given")
    if len(normalised.split()) < min_words:
        return QuoteLocation(
            False,
            detail=f"the quotation is too short to check ({len(normalised.split())} words)",
        )

    items: list[tuple[int, str]] = [
        (page.page_no, page.text) if isinstance(page, PdfPage) else page
        for page in pages
    ]
    if not items:
        return QuoteLocation(False, detail="no text was extracted from this report")

    hits = [
        page_no
        for page_no, text in items
        if normalised in normalise_for_match(text)
    ]
    if not hits:
        return QuoteLocation(
            False, detail="these words do not appear anywhere in the report"
        )
    if cited_page is not None and cited_page in hits:
        return QuoteLocation(True, page_no=cited_page, on_cited_page=True)
    first = hits[0]
    if cited_page is None:
        return QuoteLocation(True, page_no=first, on_cited_page=False)
    return QuoteLocation(
        True,
        page_no=first,
        on_cited_page=False,
        detail=(
            f"the quotation is in the report, but on page {first}, "
            f"not page {cited_page} as cited"
        ),
    )
