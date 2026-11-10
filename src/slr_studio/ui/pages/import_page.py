"""Stage 3: importing database exports."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from ...importers.registry import SUPPORTED_SUFFIXES
from ...importers.service import ImportService
from ..widgets import DropArea, SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["ImportPage"]

_FILE_FILTER = (
    "Reference exports (*.ris *.txt *.nbib *.xml *.csv *.tsv *.enw);;All files (*)"
)


class ImportPage(StagePage):
    title = "Import"
    subtitle = "Bring in RIS, PubMed, EndNote XML and CSV exports."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None

        self.drop = DropArea(
            "Drop RIS, .nbib, EndNote XML or CSV exports here",
            suffixes=SUPPORTED_SUFFIXES,
            file_filter=_FILE_FILTER,
        )
        self.drop.files_dropped.connect(self._import_files)
        self._layout.addWidget(self.drop)

        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet("color: #2d6cdf;")
        self._layout.addWidget(self.busy_label)

        self._layout.addWidget(section_label("Files imported"))
        self.files_table = SimpleTable(
            ["File", "Database", "Format", "Records", "No abstract", "Unreadable", "Imported"]
        )
        self.files_table.fit_to_rows(8)
        self._layout.addWidget(self.files_table)

        self._layout.addWidget(section_label("Records per database"))
        self.db_table = SimpleTable(["Database", "Imported", "After deduplication"])
        self.db_table.fit_to_rows(6)
        self._layout.addWidget(self.db_table)

        self._layout.addWidget(section_label("Seed studies"))
        seed_hint = QLabel(
            "Studies the search must find. A seed that is missing is the first "
            "sign the query is too narrow."
        )
        seed_hint.setWordWrap(True)
        seed_hint.setStyleSheet("color: #5b6677;")
        self._layout.addWidget(seed_hint)

        seed_row = QHBoxLayout()
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("Seed study DOI, e.g. 10.1080/jsts.2021.004512")
        self.seed_edit.returnPressed.connect(self._add_seed)
        seed_row.addWidget(self.seed_edit, 1)
        add_seed = QPushButton("Add seed")
        add_seed.clicked.connect(self._add_seed)
        seed_row.addWidget(add_seed)
        self._layout.addLayout(seed_row)

        self.seed_table = SimpleTable(["DOI", "Found", "In database", "Title"])
        self.seed_table.fit_to_rows(6)
        self._layout.addWidget(self.seed_table)
        self._layout.addStretch(1)

    # -- importing ---------------------------------------------------------

    def _import_files(self, paths: list[str]) -> None:
        if self.project is None or not paths:
            return
        if self._task is not None and self._task.isRunning():
            QMessageBox.information(
                self, "Import in progress", "Wait for the current import to finish."
            )
            return
        service = ImportService(self.project)
        self.busy_label.setText(f"Importing {len(paths)} file(s)…")
        self.drop.setEnabled(False)

        task = Task(service.import_many, list(paths))
        task.succeeded.connect(self._import_done)
        task.failed.connect(self._import_failed)
        task.finished.connect(lambda: self.drop.setEnabled(True))
        self._task = task
        task.start()

    def _import_done(self, outcome: tuple) -> None:
        summaries, failures = outcome
        self.busy_label.setText("")
        total = sum(s.n_records for s in summaries)
        self.status_message.emit(
            f"Imported {total} record(s) from {len(summaries)} file(s)."
        )
        if failures:
            from ...errorlog import log_path

            failures = list(failures) + [("Details", f"Written to {log_path()}")]
            QMessageBox.warning(
                self,
                "Some files were not imported",
                "\n\n".join(f"{name}:\n{message}" for name, message in failures),
            )
        self.refresh()
        self.data_changed.emit()

    def _import_failed(self, message: str) -> None:
        self.busy_label.setText("")
        QMessageBox.critical(self, "Import failed", message)

    # -- seeds -------------------------------------------------------------

    def _add_seed(self) -> None:
        if self.project is None:
            return
        text = self.seed_edit.text().strip()
        if not text:
            return
        try:
            ImportService(self.project).add_seed(text)
        except ValueError as exc:
            QMessageBox.warning(self, "Not a DOI", str(exc))
            return
        self.seed_edit.clear()
        self.refresh()

    # -- display -----------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            for table in (self.files_table, self.db_table, self.seed_table):
                table.fill([])
            return
        service = ImportService(self.project)
        self.files_table.fill(
            [
                [
                    Path(f["filename"]).name,
                    f["source_database"],
                    f"{f['format']}/{f['dialect']}",
                    f["n_records"],
                    f["n_missing_abstract"],
                    f["n_parse_errors"],
                    f["imported_at"].replace("T", " ").replace("Z", ""),
                ]
                for f in service.files()
            ]
        )
        self.db_table.fill(
            [
                [row["source_database"], row["n_imported"], row["n_after_dedup"]]
                for row in service.counts_per_database()
            ]
        )
        self.seed_table.fill(
            [
                [
                    seed["doi"],
                    "yes" if seed["found"] else "NOT FOUND",
                    seed["source_database"],
                    seed["title"][:90],
                ]
                for seed in service.check_seeds()
            ]
        )
