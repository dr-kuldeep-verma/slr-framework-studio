"""Generate the synthetic PDFs used by the tests and the bundled demo.

The demo cannot ship real papers: they are other people's copyright, and a
reviewer must never be able to confuse a sample with a study. These are
invented reports whose metadata matches the invented records in the demo RIS
files, written so that the full-text stage has something real to read — a
methods section, a findings section, and a page with no text layer at all so
the OCR path is exercised.

Run from the project root:

    python tools/make_sample_pdfs.py
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

ROOT = Path(__file__).resolve().parent.parent
TARGETS = (
    ROOT / "tests" / "fixtures" / "pdfs",
    ROOT / "src" / "slr_studio" / "demo" / "pdfs",
)

BANNER = (
    "SYNTHETIC SAMPLE — not a real study. Generated for SLR Framework Studio."
)


def styles():
    sheet = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=sheet["Title"], fontSize=16, leading=20, spaceAfter=10
        ),
        "author": ParagraphStyle(
            "author", parent=sheet["Normal"], fontSize=10, leading=14, spaceAfter=6
        ),
        "meta": ParagraphStyle(
            "meta", parent=sheet["Normal"], fontSize=8, leading=11,
            textColor="#666666", spaceAfter=12,
        ),
        "h": ParagraphStyle(
            "h", parent=sheet["Heading2"], fontSize=12, leading=15, spaceBefore=10,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", parent=sheet["BodyText"], fontSize=10, leading=14, spaceAfter=8
        ),
    }


def scanned_page_image(lines: list[str], width: int = 1240, height: int = 1754) -> bytes:
    """A page of text rendered as an image, so it has no text layer at all."""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default(size=34)
    y = 140
    for line in lines:
        draw.text((120, y), line, fill="black", font=font)
        y += 56
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(200, 200))
    return buffer.getvalue()


def build(path: Path, spec: dict) -> None:
    style = styles()
    document = SimpleDocTemplate(
        str(path), pagesize=A4,
        leftMargin=2.2 * cm, rightMargin=2.2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=spec["title"], author="; ".join(spec["authors"]),
    )
    flow = [
        Paragraph(spec["title"], style["title"]),
        Paragraph("; ".join(spec["authors"]), style["author"]),
        Paragraph(
            f"{spec['journal']} ({spec['year']}) "
            + (f"&middot; doi:{spec['doi']}" if spec.get("doi") else "")
            + f"<br/>{BANNER}",
            style["meta"],
        ),
    ]
    for heading, paragraphs in spec["sections"]:
        flow.append(Paragraph(heading, style["h"]))
        for text in paragraphs:
            flow.append(Paragraph(text, style["body"]))
        if heading == spec.get("break_after"):
            flow.append(PageBreak())

    if spec.get("scanned_page"):
        flow.append(PageBreak())
        image = RLImage(
            io.BytesIO(scanned_page_image(spec["scanned_page"])),
            width=16 * cm, height=22.6 * cm,
        )
        flow.append(image)
        flow.append(Spacer(1, 2))

    document.build(flow)
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")


SPECS: dict[str, dict] = {
    "menon_2021_kerala.pdf": {
        "title": (
            "Resident participation in sustainable tourism planning: "
            "evidence from coastal Kerala"
        ),
        "authors": ["R. Menon", "L. P. Fernandes"],
        "journal": "Journal of Sustainable Tourism Studies 29(4)",
        "year": 2021,
        "doi": "10.1080/jsts.2021.004512",
        "break_after": "2. Methods",
        "sections": [
            ("1. Introduction", [
                "Coastal tourism in Kerala has grown steadily over two decades, "
                "and with it the question of who decides how that growth is "
                "managed. This paper examines how residents of eleven coastal "
                "panchayats take part in tourism planning decisions.",
                "We ask two questions. First, which residents participate, and "
                "through which channels. Second, whether participation is "
                "associated with support for further tourism development.",
            ]),
            ("2. Methods", [
                "This is a cross-sectional household survey. We surveyed 814 "
                "households across eleven panchayats between January and April "
                "2019, using stratified random sampling within each panchayat.",
                "Participation was measured with a six-item scale covering "
                "attendance at planning meetings, membership of tourism "
                "committees, and written submissions. Support for tourism "
                "development was measured with four items adapted from earlier "
                "work. We also interviewed nineteen planning officers.",
                "There was no manipulated treatment and no control group; the "
                "design is observational.",
            ]),
            ("3. Findings", [
                "Participation was strongly associated with support for further "
                "tourism development (r = 0.41, p &lt; .001). The association "
                "held after controlling for household income and distance from "
                "the shoreline.",
                "Residents who had attended at least one planning meeting "
                "reported higher trust in the panchayat than those who had not. "
                "Trust in local government predicted willingness to join a "
                "tourism committee.",
                "Participation was uneven. Households headed by women were "
                "under-represented at planning meetings, a pattern the planning "
                "officers we interviewed recognised but did not attribute to "
                "any formal barrier.",
            ]),
            ("4. Limitations", [
                "The cross-sectional design cannot establish the direction of "
                "the association between participation and support. The sample "
                "covers one state and may not generalise.",
            ]),
        ],
    },
    "okonkwo_2019_benefit_sharing.pdf": {
        # No DOI printed anywhere, so this one has to be matched by title.
        "title": "Benefit sharing and perceived fairness in community-based ecotourism",
        "authors": ["A. Okonkwo", "M. Diallo", "S. Baptiste"],
        "journal": "Tourism Governance Review 11(2)",
        "year": 2019,
        "doi": "",
        "break_after": "2. Methods",
        "sections": [
            ("1. Introduction", [
                "Community-based ecotourism is often justified by the benefits "
                "it returns to the host community. Whether those benefits are "
                "perceived as fairly shared is a separate question, and the one "
                "this study addresses.",
            ]),
            ("2. Methods", [
                "We surveyed 420 residents across three villages adjacent to a "
                "protected area. Perceived fairness of benefit distribution was "
                "measured with a five-item scale. Support for further tourism "
                "development was the outcome.",
                "The study is observational. Data were collected in a single "
                "wave during 2017.",
            ]),
            ("3. Findings", [
                "Perceived fairness explained 31% of the variance in support for "
                "further tourism development. The relationship was positive and "
                "statistically significant.",
                "Residents employed directly in tourism reported higher perceived "
                "fairness than those who were not, but the difference narrowed "
                "once village of residence was taken into account.",
            ]),
            ("4. Limitations", [
                "Self-reported fairness may reflect general satisfaction rather "
                "than any specific distribution of benefits.",
            ]),
        ],
    },
    "pereira_2020_scoping_review.pdf": {
        # A review article: the full-text stage should exclude this one.
        "title": "Volunteer tourism and host community voice: a scoping review",
        "authors": ["A. Pereira"],
        "journal": "Review of Tourism Practice 6(1)",
        "year": 2020,
        "doi": "10.5555/rtp.2020.0005",
        "break_after": "2. Method",
        "sections": [
            ("1. Introduction", [
                "This article reports a scoping review of the literature on "
                "volunteer tourism and host community voice. It does not report "
                "primary data.",
            ]),
            ("2. Method", [
                "We searched four databases for studies published between 2000 "
                "and 2019, and charted 64 studies against a framework of four "
                "conceptions of community voice.",
                "As a review of existing studies, this work collects no new data "
                "from residents or operators.",
            ]),
            ("3. Charting the literature", [
                "Most of the 64 studies conceptualised host community voice as "
                "consultation rather than as decision-making authority.",
                "Only nine studies reported any measure of what residents "
                "themselves said they wanted.",
            ]),
        ],
        "scanned_page": [
            "Appendix A. Search strategy",
            "",
            "Database searches were run on 14 March 2019.",
            "The scoping review covered four databases.",
            "This page is an image with no text layer,",
            "so reading it requires optical character",
            "recognition.",
        ],
    },
}


def main() -> None:
    for directory in TARGETS:
        directory.mkdir(parents=True, exist_ok=True)
    first = TARGETS[0]
    for name, spec in SPECS.items():
        build(first / name, spec)
    for directory in TARGETS[1:]:
        for name in SPECS:
            (directory / name).write_bytes((first / name).read_bytes())
            print(f"copied to {directory / name}")


if __name__ == "__main__":
    main()
