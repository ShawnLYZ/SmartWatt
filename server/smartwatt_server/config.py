"""Runtime configuration.

The broker address is an IP LITERAL. Nothing in this system resolves a
hostname, so an mDNS failure at a venue cannot break it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

#: firmware/data/ is the source directory of the device's LittleFS image
#: (platformio.ini's board_build.filesystem), so the file the wizard writes
#: is the file `pio run -t uploadfs` puts on the device. Anchored to the
#: repository rather than the working directory.
DEFAULT_FINGERPRINTS = PACKAGE_DIR.parents[1] / "firmware" / "data" / "fingerprints.csv"

#: The household's own appliance list (appliances.py), at the repository
#: root where the person setting SmartWatt up will find it.
DEFAULT_APPLIANCES = PACKAGE_DIR.parents[1] / "appliances.toml"


@dataclass(frozen=True, slots=True)
class Settings:
    broker_ip: str
    broker_port: int
    db_path: Path
    #: 1 Hz telemetry retained this long, then rolled to one-minute and deleted.
    hz_retention_s: float
    rollup_interval_s: float
    static_dir: Path
    #: True selects rules.DEMO_DEFAULTS over rules.SHIPPED_DEFAULTS.
    #: Off by default so a demonstration-length threshold (seconds, not
    #: hours) is never the one a real deployment runs with by accident --
    #: it must be turned on deliberately, the same way a demo scenario is
    #: chosen deliberately. Without it, Task 5's Step 6 acceptance gate
    #: cannot run at all: SHIPPED_DEFAULTS' 4 h left_on_seconds cannot
    #: complete inside a manual demonstration.
    rules_demo: bool = False
    #: The classes the setup wizard captures and verifies, in order. None,
    #: the default, means the appliances marked `train = true` in the
    #: appliance list; SMARTWATT_TRAINED_CLASSES overrides that.
    trained_classes: tuple[str, ...] | None = None
    #: The training file the wizard appends to and pushes from.
    fingerprints_path: Path = DEFAULT_FINGERPRINTS
    #: The household's appliance list, read once at startup.
    appliances_path: Path = DEFAULT_APPLIANCES


def settings() -> Settings:
    return Settings(
        broker_ip=os.environ.get("SMARTWATT_BROKER", "127.0.0.1"),
        broker_port=int(os.environ.get("SMARTWATT_BROKER_PORT", "1883")),
        db_path=Path(os.environ.get("SMARTWATT_DB", "smartwatt.db")),
        # Overridable so the manual acceptance gate is runnable. The
        # retention loop only rolls rows OLDER than hz_retention_s, so at
        # the 48 h default a one-hour soak from a fresh database produces
        # zero telemetry_1min rows and the operator would have to edit
        # this file to check "~60 rows" at all.
        hz_retention_s=float(
            os.environ.get("SMARTWATT_HZ_RETENTION_S", 48 * 3600.0)
        ),
        rollup_interval_s=float(
            os.environ.get("SMARTWATT_ROLLUP_INTERVAL_S", 300.0)
        ),
        static_dir=PACKAGE_DIR.parent / "static",
        # SMARTWATT_RULES_DEMO=1 to select the demo thresholds; unset (or
        # anything else) keeps the shipped ones. Matches the existing
        # SMARTWATT_* convention above rather than inventing a config file.
        rules_demo=os.environ.get("SMARTWATT_RULES_DEMO") == "1",
        # Comma-separated; blanks around and between names are ignored.
        # Unset leaves the choice to the appliance list's `train` flags.
        trained_classes=_trained_classes(os.environ.get("SMARTWATT_TRAINED_CLASSES")),
        fingerprints_path=Path(
            os.environ.get("SMARTWATT_FINGERPRINTS", DEFAULT_FINGERPRINTS)
        ),
        appliances_path=Path(
            os.environ.get("SMARTWATT_APPLIANCES", DEFAULT_APPLIANCES)
        ),
    )


def _trained_classes(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(name.strip() for name in raw.split(",") if name.strip())
