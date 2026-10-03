import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

CONTRACT = Path(__file__).resolve().parents[1]


def load_schema(name: str) -> dict:
    return json.loads((CONTRACT / "schemas" / f"{name}.schema.json").read_text())


def load_example(name: str) -> dict:
    return json.loads((CONTRACT / "examples" / f"{name}.json").read_text())


@pytest.fixture
def telemetry_validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema("telemetry"))


@pytest.fixture
def event_validator() -> Draft202012Validator:
    return Draft202012Validator(load_schema("event"))


FEATURE_NAMES = (
    "delta_p", "delta_q1", "delta_dist", "delta_s",
    "pf_disp", "pf_true", "delta_irms", "delta_crest",
    "h3_h1", "h5_h1", "h7_h1",
    "inrush_ratio", "settle_cycles", "log_delta_p",
)
