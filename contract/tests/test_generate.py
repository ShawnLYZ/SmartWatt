import subprocess
import sys
from pathlib import Path

CONTRACT = Path(__file__).resolve().parents[1]
REPO = CONTRACT.parent
OUTPUT = REPO / "dashboard" / "src" / "types" / "contract.ts"


def _generate() -> str:
    sys.path.insert(0, str(CONTRACT))
    from generate import generate_typescript

    return generate_typescript(CONTRACT / "schemas")


def test_emits_the_four_types():
    ts = _generate()
    for name in ("Telemetry", "SmartWattEvent", "Command", "Fingerprints"):
        assert f"export interface {name}" in ts or f"export type {name}" in ts


def test_snake_case_preserved():
    """snake_case everywhere, including TypeScript. No camelCase mapping layer."""
    ts = _generate()
    assert "residual_w" in ts
    assert "residualW" not in ts
    assert "fingerprint_id" in ts
    assert "fingerprintId" not in ts


def test_carries_generated_banner():
    ts = _generate()
    assert ts.startswith("// GENERATED FILE")
    assert "do not edit" in ts.lower()


def test_source_enum_is_a_union():
    ts = _generate()
    assert '"device" | "simulator" | "replay"' in ts


def test_residual_is_plain_number_not_optional():
    ts = _generate()
    assert "residual_w: number;" in ts


def test_health_is_nullable():
    ts = _generate()
    assert "health: TelemetryHealth | null;" in ts


def test_output_is_deterministic():
    """A hand-edit must fail CI rather than survive."""
    first = _generate()
    second = _generate()
    assert first == second


def test_committed_file_matches_fresh_generation():
    assert OUTPUT.exists(), "run: uv run python contract/generate.py"
    assert OUTPUT.read_text(encoding="utf-8") == _generate()


def test_cli_writes_the_file():
    result = subprocess.run(
        [sys.executable, str(CONTRACT / "generate.py")],
        capture_output=True, text=True, cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert OUTPUT.exists()
