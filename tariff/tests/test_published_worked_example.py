"""US29: the billing engine must reproduce Sarawak Energy's own published
worked example exactly, so an evaluator can verify the arithmetic rather
than trust it.

BLOCKED: the worked example itself is not present in PRD.md, SmartWatt.md
or Problem Statement.md. The harness is built; supplying the figures is a
one-file change, not a redesign.

To unblock:
  1. Obtain Sarawak Energy's published worked example (a sample bill
     showing consumption, rate, energy charge, service tax and total).
  2. Fill in WORKED_EXAMPLE below.
  3. Delete the skip marker.

Every other test in this package derives from PRD.md's own numbers and is
not blocked by this.
"""

from datetime import date
from decimal import Decimal

import pytest

from smartwatt_tariff.bill import evaluate

PENDING_SOURCE = True

WORKED_EXAMPLE = {
    # "kwh": Decimal("..."),
    # "on": date(...),
    # "rate_sen": Decimal("..."),
    # "energy": Decimal("..."),
    # "service_tax": Decimal("..."),
    # "total": Decimal("..."),
}


@pytest.mark.skipif(
    PENDING_SOURCE,
    reason="PENDING_SOURCE: Sarawak Energy's published worked example is not "
           "available in any project document. See module docstring to unblock.",
)
def test_reproduces_the_published_worked_example():
    bill = evaluate(WORKED_EXAMPLE["kwh"], WORKED_EXAMPLE["on"])
    assert bill.rate_sen == WORKED_EXAMPLE["rate_sen"]
    assert bill.display()["energy"] == str(WORKED_EXAMPLE["energy"])
    assert bill.display()["service_tax"] == str(WORKED_EXAMPLE["service_tax"])
    assert bill.display()["total"] == str(WORKED_EXAMPLE["total"])
