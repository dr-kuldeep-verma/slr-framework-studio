"""Labelled screening benchmarks, loaded from disk or fetched once.

The format is the one the SYNERGY collection publishes: one CSV per
published systematic review, a row per record the search returned, and the
decisions the human reviewers actually made. Those datasets are CC-BY and
each cites the review it came from.

Two details in that format decide whether a benchmark is fair, and both
are easy to get wrong:

**Which label.** The CSVs carry two. `label_abstract_screening` is what
the reviewers decided from the title and abstract; `label_included` is
what survived full-text assessment as well. Scoring a title/abstract
screener against the second punishes it for every study that looked
eligible and turned out not to be -- which was the correct decision at
that stage. On the first dataset tried, the two differ by a factor of six:
178 records against 29. This module prefers the abstract-screening label
and records which one it used, because a benchmark result without that
fact is not interpretable.

**Missing abstracts.** A third of the records in a typical dataset have no
abstract at all -- a title and nothing else. That is a property of the
corpus, not of any tool, and it bounds what any screener can achieve. The
loader counts them so the report can say so.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "BenchmarkRecord",
    "BenchmarkDataset",
    "DatasetError",
    "load_csv",
    "stratified_sample",
    "SYNERGY_INDEX_URL",
]

SYNERGY_INDEX_URL = (
    "https://raw.githubusercontent.com/asreview/synergy-dataset/master/index.json"
)

#: Preferred first. A title/abstract screener is judged on the decision
#: made from the title and abstract.
_LABEL_COLUMNS = ("label_abstract_screening", "label_included", "included", "label")

_TITLE_COLUMNS = ("title", "primary_title", "Title")
_ABSTRACT_COLUMNS = ("abstract", "Abstract", "notes_abstract")


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class BenchmarkRecord:
    title: str
    abstract: str
    relevant: int
    year: int | None = None
    doi: str = ""
    keywords: str = ""
    #: Row number in the source file, so a result can be traced back.
    source_row: int = 0

    @property
    def has_abstract(self) -> bool:
        return bool(self.abstract.strip())


@dataclass
class BenchmarkDataset:
    """A labelled corpus, with the facts needed to interpret a score."""

    name: str
    records: list[BenchmarkRecord]
    label_column: str
    source: str = ""
    licence: str = ""
    reference: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def n_relevant(self) -> int:
        return sum(r.relevant for r in self.records)

    @property
    def prevalence(self) -> float:
        return self.n_relevant / len(self.records) if self.records else 0.0

    @property
    def n_without_abstract(self) -> int:
        return sum(1 for r in self.records if not r.has_abstract)

    def characteristics(self) -> dict[str, Any]:
        """What a reader needs in order to judge any score computed on this.

        Reported with every result rather than on request: a recall figure
        means something different on a corpus with 0.8% prevalence and a
        third of its abstracts missing than on a balanced one.
        """
        n = len(self.records)
        return {
            "name": self.name,
            "n_records": n,
            "n_relevant": self.n_relevant,
            "prevalence": round(self.prevalence, 5),
            "n_without_abstract": self.n_without_abstract,
            "share_without_abstract": round(self.n_without_abstract / n, 4) if n else 0.0,
            "label_column": self.label_column,
            "source": self.source,
            "licence": self.licence,
            "reference": self.reference,
            "notes": list(self.notes),
        }


def _first_present(row: dict[str, str], names: Iterable[str]) -> str | None:
    for name in names:
        if name in row:
            return name
    return None


def _as_label(raw: str) -> int | None:
    value = (raw or "").strip().lower()
    if value in ("1", "true", "yes", "included"):
        return 1
    if value in ("0", "false", "no", "excluded"):
        return 0
    return None


def load_csv(
    path: str | Path,
    *,
    name: str = "",
    label_column: str | None = None,
    source: str = "",
    licence: str = "",
    reference: str = "",
) -> BenchmarkDataset:
    """Read a labelled benchmark from a CSV file.

    Rows whose label is neither 0 nor 1 are dropped and counted, rather
    than being guessed at: an unlabelled record is not a negative, and
    treating it as one inflates specificity.
    """
    path = Path(path)
    if not path.is_file():
        raise DatasetError(f"No benchmark dataset at {path}")

    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise DatasetError(f"{path.name} has no rows")

    header = rows[0]
    chosen = label_column or _first_present(header, _LABEL_COLUMNS)
    if chosen is None:
        raise DatasetError(
            f"{path.name} has no label column. Looked for: "
            f"{', '.join(_LABEL_COLUMNS)}. Without human decisions there is "
            "nothing to score against."
        )
    if chosen not in header:
        raise DatasetError(f"{path.name} has no column {chosen!r}")

    title_col = _first_present(header, _TITLE_COLUMNS)
    abstract_col = _first_present(header, _ABSTRACT_COLUMNS)
    if title_col is None:
        raise DatasetError(f"{path.name} has no title column")

    records: list[BenchmarkRecord] = []
    n_unlabelled = 0
    n_untitled = 0
    for index, row in enumerate(rows, start=2):  # 2: row 1 is the header
        label = _as_label(row.get(chosen, ""))
        if label is None:
            n_unlabelled += 1
            continue
        title = (row.get(title_col) or "").strip()
        if not title:
            n_untitled += 1
            continue
        year_raw = (row.get("year") or "").strip()[:4]
        records.append(
            BenchmarkRecord(
                title=title,
                abstract=(row.get(abstract_col) or "").strip() if abstract_col else "",
                relevant=label,
                year=int(year_raw) if year_raw.isdigit() else None,
                doi=(row.get("doi") or "").strip(),
                keywords=(row.get("keywords") or "").strip(),
                source_row=index,
            )
        )

    if not records:
        raise DatasetError(f"{path.name} yielded no labelled records")

    notes = []
    if chosen != "label_abstract_screening" and "label_abstract_screening" in header:
        notes.append(
            f"Scored against {chosen!r} although this dataset also has "
            "'label_abstract_screening'. The latter is the decision made "
            "from the title and abstract and is the fairer standard for a "
            "title/abstract screener."
        )
    if chosen == "label_included":
        notes.append(
            "'label_included' is the post-full-text decision. A record "
            "correctly kept at abstract screening and later excluded on "
            "full text counts here as a false positive, so specificity is "
            "understated against this label."
        )
    if n_unlabelled:
        notes.append(
            f"{n_unlabelled} row(s) had no usable label and were dropped "
            "rather than counted as irrelevant."
        )
    if n_untitled:
        notes.append(f"{n_untitled} row(s) had no title and were dropped.")

    dataset = BenchmarkDataset(
        name=name or path.stem,
        records=records,
        label_column=chosen,
        source=source or str(path),
        licence=licence,
        reference=reference,
        notes=notes,
    )
    without = dataset.n_without_abstract
    if without:
        dataset.notes.append(
            f"{without} of {len(records)} records ({without / len(records):.0%}) "
            "have no abstract. Those are screened on their title alone, which "
            "bounds what any screener can do with them."
        )
    return dataset


def stratified_sample(
    dataset: BenchmarkDataset,
    *,
    n_irrelevant: int,
    seed: int = 20260101,
) -> BenchmarkDataset:
    """Keep every relevant record; sample the irrelevant ones.

    A full dataset is thousands of records, and screening each one costs
    two model calls plus adjudication. Sampling only the irrelevant side
    keeps recall exact -- every relevant record is still screened, and
    recall is the figure that matters -- while specificity becomes an
    estimate over the sample, which is what its interval is for.

    Deterministic given the seed, so a result can be reproduced.
    """
    import random

    if n_irrelevant < 0:
        raise ValueError("n_irrelevant cannot be negative")
    relevant = [r for r in dataset.records if r.relevant]
    irrelevant = [r for r in dataset.records if not r.relevant]
    if n_irrelevant >= len(irrelevant):
        return dataset

    rng = random.Random(seed)
    kept = rng.sample(irrelevant, n_irrelevant)
    # Back into source order, so the sample is not ordered by label: a
    # screener is not influenced by order, but a human reading the log is.
    sampled = sorted(relevant + kept, key=lambda r: r.source_row)

    return BenchmarkDataset(
        name=f"{dataset.name} (sampled)",
        records=sampled,
        label_column=dataset.label_column,
        source=dataset.source,
        licence=dataset.licence,
        reference=dataset.reference,
        notes=[
            *dataset.notes,
            f"Stratified sample: all {len(relevant)} relevant record(s) kept, "
            f"{n_irrelevant} of {len(irrelevant)} irrelevant sampled with "
            f"seed {seed}. Recall is therefore computed over every relevant "
            "record in the dataset; specificity and burden are estimates "
            "over the sample, and their intervals reflect that. Prevalence "
            "in the sample is not the prevalence of the corpus.",
        ],
    )


def fetch_synergy_index(cache: str | Path) -> dict[str, Any]:
    """Download the SYNERGY dataset index, once, into *cache*.

    Deliberately not called by anything that runs automatically. A
    benchmark that silently downloads is a benchmark that behaves
    differently on a machine with no network, and the point of it is to
    produce a number somebody can rely on.
    """
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / "synergy-index.json"
    if target.is_file():
        return json.loads(target.read_text(encoding="utf-8"))

    try:
        import httpx

        response = httpx.get(SYNERGY_INDEX_URL, timeout=60, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        raise DatasetError(
            f"Could not fetch the SYNERGY index from {SYNERGY_INDEX_URL}: {exc}. "
            "Download it by hand and put it at "
            f"{target}, or pass a CSV you already have."
        ) from exc
    target.write_text(response.text, encoding="utf-8")
    return json.loads(response.text)


def fetch_synergy_dataset(dataset_id: str, cache: str | Path) -> Path:
    """Download one SYNERGY dataset CSV into *cache* and return its path."""
    cache = Path(cache)
    index = fetch_synergy_index(cache)
    entry = index.get(dataset_id)
    if entry is None:
        raise DatasetError(
            f"{dataset_id!r} is not in the SYNERGY index. Available: "
            + ", ".join(sorted(index)[:10])
            + f" ... ({len(index)} in total)"
        )
    target = cache / f"{dataset_id}.csv"
    if target.is_file():
        return target

    try:
        import httpx

        response = httpx.get(entry["url"], timeout=300, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise DatasetError(
            f"Could not fetch {dataset_id} from {entry['url']}: {exc}"
        ) from exc
    target.write_bytes(response.content)
    print(f"  {dataset_id}: {len(response.content) / 1e6:.1f} MB", file=sys.stderr)
    return target
