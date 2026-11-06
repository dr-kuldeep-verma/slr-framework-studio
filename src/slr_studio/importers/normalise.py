"""Field normalisation shared by every importer and by deduplication.

Two databases rarely agree on how to write the same reference. Scopus
upper-cases nothing, Web of Science upper-cases titles in some exports, EBSCO
leaves HTML entities in abstracts, and everyone disagrees about DOI prefixes.
These functions produce the comparable forms; the originals stay untouched in
`record.raw`.
"""

from __future__ import annotations

import html
import re
import unicodedata

__all__ = [
    "clean_text",
    "normalise_doi",
    "normalise_title",
    "title_key",
    "normalise_author",
    "first_author_key",
    "parse_year",
    "normalise_language",
    "split_keywords",
    "normalise_doc_type",
]

_TAG_RE = re.compile(r"<[^>]{1,40}>")
_WS_RE = re.compile(r"\s+")
_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
_DOI_TRAILING = ".,;:)]}>'\"­"
_YEAR_RE = re.compile(r"(1[6-9]\d{2}|20\d{2}|21\d{2})")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

#: Words dropped from the blocking key only. They stay in `title_norm`, which
#: is what the title+year rule compares, so dropping them can never merge two
#: genuinely different titles -- it only widens the net for candidate pairs.
_STOPWORDS = frozenset(
    """a an the of and or for in on at to from with without by as is are be
    been being this that these those its their his her our your""".split()
)


def clean_text(value: str | None) -> str:
    """Collapse whitespace, unescape entities and drop inline markup.

    Export files routinely carry `<sub>2</sub>`, `&amp;` and hard-wrapped
    lines. None of that should reach a screening prompt or a figure label.
    """
    if not value:
        return ""
    text = html.unescape(str(value))
    text = _TAG_RE.sub("", text)
    text = text.replace("­", "")  # soft hyphen
    text = _WS_RE.sub(" ", text)
    return text.strip()


def normalise_doi(value: str | None) -> str | None:
    """Return a bare lower-case DOI, or None if the string holds none.

    Accepts the many shapes exports use: `https://doi.org/10.x/y`,
    `doi:10.x/y`, `DOI 10.x/y`, or a DOI buried in a URL field.
    """
    if not value:
        return None
    text = html.unescape(str(value)).strip()
    match = _DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(1).rstrip(_DOI_TRAILING)
    # Some exports append a second URL or an access date after the DOI.
    doi = doi.split()[0]
    return doi.lower() or None


def normalise_title(value: str | None) -> str:
    """Case-folded, accent-stripped, punctuation-free form used for matching.

    `Résumé: A Study (Part II)` and `RESUME - a study, part ii` both become
    `resume a study part ii`, which is what makes the title+year rule work
    across databases that disagree about styling.
    """
    text = clean_text(value)
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ß", "ss").replace("æ", "ae").replace("ø", "o").replace("đ", "d")
    text = text.casefold()
    # Hyphens and slashes join words in one database and separate them in
    # another, so treat them as spaces rather than deleting them.
    text = re.sub(r"[-‐-―/_]", " ", text)
    text = _PUNCT_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def title_key(value: str | None, *, n_tokens: int = 4) -> str:
    """A coarse blocking key: the first few content words of the title.

    Deduplication compares only records that share a block, which keeps the
    fuzzy stage near-linear instead of comparing every pair in a 20,000-record
    import.
    """
    norm = normalise_title(value)
    if not norm:
        return ""
    tokens = [t for t in norm.split() if t not in _STOPWORDS and len(t) > 1]
    if not tokens:
        tokens = norm.split()
    return " ".join(tokens[:n_tokens])


def normalise_author(value: str | None) -> str:
    """Normalise one author name to `surname, i` form.

    Handles `Smith, John A.`, `Smith J.A.`, `John A. Smith` and `SMITH, J`.
    Initials beyond the first are dropped: databases disagree about middle
    initials far more often than they disagree about surnames.
    """
    text = clean_text(value)
    if not text:
        return ""
    text = text.replace(".", " ").replace("’", "'")
    text = _WS_RE.sub(" ", text).strip(" ,;")
    if not text:
        return ""
    if "," in text:
        surname, _, rest = text.partition(",")
    else:
        parts = text.split()
        if len(parts) == 1:
            surname, rest = parts[0], ""
        else:
            # Trailing token is the surname unless it looks like initials.
            if len(parts[-1]) <= 2 and parts[-1].isupper():
                surname, rest = parts[0], " ".join(parts[1:])
            else:
                surname, rest = parts[-1], " ".join(parts[:-1])
    surname = normalise_title(surname).replace(" ", "")
    initial = ""
    rest = rest.strip()
    if rest:
        first_token = normalise_title(rest).split()
        if first_token:
            initial = first_token[0][0]
    return f"{surname}, {initial}".strip(" ,") if surname else ""


def first_author_key(authors: list[str] | None) -> str:
    """Normalised surname of the first author, used as corroborating evidence."""
    if not authors:
        return ""
    normalised = normalise_author(authors[0])
    return normalised.split(",")[0].strip() if normalised else ""


def parse_year(*values: str | None) -> int | None:
    """Pull a four-digit year out of the first value that contains one.

    RIS year fields arrive as `2019`, `2019///`, `2019/05/12/`, `2019 Jun`,
    `c2019` and worse. The first plausible year wins.
    """
    for value in values:
        if value is None:
            continue
        match = _YEAR_RE.search(str(value))
        if match:
            year = int(match.group(1))
            if 1500 <= year <= 2200:
                return year
    return None


_LANGUAGE_MAP = {
    "en": "English", "eng": "English", "english": "English",
    "de": "German", "ger": "German", "deu": "German", "german": "German",
    "fr": "French", "fre": "French", "fra": "French", "french": "French",
    "es": "Spanish", "spa": "Spanish", "spanish": "Spanish",
    "pt": "Portuguese", "por": "Portuguese", "portuguese": "Portuguese",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese", "chinese": "Chinese",
    "ja": "Japanese", "jpn": "Japanese", "japanese": "Japanese",
    "it": "Italian", "ita": "Italian", "italian": "Italian",
    "nl": "Dutch", "dut": "Dutch", "nld": "Dutch", "dutch": "Dutch",
    "ru": "Russian", "rus": "Russian", "russian": "Russian",
    "hi": "Hindi", "hin": "Hindi", "hindi": "Hindi",
}


def normalise_language(value: str | None) -> str:
    """Map a language code or name to a consistent English name."""
    text = clean_text(value)
    if not text:
        return ""
    key = text.strip().strip(".").casefold()
    if key in _LANGUAGE_MAP:
        return _LANGUAGE_MAP[key]
    # `eng; fre` and `English (United States)` both appear in the wild.
    first = re.split(r"[;,(]", key)[0].strip()
    return _LANGUAGE_MAP.get(first, text)


_DOC_TYPE_MAP = {
    "jour": "Journal Article",
    "journal article": "Journal Article",
    "article": "Journal Article",
    "article in press": "Journal Article",
    "ejour": "Journal Article",
    "conf": "Conference Paper",
    "cpaper": "Conference Paper",
    "conference paper": "Conference Paper",
    "conference proceeding": "Conference Paper",
    "proceedings paper": "Conference Paper",
    "book": "Book",
    "chap": "Book Chapter",
    "book chapter": "Book Chapter",
    "thes": "Thesis",
    "dissertation": "Thesis",
    "thesis": "Thesis",
    "book section": "Book Chapter",
    "chapter": "Book Chapter",
    "rprt": "Report",
    "report": "Report",
    "review": "Review",
    "editorial material": "Editorial",
    "editorial": "Editorial",
    "note": "Note",
    "letter": "Letter",
    "erratum": "Erratum",
    "correction": "Erratum",
}


def normalise_doc_type(value: str | None) -> str:
    """Map an RIS type tag or a database's own label to a common vocabulary."""
    text = clean_text(value)
    if not text:
        return ""
    key = text.casefold().strip()
    if key in _DOC_TYPE_MAP:
        return _DOC_TYPE_MAP[key]
    first = re.split(r"[;,]", key)[0].strip()
    if first in _DOC_TYPE_MAP:
        return _DOC_TYPE_MAP[first]
    # Databases qualify these endlessly: "Ph.D. dissertation", "Masters
    # thesis", "Proceedings paper (conference)". Match on the head noun.
    for marker, label in (
        ("dissertation", "Thesis"),
        ("thesis", "Thesis"),
        ("conference", "Conference Paper"),
        ("proceedings", "Conference Paper"),
        ("book section", "Book Chapter"),
        ("book chapter", "Book Chapter"),
        ("review", "Review"),
        ("editorial", "Editorial"),
        ("erratum", "Erratum"),
        ("correction", "Erratum"),
    ):
        if marker in key:
            return label
    return text


def split_keywords(values: list[str] | str | None) -> list[str]:
    """Flatten keyword fields into a de-duplicated, order-preserving list.

    Some databases emit one `KW` line per keyword; others pack them all onto
    one line separated by semicolons.
    """
    if not values:
        return []
    items = [values] if isinstance(values, str) else list(values)
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        for part in re.split(r"[;•]|(?:,\s)(?=[A-Z])", str(item)):
            keyword = clean_text(part).strip(" ,;.")
            if not keyword:
                continue
            fold = keyword.casefold()
            if fold not in seen:
                seen.add(fold)
                out.append(keyword)
    return out
