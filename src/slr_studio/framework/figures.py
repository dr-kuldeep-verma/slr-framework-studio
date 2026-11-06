"""Drawing the figures: SVG to look at, PNG to paste, PPTX to edit.

The same layout and the same styles go into all three, so the PowerPoint file
is the figure rather than a picture of it. That is the point of the spec's
"editable in PowerPoint": a reviewer who wants to move one box should be able
to, without redrawing the rest or asking this application to do it.

The PowerPoint file is therefore written as native shapes -- text boxes and
connectors -- and not as an embedded image. The PNG is an image, which is what
a word processor wants.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

from ..core.db import utc_now
from .layout import Edge, Node, count_crossings, layout_graph
from .style import arrow_label, style_for

__all__ = ["write_figures", "to_svg", "write_pdf"]

#: Point size of the text in a box and on an arrow.
_BOX_FONT = 10.0
_ARROW_FONT = 8.5

#: How the three kinds of element are told apart in the integrated diagram.
#: Evidenced links carry their grade's style; the other two are always drawn
#: as what they are, whatever else is on the page.
_ELEMENT_STYLE = {
    "proposed_mechanism": {"dash": (2.0, 2.0), "colour": "#2f6f4f", "width": 1.4},
    "untested_pathway": {"dash": (1.0, 3.0), "colour": "#8a6d3b", "width": 1.2},
}


def _wrap(label: str, width: float) -> list[str]:
    """Break a label to fit a box, at most three lines."""
    limit = max(8, int((width - 12) / (_BOX_FONT * 0.55)))
    words, lines, current = label.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][: max(0, limit - 1)] + "…"
    return lines


def _longest_segment(points: Sequence[tuple[float, float]]):
    """The segment a label should sit on: the longest one.

    The midpoint of a whole polyline often lands on a corner, where the text
    collides with both segments and reads as belonging to neither.
    """
    best = (points[0], points[1])
    best_length = -1.0
    for start, end in zip(points, points[1:]):
        length = abs(end[0] - start[0]) + abs(end[1] - start[1])
        if length > best_length:
            best, best_length = (start, end), length
    (x0, y0), (x1, y1) = best
    return ((x0 + x1) / 2, (y0 + y1) / 2), abs(x1 - x0) >= abs(y1 - y0)


def to_svg(layout, drawn: dict[str, dict[str, Any]], title: str = "") -> str:
    """The figure as an SVG document."""
    head = 26.0 if title else 0.0
    width, height = layout.width, layout.height + head
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<defs>",
    ]
    # One marker per colour, because an SVG marker cannot inherit the stroke.
    for colour in sorted({d["style"].colour for d in drawn.values()}):
        ident = colour.lstrip("#")
        parts.append(
            f'<marker id="a{ident}" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
        )
    parts.append("</defs>")
    if title:
        parts.append(
            f'<text x="{width / 2:.1f}" y="18" font-family="Helvetica" '
            f'font-size="12" font-weight="bold" text-anchor="middle" '
            f'fill="#1a1a1a">{escape(title)}</text>'
        )

    for key, detail in drawn.items():
        points = layout.edges[key]
        style = detail["style"]
        ident = style.colour.lstrip("#")
        path = " ".join(
            ("M" if i == 0 else "L") + f"{x:.1f},{y + head:.1f}"
            for i, (x, y) in enumerate(points)
        )
        dash = (
            f' stroke-dasharray="{",".join(str(v) for v in style.dash)}"'
            if style.dash
            else ""
        )
        heads = f' marker-end="url(#a{ident})"'
        if style.heads == 2:
            heads += f' marker-start="url(#a{ident})"'
        parts.append(
            f'<path d="{path}" fill="none" stroke="{style.colour}" '
            f'stroke-width="{style.width}"{dash}{heads}/>'
        )
        label = detail.get("label") or ""
        if label:
            (lx, ly), horizontal = _longest_segment(points)
            offset = -5 if horizontal else 0
            parts.append(
                f'<text x="{lx:.1f}" y="{ly + head + offset:.1f}" '
                f'font-family="Helvetica" font-size="{_ARROW_FONT}" '
                f'fill="#55606e" text-anchor="middle" '
                f'paint-order="stroke" stroke="#ffffff" stroke-width="3">'
                f"{escape(label)}</text>"
            )

    for key, (x, y, w, h) in layout.nodes.items():
        parts.append(
            f'<rect x="{x:.1f}" y="{y + head:.1f}" width="{w:.1f}" '
            f'height="{h:.1f}" rx="4" fill="#f4f6f8" stroke="#8a93a0" '
            'stroke-width="1"/>'
        )
        lines = _wrap(key, w)
        for index, line in enumerate(lines):
            text_y = (
                y + head + h / 2 + 3.5
                + (index - (len(lines) - 1) / 2) * (_BOX_FONT + 1.5)
            )
            parts.append(
                f'<text x="{x + w / 2:.1f}" y="{text_y:.1f}" '
                f'font-family="Helvetica" font-size="{_BOX_FONT}" '
                f'text-anchor="middle" fill="#1a1a1a">{escape(line)}</text>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def _font(size: int):
    """A readable font, falling back to Pillow's built-in one."""
    from PIL import ImageFont

    for name in (
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "Helvetica.ttf",
        "Arial.ttf",
        "arial.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow older than 9.2
        return ImageFont.load_default()


def _dashed(draw, start, end, *, fill, width: int, dash: tuple[float, ...]) -> None:
    """A dashed line, walked by hand because Pillow draws only solid ones."""
    import math

    if not dash:
        draw.line([start, end], fill=fill, width=width)
        return
    (x0, y0), (x1, y1) = start, end
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 1e-6:
        return
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    position, index = 0.0, 0
    while position < length:
        run = dash[index % len(dash)]
        nxt = min(length, position + run)
        if index % 2 == 0:
            draw.line(
                [
                    (x0 + ux * position, y0 + uy * position),
                    (x0 + ux * nxt, y0 + uy * nxt),
                ],
                fill=fill,
                width=width,
            )
        position, index = nxt, index + 1


def _arrowhead(draw, start, end, *, fill, size: float) -> None:
    import math

    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    spread = math.radians(24)
    draw.polygon(
        [
            end,
            (
                end[0] - size * math.cos(angle - spread),
                end[1] - size * math.sin(angle - spread),
            ),
            (
                end[0] - size * math.cos(angle + spread),
                end[1] - size * math.sin(angle + spread),
            ),
        ],
        fill=fill,
    )


def _to_png(layout, drawn: dict[str, dict[str, Any]], path: Path, title: str) -> bool:
    """Draw the figure as a bitmap, for pasting into a word processor.

    Drawn directly rather than rasterised from the SVG: the obvious
    rasteriser is Qt's, and Qt will not render without a running application,
    which a scheduled or headless run does not have. Returns False rather
    than raising if Pillow is missing, because a missing PNG costs the
    reviewer one format out of three.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False

    scale = 2                      # legible when printed
    head = 26.0 if title else 0.0
    width = int(layout.width * scale)
    height = int((layout.height + head) * scale)
    if width <= 0 or height <= 0:
        return False

    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)

    def point(xy):
        return (xy[0] * scale, (xy[1] + head) * scale)

    if title:
        font = _font(int(12 * scale))
        draw.text(
            (width / 2, 6 * scale), title, fill="#1a1a1a", font=font, anchor="ma"
        )

    for key, detail in drawn.items():
        points = [point(p) for p in layout.edges[key]]
        style = detail["style"]
        stroke = max(1, round(style.width * scale * 0.8))
        dash = tuple(v * scale for v in style.dash)
        for start, end in zip(points, points[1:]):
            _dashed(draw, start, end, fill=style.colour, width=stroke, dash=dash)
        _arrowhead(
            draw, points[-2], points[-1], fill=style.colour, size=7 * scale
        )
        if style.heads == 2:
            _arrowhead(
                draw, points[1], points[0], fill=style.colour, size=7 * scale
            )
        label = detail.get("label") or ""
        if label:
            (lx, ly), horizontal = _longest_segment(layout.edges[key])
            at = point((lx, ly))
            font = _font(int(_ARROW_FONT * scale))
            draw.text(
                (at[0], at[1] - (5 * scale if horizontal else 0)),
                label,
                fill="#55606e",
                font=font,
                anchor="md",
                stroke_width=max(1, round(scale)),
                stroke_fill="#ffffff",
            )

    font = _font(int(_BOX_FONT * scale))
    for key, (x, y, w, h) in layout.nodes.items():
        box = [
            (x * scale, (y + head) * scale),
            ((x + w) * scale, (y + h + head) * scale),
        ]
        draw.rounded_rectangle(
            box, radius=4 * scale, fill="#f4f6f8", outline="#8a93a0",
            width=max(1, round(scale)),
        )
        lines = _wrap(key, w)
        for index, line in enumerate(lines):
            text_y = (
                (y + head + h / 2) * scale
                + (index - (len(lines) - 1) / 2) * (_BOX_FONT + 1.5) * scale
            )
            draw.text(
                ((x + w / 2) * scale, text_y),
                line,
                fill="#1a1a1a",
                font=font,
                anchor="mm",
            )

    image.save(path, "PNG")
    return True


def _to_pptx(layout, drawn: dict[str, dict[str, Any]], path: Path, title: str) -> bool:
    """Write the figure as native PowerPoint shapes.

    Not an image: boxes are text boxes and arrows are connectors, so the
    reviewer can move, recolour and retype anything in PowerPoint itself.
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Emu, Pt
    except Exception:
        return False

    def emu(points: float) -> int:
        return Emu(int(points * 12700))

    def rgb(colour: str) -> "RGBColor":
        return RGBColor.from_string(colour.lstrip("#").upper())

    head = 26.0 if title else 0.0
    presentation = Presentation()
    presentation.slide_width = emu(max(layout.width, 300))
    presentation.slide_height = emu(max(layout.height + head, 200))
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])

    if title:
        box = slide.shapes.add_textbox(
            emu(0), emu(2), emu(layout.width), emu(20)
        )
        frame = box.text_frame
        frame.text = title
        run = frame.paragraphs[0].runs[0]
        run.font.size = Pt(12)
        run.font.bold = True
        frame.paragraphs[0].alignment = PP_ALIGN.CENTER

    for key, points in layout.edges.items():
        detail = drawn.get(key)
        if detail is None:
            continue
        style = detail["style"]
        for start, end in zip(points, points[1:]):
            connector = slide.shapes.add_connector(
                MSO_CONNECTOR.STRAIGHT,
                emu(start[0]), emu(start[1] + head),
                emu(end[0]), emu(end[1] + head),
            )
            line = connector.line
            line.color.rgb = rgb(style.colour)
            line.width = Pt(style.width)
        label = detail.get("label") or ""
        if label:
            (lx, ly), _ = _longest_segment(points)
            box = slide.shapes.add_textbox(
                emu(lx - 26), emu(ly + head - 14), emu(52), emu(12)
            )
            frame = box.text_frame
            frame.text = label
            frame.paragraphs[0].alignment = PP_ALIGN.CENTER
            frame.paragraphs[0].runs[0].font.size = Pt(_ARROW_FONT)

    for key, (x, y, w, h) in layout.nodes.items():
        shape = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, emu(x), emu(y + head), emu(w), emu(h)
        )
        shape.fill.solid()
        shape.fill.fore_color.rgb = rgb("#f4f6f8")
        shape.line.color.rgb = rgb("#8a93a0")
        shape.line.width = Pt(1)
        frame = shape.text_frame
        frame.word_wrap = True
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        frame.text = key
        paragraph = frame.paragraphs[0]
        paragraph.alignment = PP_ALIGN.CENTER
        for run in paragraph.runs:
            run.font.size = Pt(_BOX_FONT)
            run.font.color.rgb = rgb("#1a1a1a")

    presentation.save(str(path))
    return True


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:48] or "figure"


def _draw_one(
    project,
    engine,
    *,
    kind: str,
    title: str,
    stem: str,
    nodes: list[Node],
    edges: list[Edge],
    drawn: dict[str, dict[str, Any]],
    theme_id: int | None,
) -> list[dict[str, Any]]:
    """Lay one figure out and write it in all three formats."""
    layout = layout_graph(nodes, edges)
    svg = to_svg(layout, drawn, title)
    directory = project.exports_dir / "figures"
    directory.mkdir(parents=True, exist_ok=True)

    grades: dict[str, int] = {}
    for detail in drawn.values():
        grade = detail.get("grade", "")
        grades[grade] = grades.get(grade, 0) + 1

    written: list[dict[str, Any]] = []
    svg_path = directory / f"{stem}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    written.append({"format": "svg", "path": svg_path})

    png_path = directory / f"{stem}.png"
    if _to_png(layout, drawn, png_path, title):
        written.append({"format": "png", "path": png_path})

    pptx_path = directory / f"{stem}.pptx"
    if _to_pptx(layout, drawn, pptx_path, title):
        written.append({"format": "pptx", "path": pptx_path})

    pdf_path = directory / f"{stem}.pdf"
    if write_pdf(layout, drawn, pdf_path, title) is not None:
        written.append({"format": "pdf", "path": pdf_path})

    crossings = count_crossings(layout)
    rows = []
    for item in written:
        project.db.insert(
            "figure",
            {
                "kind": kind,
                "theme_id": theme_id,
                "filename": item["path"].name,
                "format": item["format"],
                "n_constructs": len(layout.nodes),
                "n_links": len(drawn),
                "n_crossings": crossings,
                "grades": json.dumps(grades, sort_keys=True),
                "created_at": utc_now(),
            },
        )
        rows.append(
            {
                "format": item["format"],
                "path": item["path"],
                "n_links": len(drawn),
                "n_constructs": len(layout.nodes),
                "n_crossings": crossings,
                # What was actually drawn, keyed by grade for an evidenced
                # link and by element kind for the other two. The caller
                # reports from this rather than from the element table,
                # which may be empty when the figure stands in for it.
                "drawn": dict(grades),
            }
        )
    return rows


def write_figures(
    project,
    engine,
    themes: Sequence[dict[str, Any]],
    links: Sequence[dict[str, Any]],
    *,
    kind: str,
) -> list[dict[str, Any]]:
    """Draw the evidence maps, or the one integrated diagram."""
    project.db.execute("DELETE FROM figure WHERE kind = ?", (kind,))
    if kind == "evidence_map":
        return _evidence_maps(project, engine, themes, links)
    return _integrated(project, engine, links)


def _evidence_maps(project, engine, themes, links) -> list[dict[str, Any]]:
    written: list[dict[str, Any]] = []
    for theme in themes:
        constructs = engine.theme_constructs(theme["id"])
        names = {c["id"]: c["name"] for c in constructs}
        inside = [
            link
            for link in links
            if link["predictor_construct"] in names
            and link["outcome_construct"] in names
        ]
        if not inside:
            continue
        nodes = [Node(key=name, label=name) for name in names.values()]
        edges, drawn = [], {}
        for link in inside:
            key = f"{names[link['predictor_construct']]}->" \
                  f"{names[link['outcome_construct']]}"
            key = f"{key}#{link['id']}"
            edges.append(
                Edge(
                    key=key,
                    source=names[link["predictor_construct"]],
                    target=names[link["outcome_construct"]],
                )
            )
            drawn[key] = {
                "style": style_for(link["grade"], link["direction"]),
                "label": arrow_label(link["id"], link["direction"]),
                "grade": link["grade"],
            }
        written.extend(
            _draw_one(
                project, engine,
                kind="evidence_map",
                title=theme["name"],
                stem=f"theme-{theme['position'] or theme['id']}-{_slug(theme['name'])}",
                nodes=nodes, edges=edges, drawn=drawn,
                theme_id=theme["id"],
            )
        )
    return written


def _integrated(project, engine, links) -> list[dict[str, Any]]:
    """The whole framework, with the three kinds of element kept apart."""
    from .style import ArrowStyle

    by_id = {link["id"]: link for link in links}
    elements = engine.elements()
    if not elements:
        # Nothing has been proposed yet, so the framework is the evidence.
        elements = [
            {
                "kind": "evidenced_link",
                "source_label": link["predictor_name"],
                "target_label": link["outcome_name"],
                "graded_link_id": link["id"],
                "direction": link["direction"],
            }
            for link in links
        ]

    names: list[str] = []
    edges, drawn = [], {}
    for index, element in enumerate(elements):
        source = element["source_label"]
        target = element["target_label"]
        for name in (source, target):
            if name not in names:
                names.append(name)
        key = f"{source}->{target}#{index}"
        if element["kind"] == "evidenced_link":
            link = by_id.get(element["graded_link_id"])
            if link is None:
                continue
            style = style_for(link["grade"], link["direction"])
            label = arrow_label(link["id"], link["direction"])
            grade = link["grade"]
        else:
            overrides = _ELEMENT_STYLE[element["kind"]]
            style = ArrowStyle(
                width=overrides["width"],
                dash=overrides["dash"],
                heads=1,
                colour=overrides["colour"],
            )
            label = (
                "mechanism"
                if element["kind"] == "proposed_mechanism"
                else "untested"
            )
            grade = element["kind"]
        edges.append(Edge(key=key, source=source, target=target))
        drawn[key] = {"style": style, "label": label, "grade": grade}

    nodes = [Node(key=name, label=name) for name in names]
    if not nodes:
        return []
    return _draw_one(
        project, engine,
        kind="integrated",
        title="Integrated conceptual framework",
        stem="integrated-framework",
        nodes=nodes, edges=edges, drawn=drawn,
        theme_id=None,
    )


def write_pdf(layout, drawn: dict[str, Any], path: Path, title: str) -> Path | None:
    """The figure as a PDF, drawn rather than converted.

    Converted from what? There is no SVG-to-PDF converter in the packaged
    application, and producing a PDF by printing the PowerPoint file would
    need PowerPoint. Drawing it directly from the same layout and the same
    styles keeps all four formats the same figure.
    """
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.pdfgen import canvas as pdfcanvas
    except Exception:
        return None

    head = 26.0 if title else 0.0
    width, height = layout.width, layout.height + head
    page = pdfcanvas.Canvas(str(path), pagesize=(width, height))
    page.setTitle(title or "Figure")

    def y_of(value: float) -> float:
        # PDF counts from the bottom; the layout counts from the top.
        return height - (value + head)

    if title:
        page.setFont("Helvetica-Bold", 12)
        page.drawCentredString(width / 2, height - 18, title)

    for key, detail in drawn.items():
        points = layout.edges[key]
        style = detail["style"]
        page.setStrokeColor(HexColor(style.colour))
        page.setFillColor(HexColor(style.colour))
        page.setLineWidth(style.width)
        page.setDash(list(style.dash) if style.dash else [])
        path_object = page.beginPath()
        path_object.moveTo(points[0][0], y_of(points[0][1]))
        for point in points[1:]:
            path_object.lineTo(point[0], y_of(point[1]))
        page.drawPath(path_object)
        page.setDash([])
        _pdf_arrowhead(page, points[-2], points[-1], y_of)
        if style.heads == 2:
            _pdf_arrowhead(page, points[1], points[0], y_of)
        label = detail.get("label") or ""
        if label:
            (lx, ly), horizontal = _longest_segment(points)
            page.setFillColor(HexColor("#55606e"))
            page.setFont("Helvetica", _ARROW_FONT)
            page.drawCentredString(
                lx, y_of(ly) + (3 if horizontal else 0), label
            )

    page.setFont("Helvetica", _BOX_FONT)
    for key, (x, y, w, h) in layout.nodes.items():
        page.setFillColor(HexColor("#f4f6f8"))
        page.setStrokeColor(HexColor("#8a93a0"))
        page.setLineWidth(1)
        page.roundRect(x, y_of(y + h), w, h, 4, stroke=1, fill=1)
        page.setFillColor(HexColor("#1a1a1a"))
        lines = _wrap(key, w)
        for index, line in enumerate(lines):
            offset = (index - (len(lines) - 1) / 2) * (_BOX_FONT + 1.5)
            page.drawCentredString(
                x + w / 2, y_of(y + h / 2 + offset) - _BOX_FONT / 3, line
            )

    page.showPage()
    page.save()
    return path


def _pdf_arrowhead(page, start, end, y_of) -> None:
    import math

    angle = math.atan2(y_of(end[1]) - y_of(start[1]), end[0] - start[0])
    spread = math.radians(24)
    size = 7.0
    head = page.beginPath()
    head.moveTo(end[0], y_of(end[1]))
    head.lineTo(
        end[0] - size * math.cos(angle - spread),
        y_of(end[1]) - size * math.sin(angle - spread),
    )
    head.lineTo(
        end[0] - size * math.cos(angle + spread),
        y_of(end[1]) - size * math.sin(angle + spread),
    )
    head.close()
    page.drawPath(head, stroke=0, fill=1)
