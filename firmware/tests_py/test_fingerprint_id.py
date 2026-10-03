"""The Python half of the fingerprint_id pin.

firmware/test/test_fingerprint_id loads the same committed CSV through
FileFingerprintSource and asserts the same literal.

The wizard's PUSH step moves to VERIFY only when the device's newest
telemetry reports the fingerprint_id of the table the server built
(server/smartwatt_server/wizard.py); the broker accepting the retained
publish is not that gate. The device has no fingerprint subscriber yet, so
the table reaches it by `pio run -e esp32-s3 -t uploadfs` from firmware/.
Either way the two implementations must agree byte for byte, or the gate
never opens; a literal on each side is what makes a drift in either fail.
"""

from pathlib import Path

from smartwatt_analysis.fingerprints import read_fingerprints

from smartwatt_server.publish_fingerprints import build_fingerprints_payload

GOLDEN = Path(__file__).resolve().parents[1] / "test" / "fixtures" / "fingerprints-golden.csv"

#: Must equal the literal in firmware/test/test_fingerprint_id.
GOLDEN_ID = "3853d83d"


def test_golden_fingerprint_id_matches_the_firmware():
    rows = read_fingerprints(GOLDEN)
    assert len(rows) == 4
    assert build_fingerprints_payload(rows)["fingerprint_id"] == GOLDEN_ID
