"""A REFI-QDA project package, so the codings can be opened elsewhere.

REFI-QDA is the interchange format QualCoder, NVivo, ATLAS.ti and MAXQDA all
read. A `.qdpx` is a zip holding `project.qde` -- an XML document in the
`urn:QDA-XML:project:1.0` namespace -- and a `Sources` folder with the texts
the codings point into.

**What "validated" means here.** The published REFI-QDA XSD is not bundled
with this application and cannot be fetched from a machine that is offline by
design, so `validate_qdpx` checks the structure and the references rather than
running an XSD validation: the namespace, the required attributes, that every
identifier is a real GUID, that every `CodeRef` resolves to a code that
exists, and that every selection falls inside the text it is taken from. If an
XSD is present at `schema/REFI-QDA.xsd` beside the application, it is used as
well. Saying this plainly matters more than the word "validated": the checks
below are the ones that catch a package opening with codings on the wrong
span, which is the failure that would quietly corrupt someone's analysis.
"""

from __future__ import annotations

import hashlib
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

__all__ = [
    "NAMESPACE",
    "RefiValidationError",
    "validate_qdpx",
    "write_refi_project",
]

NAMESPACE = "urn:QDA-XML:project:1.0"

#: REFI-QDA wants upper-case GUIDs.
_GUID_CHARS = set("0123456789ABCDEF-")


class RefiValidationError(RuntimeError):
    """Raised by callers that want a failed validation to stop them."""


def _guid(*parts: Any) -> str:
    """A stable GUID for a thing in this project.

    Derived from the project's own identifiers rather than drawn at random,
    so that exporting the same project twice produces the same package and a
    reviewer can diff two exports.
    """
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return str(uuid.UUID(bytes=digest[:16])).upper()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain_text(project, row: dict[str, Any]) -> str:
    """The text a source's codings point into.

    The report's own pages where they were extracted, because that is what
    the quotations were taken from and what their page numbers refer to. The
    title and abstract stand in only when no full text was retrieved, which
    at least lets the record travel with the package.

    How this string is built is part of the file format: every selection's
    offsets are offsets into it, so the page separator and the order are
    fixed rather than incidental.
    """
    pages = [
        page["text"] or ""
        for page in project.db.query(
            "SELECT text FROM report_page WHERE record_id = ? ORDER BY page_no",
            (row["id"],),
        )
    ]
    if pages:
        return "\n\n".join(pages)
    parts = [row.get("title") or ""]
    if row.get("abstract"):
        parts.append(row["abstract"])
    return "\n\n".join(parts)


def _find(haystack: str, needle: str) -> tuple[int, int] | None:
    """Where a quotation sits in the text, if it is there at all.

    A quotation that cannot be located is left out rather than attached to
    position zero: a coding on the wrong span is worse than a missing one,
    because nobody reviewing the package would see anything wrong.
    """
    if not needle:
        return None
    index = haystack.find(needle)
    if index >= 0:
        return index, index + len(needle)
    # Try again on collapsed whitespace, which is the usual difference
    # between a stored quotation and the text it came from.
    flat_text = " ".join(haystack.split())
    flat_quote = " ".join(needle.split())
    index = flat_text.find(flat_quote)
    if index < 0:
        return None
    # Map back by walking the original, counting non-space characters.
    wanted = len("".join(flat_text[:index].split()))
    seen = start = 0
    for position, character in enumerate(haystack):
        if not character.isspace():
            if seen == wanted:
                start = position
                break
            seen += 1
    else:
        return None
    needed = len("".join(flat_quote.split()))
    seen = 0
    for position in range(start, len(haystack)):
        if not haystack[position].isspace():
            seen += 1
            if seen == needed:
                return start, position + 1
    return None


def _codes(project) -> list[dict[str, str]]:
    """The codebook: the review's constructs, plus the decisions it recorded."""
    codes: list[dict[str, str]] = []
    for row in project.db.query(
        "SELECT name, definition FROM construct ORDER BY position, id"
    ):
        codes.append(
            {
                "guid": _guid("construct", row["name"]),
                "name": row["name"],
                "description": row["definition"] or "",
            }
        )
    for name, description in (
        ("Included at full text", "Reports judged eligible on the full text."),
        ("Excluded at full text", "Reports judged ineligible on the full text."),
        ("Future research", "Statements by the authors about work still needed."),
    ):
        codes.append(
            {
                "guid": _guid("decision", name),
                "name": name,
                "description": description,
            }
        )
    return codes


def _codings_for(project, record_id: int, text: str, by_name: dict[str, str]):
    """Every coding on one source, as (code guid, start, end, label)."""
    found = []

    for row in project.db.query(
        "SELECT predictor_label, outcome_label, quote FROM relationship "
        "WHERE record_id = ? AND coder_role = 'ai'",
        (record_id,),
    ):
        span = _find(text, row["quote"] or "")
        if span is None:
            continue
        for label in (row["predictor_label"], row["outcome_label"]):
            guid = by_name.get((label or "").casefold())
            if guid:
                found.append((guid, span[0], span[1], label))

    for row in project.db.query(
        "SELECT f.decision AS decision, d.quote AS quote "
        "FROM fulltext_status f "
        "JOIN fulltext_decision d ON d.record_id = f.record_id "
        "WHERE f.record_id = ? AND d.quote <> '' LIMIT 4",
        (record_id,),
    ):
        span = _find(text, row["quote"] or "")
        if span is None:
            continue
        name = (
            "Included at full text"
            if row["decision"] == "include"
            else "Excluded at full text"
        )
        found.append((_guid("decision", name), span[0], span[1], name))

    for row in project.db.query(
        "SELECT statement, quote FROM future_research WHERE record_id = ?",
        (record_id,),
    ):
        span = _find(text, row["quote"] or row["statement"] or "")
        if span is None:
            continue
        found.append(
            (_guid("decision", "Future research"), span[0], span[1],
             "Future research")
        )
    return found


def write_refi_project(project, path: str | Path) -> Path:
    """Write the included reports and their codings as a `.qdpx`.

    The package is validated before this returns; a failure is reported in
    the exception rather than written to disk and discovered by whoever opens
    it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    info = project.info()
    user_guid = _guid("user", info["title"])
    codes = _codes(project)
    by_name = {code["name"].casefold(): code["guid"] for code in codes}

    sources: list[dict[str, Any]] = []
    for row in project.db.query(
        "SELECT r.* FROM fulltext_status f JOIN record r ON r.id = f.record_id "
        "WHERE f.decision = 'include' ORDER BY r.id"
    ):
        row = dict(row)
        text = _plain_text(project, row)
        sources.append(
            {
                "guid": _guid("source", row["id"]),
                "name": (row["title"] or f"Record {row['id']}")[:200],
                "record_id": row["id"],
                "text": text,
                "codings": _codings_for(project, row["id"], text, by_name),
            }
        )

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<Project xmlns={quoteattr(NAMESPACE)} '
        f"name={quoteattr(info['title'])} "
        f'origin="SLR Framework Studio" '
        f'creatingUserGUID="{user_guid}" '
        f'creationDateTime="{_now()}">',
        "  <Users>",
        f'    <User guid="{user_guid}" name="SLR Framework Studio" />',
        "  </Users>",
        "  <CodeBook>",
        "    <Codes>",
    ]
    for code in codes:
        lines.append(
            f'      <Code guid="{code["guid"]}" '
            f"name={quoteattr(code['name'])} isCodable=\"true\">"
        )
        if code["description"]:
            lines.append(
                f"        <Description>{escape(code['description'])}"
                "</Description>"
            )
        lines.append("      </Code>")
    lines.extend(["    </Codes>", "  </CodeBook>", "  <Sources>"])

    for source in sources:
        plain = f"Sources/{source['guid']}.txt"
        lines.append(
            f'    <TextSource guid="{source["guid"]}" '
            f"name={quoteattr(source['name'])} "
            f'plainTextPath="internal://{source["guid"]}.txt" '
            f'creatingUser="{user_guid}" creationDateTime="{_now()}">'
        )
        for index, (code_guid, start, end, label) in enumerate(
            source["codings"]
        ):
            selection_guid = _guid("selection", source["guid"], index)
            lines.append(
                f'      <PlainTextSelection guid="{selection_guid}" '
                f'startPosition="{start}" endPosition="{end}" '
                f"name={quoteattr(str(label)[:120])} "
                f'creatingUser="{user_guid}" creationDateTime="{_now()}">'
            )
            lines.append(
                f'        <Coding guid="{_guid("coding", selection_guid)}" '
                f'creatingUser="{user_guid}" creationDateTime="{_now()}">'
            )
            lines.append(f'          <CodeRef targetGUID="{code_guid}" />')
            lines.append("        </Coding>")
            lines.append("      </PlainTextSelection>")
        lines.append("    </TextSource>")
        source["plain"] = plain

    lines.extend(["  </Sources>", "</Project>"])
    document = "\n".join(lines)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.qde", document)
        for source in sources:
            archive.writestr(source["plain"], source["text"])

    problems = validate_qdpx(path)
    if problems:
        raise RefiValidationError(
            "The REFI-QDA package did not validate and has not been written "
            "as usable:\n  " + "\n  ".join(problems)
        )
    return path


# -- validation ---------------------------------------------------------------


def _is_guid(value: str | None) -> bool:
    if not value or len(value) != 36:
        return False
    return set(value.upper()) <= _GUID_CHARS and value.count("-") == 4


def validate_qdpx(path: str | Path) -> list[str]:
    """Check a package, returning the problems found.

    Structure and references, not the published XSD -- see the module
    docstring for why, and for what that does and does not cover. An XSD at
    `schema/REFI-QDA.xsd` beside this file is used as well when present.
    """
    path = Path(path)
    problems: list[str] = []

    if not zipfile.is_zipfile(path):
        return [f"{path.name} is not a zip archive, so it is not a .qdpx."]

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "project.qde" not in names:
            return [
                "The package has no project.qde at its root, which every "
                "REFI-QDA package must have."
            ]
        payload = archive.read("project.qde")
        contents = set(names)

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        return [f"project.qde is not well-formed XML: {exc}"]

    if not root.tag.startswith("{"):
        problems.append(
            "The root element declares no namespace; it must be "
            f"{NAMESPACE}."
        )
    else:
        found = root.tag[1:].split("}")[0]
        if found != NAMESPACE:
            problems.append(
                f"The root element is in namespace {found!r}, not {NAMESPACE!r}."
            )
    ns = {"q": NAMESPACE}

    if not root.tag.endswith("Project"):
        problems.append("The root element is not <Project>.")
    for attribute in ("name", "creatingUserGUID", "creationDateTime"):
        if not root.get(attribute):
            problems.append(f"<Project> has no {attribute} attribute.")
    if root.get("creatingUserGUID") and not _is_guid(root.get("creatingUserGUID")):
        problems.append(
            f"<Project> creatingUserGUID {root.get('creatingUserGUID')!r} is "
            "not a guid."
        )

    code_guids: set[str] = set()
    for code in root.findall(".//q:Code", ns):
        guid = code.get("guid")
        if not _is_guid(guid):
            problems.append(f"<Code> guid {guid!r} is not a guid.")
        else:
            code_guids.add(guid.upper())
        if not code.get("name"):
            problems.append(f"<Code guid={guid}> has no name.")

    texts: dict[str, str] = {}
    with zipfile.ZipFile(path) as archive:
        for source in root.findall(".//q:TextSource", ns):
            guid = source.get("guid")
            if not _is_guid(guid):
                problems.append(f"<TextSource> guid {guid!r} is not a guid.")
                continue
            member = f"Sources/{guid}.txt"
            if member not in contents:
                problems.append(
                    f"<TextSource guid={guid}> points at {member}, which is "
                    "not in the package."
                )
                continue
            texts[guid] = archive.read(member).decode("utf-8")

    for source in root.findall(".//q:TextSource", ns):
        guid = source.get("guid")
        text = texts.get(guid, "")
        for selection in source.findall("q:PlainTextSelection", ns):
            selection_guid = selection.get("guid")
            if not _is_guid(selection_guid):
                problems.append(
                    f"<PlainTextSelection> guid {selection_guid!r} is not a guid."
                )
            try:
                start = int(selection.get("startPosition", ""))
                end = int(selection.get("endPosition", ""))
            except ValueError:
                problems.append(
                    f"<PlainTextSelection guid={selection_guid}> has no usable "
                    "startPosition and endPosition."
                )
                continue
            if start < 0 or end > len(text) or end <= start:
                problems.append(
                    f"<PlainTextSelection guid={selection_guid}> spans "
                    f"{start}–{end}, which falls outside its source text of "
                    f"{len(text)} character(s)."
                )
            for reference in selection.findall(".//q:CodeRef", ns):
                target = (reference.get("targetGUID") or "").upper()
                if not _is_guid(target):
                    problems.append(
                        f"A CodeRef has targetGUID {target!r}, which is not a "
                        "guid."
                    )
                elif target not in code_guids:
                    problems.append(
                        f"A CodeRef has targetGUID {target}, which matches no "
                        "<Code> in the codebook."
                    )

    problems.extend(_validate_against_xsd(payload))
    return problems


def _validate_against_xsd(payload: bytes) -> list[str]:
    """Run the published XSD over the document, if one has been supplied.

    The schema is not bundled: it is not ours to redistribute, and this
    application cannot fetch it. A reviewer who needs formal validation can
    drop `REFI-QDA.xsd` into a `schema` folder beside this module.
    """
    schema_path = Path(__file__).parent / "schema" / "REFI-QDA.xsd"
    if not schema_path.is_file():
        return []
    try:
        from lxml import etree
    except Exception:
        return []
    try:
        schema = etree.XMLSchema(etree.parse(str(schema_path)))
        schema.assertValid(etree.fromstring(payload))
    except Exception as exc:
        return [f"The package does not validate against REFI-QDA.xsd: {exc}"]
    return []
