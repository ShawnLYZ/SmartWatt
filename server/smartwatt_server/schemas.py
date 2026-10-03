"""Contract validators, loaded once from contract/schemas/.

The server validates against the SAME files the simulator, the firmware's
native tests and the dashboard's fixtures use. That shared source is what
stops three languages drifting apart.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import json

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contract" / "schemas"

_STEMS = ("telemetry", "event", "command", "fingerprints")


@lru_cache(maxsize=1)
def validators() -> dict[str, Draft202012Validator]:
    return {
        stem: Draft202012Validator(
            json.loads((SCHEMA_DIR / f"{stem}.schema.json").read_text())
        )
        for stem in _STEMS
    }
