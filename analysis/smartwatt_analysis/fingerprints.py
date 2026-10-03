"""The training file.

Plain, human-readable, hand-editable CSV. It can be inspected, corrected and
backed up without tooling, which is what US58 asks for and what makes a bad
capture fixable rather than a reason to redo a session.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FEATURE_COLUMNS: tuple[str, ...] = (
    "delta_p", "delta_q1", "delta_dist", "delta_s",
    "pf_disp", "pf_true", "delta_irms", "delta_crest",
    "h3_h1", "h5_h1", "h7_h1",
    "inrush_ratio", "settle_cycles", "log_delta_p",
)

#: The conditions of each capture. These are what let a gap between
#: cross-validated and live accuracy be EXPLAINED rather than hand-waved.
CONDITION_COLUMNS: tuple[str, ...] = (
    "ts", "session_id", "background_w", "concurrent_ids",
    "vrms_mean", "freq_mean", "notes",
)

ALL_COLUMNS: tuple[str, ...] = (
    ("training_id", "label", "edge") + FEATURE_COLUMNS + CONDITION_COLUMNS
)


class MalformedFingerprints(ValueError):
    """The file cannot be read as a table of fingerprints: a short row, an
    empty or non-numeric feature, or a non-finite one. The message names the
    line and the column, so a bad hand edit is found and fixed rather than
    surfacing as a stack trace somewhere downstream."""


@dataclass(slots=True)
class Fingerprint:
    training_id: str
    label: str
    edge: str
    features: dict[str, float]
    ts: float
    session_id: str
    #: The three measured conditions are None when nothing measured them
    #: (no telemetry in the capture's window). None is written as an EMPTY
    #: cell and read back as None -- never as 0.0, which would present an
    #: unmeasured 0 W / 0 V / 0 Hz as a measurement (non-negotiable #2).
    background_w: float | None
    concurrent_ids: str
    vrms_mean: float | None
    freq_mean: float | None
    notes: str

    def as_row(self) -> dict[str, str]:
        row = {
            "training_id": self.training_id,
            "label": self.label,
            "edge": self.edge,
            "ts": repr(self.ts),
            "session_id": self.session_id,
            "background_w": _optional_cell(self.background_w),
            "concurrent_ids": self.concurrent_ids,
            "vrms_mean": _optional_cell(self.vrms_mean),
            "freq_mean": _optional_cell(self.freq_mean),
            "notes": self.notes,
        }
        for name in FEATURE_COLUMNS:
            row[name] = repr(float(self.features[name]))
        return row


def _optional_cell(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def _number(line: int, column: str, cell: str | None, *, finite: bool) -> float:
    if cell is None or not cell.strip():
        raise MalformedFingerprints(f"line {line}: {column} is empty")
    try:
        value = float(cell)
    except ValueError:
        raise MalformedFingerprints(
            f"line {line}: {column} is not a number ({cell!r})"
        ) from None
    if finite and not math.isfinite(value):
        raise MalformedFingerprints(
            f"line {line}: {column} is not finite ({cell!r})"
        )
    return value


def _optional_number(line: int, column: str, cell: str | None) -> float | None:
    if cell is None or not cell.strip():
        return None
    return _number(line, column, cell, finite=False)


def _from_row(line: int, row: dict[str, str | None]) -> Fingerprint:
    # csv.DictReader fills the missing trailing cells of a short row with
    # None, and puts a long row's extras under the key None.
    if None in row:
        raise MalformedFingerprints(
            f"line {line}: {len(ALL_COLUMNS) + len(row[None])} cells, "  # type: ignore[arg-type]
            f"expected {len(ALL_COLUMNS)}"
        )
    missing = [name for name in ALL_COLUMNS if row.get(name) is None]
    if missing:
        raise MalformedFingerprints(
            f"line {line}: short row, missing {', '.join(missing)}"
        )
    return Fingerprint(
        training_id=row["training_id"] or "",
        label=row["label"] or "",
        edge=row["edge"] or "",
        features={
            name: _number(line, name, row[name], finite=True)
            for name in FEATURE_COLUMNS
        },
        ts=_number(line, "ts", row["ts"], finite=True),
        session_id=row["session_id"] or "",
        background_w=_optional_number(line, "background_w", row["background_w"]),
        concurrent_ids=row["concurrent_ids"] or "",
        vrms_mean=_optional_number(line, "vrms_mean", row["vrms_mean"]),
        freq_mean=_optional_number(line, "freq_mean", row["freq_mean"]),
        notes=row["notes"] or "",
    )


def read_fingerprints(path: Path) -> list[Fingerprint]:
    """Raises MalformedFingerprints, naming the line, for a row that cannot
    be read: a short row, an empty, non-numeric or non-finite feature."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        # line_num is the physical line just read, header being line 1.
        return [_from_row(reader.line_num, row) for row in reader]


def write_fingerprints(path: Path, rows: list[Fingerprint]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ALL_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_row())


def append_fingerprint(path: Path, row: Fingerprint) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(ALL_COLUMNS))
        if write_header:
            writer.writeheader()
        writer.writerow(row.as_row())


def to_matrix(rows: list[Fingerprint]) -> tuple[np.ndarray, np.ndarray]:
    X = np.array(
        [[row.features[name] for name in FEATURE_COLUMNS] for row in rows],
        dtype=float,
    )
    y = np.array([row.label for row in rows], dtype=object)
    return X, y
