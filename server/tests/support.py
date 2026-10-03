import json
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[2] / "contract"
#: The example household as an appliance file; conftest.py points every
#: server test at it (see the file's header).
EXAMPLE_APPLIANCES = Path(__file__).resolve().parent / "example_appliances.toml"


def example(name: str) -> dict:
    return json.loads((CONTRACT / "examples" / f"{name}.json").read_text())
