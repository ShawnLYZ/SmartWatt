"""Build and publish the classifier table.

`smartwatt/fingerprints` is a FOURTH topic, beyond the contract paragraph in
PRD.md, and so is `fingerprint_id` in telemetry. Both are flagged in the
master spec. The justification is that US57 - add an appliance later without
reflashing anything - cannot be satisfied otherwise: the classifier runs in
firmware, so the trained table has to reach the device, and the only
alternatives are a cable or a reflash.

THE DEVICE DOES NOT SUBSCRIBE TO THIS TOPIC YET (R7). Publishing here puts a
retained table on the broker for the day it does; it does not load anything
on the device today. What the device loads is firmware/data/fingerprints.csv,
written to its LittleFS by `pio run -e esp32-s3 -t uploadfs` from firmware/.
So nothing here is evidence that a table reached the device: the wizard
(wizard.py) leaves PUSH only when the device's newest telemetry reports the
fingerprint_id computed below, whichever way the table got there.

This module never touches MQTT itself. It calls whatever publisher it is
handed (api.py's `app.state.fingerprint_publisher`), so the architecture test's
scan of publish references stays confined to ingest.py and api.py.
"""

from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import Callable

from smartwatt_analysis.fingerprints import (
    FEATURE_COLUMNS,
    Fingerprint,
    to_matrix,
)
from smartwatt_analysis.harness import derive_threshold

TOPIC = "smartwatt/fingerprints"

#: firmware/lib/classify/fingerprints.h: FingerprintTable::kMaxRows. A file
#: with more rows than this is REFUSED by the device's loader, not truncated.
MAX_ROWS = 256
#: FingerprintRow::training_id is char[32] and ::label is char[24]. The
#: loader truncates an over-long id (so the device hashes a different one)
#: and skips a row with an over-long label.
MAX_TRAINING_ID = 31
MAX_LABEL = 23

_FNV_OFFSET = 1469598103934665603
_FNV_PRIME = 1099511628211
_MASK64 = (1 << 64) - 1
_SEPARATOR = b"\xff"
_LABEL = re.compile(r"[a-z][a-z0-9_]*")
_GENERATED_ID = re.compile(r"unknown_[0-9]+")


class UnpushableTable(ValueError):
    """The table cannot be pushed: the device would refuse it, or would load
    a different table from the one this module hashed."""


def is_emittable_label(label: str) -> bool:
    """Mirrors firmware's is_emittable_label, plus its length test.

    `[a-z][a-z0-9_]*`, never the tracker's generated `unknown_<digits>`
    shape, and at most 23 characters. The loader SKIPS a row that fails any
    of these, so a table containing one hashes differently on the device.
    """
    return (
        len(label) <= MAX_LABEL
        and _LABEL.fullmatch(label) is not None
        and _GENERATED_ID.fullmatch(label) is None
    )


def fingerprint_id(rows: list[Fingerprint]) -> str:
    """firmware's compute_fingerprint_id, byte for byte.

    FNV-1a-64 over the rows in FILE order: each row's training_id bytes, a
    0xFF separator, the label bytes, 0xFF, then the fourteen features as raw
    little-endian float64 in FEATURE_COLUMNS order. The id is the low 32
    bits as eight hex digits. Pinned against the C++ by
    firmware/test/test_fingerprint_id and firmware/tests_py/test_fingerprint_id.py.
    """
    value = _FNV_OFFSET
    for row in rows:
        data = (
            row.training_id.encode() + _SEPARATOR
            + row.label.encode() + _SEPARATOR
            + struct.pack(
                "<14d", *(float(row.features[name]) for name in FEATURE_COLUMNS)
            )
        )
        for byte in data:
            value = ((value ^ byte) * _FNV_PRIME) & _MASK64
    return f"{value & 0xFFFFFFFF:08x}"


def _check_loadable(rows: list[Fingerprint]) -> None:
    if not rows:
        raise UnpushableTable("the training file has no rows")
    if len(rows) > MAX_ROWS:
        raise UnpushableTable(
            f"{len(rows)} rows; the device holds at most {MAX_ROWS} and "
            "refuses a larger table"
        )
    for row in rows:
        if not is_emittable_label(row.label):
            raise UnpushableTable(
                f"{row.training_id}: label {row.label!r} is not one the device "
                f"loads ([a-z][a-z0-9_]*, at most {MAX_LABEL} characters, not "
                "unknown_<n>)"
            )
        if len(row.training_id.encode()) > MAX_TRAINING_ID or "," in row.training_id:
            raise UnpushableTable(
                f"training_id {row.training_id!r} does not survive the device "
                f"loader (at most {MAX_TRAINING_ID} bytes, no commas)"
            )
        if row.edge not in ("on", "off"):
            raise UnpushableTable(f"{row.training_id}: edge {row.edge!r} is not on/off")
        if not all(math.isfinite(row.features[name]) for name in FEATURE_COLUMNS):
            raise UnpushableTable(f"{row.training_id}: a feature is not finite")


def build_fingerprints_payload(
    rows: list[Fingerprint], active: list[str] | None = None
) -> dict:
    """The smartwatt.fingerprints.v1 payload for these rows.

    Raises UnpushableTable rather than build a payload the schema forbids or
    the device would load differently. In particular a single-class table
    derives a threshold of 0, which fails the schema's exclusiveMinimum and
    which the firmware loader refuses too.
    """
    _check_loadable(rows)
    X, y = to_matrix(rows)
    threshold = float(derive_threshold(X, y))
    if not threshold > 0.0:
        raise UnpushableTable(
            f"derived rejection threshold is {threshold}; a table needs at "
            "least two classes, or some within-class spread, to classify"
        )

    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0

    return {
        "schema": "smartwatt.fingerprints.v1",
        "fingerprint_id": fingerprint_id(rows),
        "rejection_threshold": threshold,
        "active_features": list(active or FEATURE_COLUMNS),
        "normalisation": {
            "mean": {name: float(mean[i]) for i, name in enumerate(FEATURE_COLUMNS)},
            "std": {name: float(std[i]) for i, name in enumerate(FEATURE_COLUMNS)},
        },
        "rows": [
            {
                "training_id": row.training_id,
                "label": row.label,
                "edge": row.edge,
                "features": {
                    name: float(row.features[name]) for name in FEATURE_COLUMNS
                },
            }
            for row in rows
        ],
    }


def publish_fingerprints(
    publisher: Callable[[str, str, bool], object], payload: dict
) -> bool:
    """Publishes RETAINED, so a device that subscribes -- none does yet --
    would pick the table up after a reboot.

    Returns True only when the publisher returned exactly True: the broker
    client accepted the message. That is all it means. A publisher that
    reports nothing has not reported success, and neither result says
    anything about the device (see the module docstring).
    """
    return publisher(TOPIC, json.dumps(payload), True) is True
