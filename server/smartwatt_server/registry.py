"""Device registry.

The gate's only input beyond the command itself. `protected` and `heating`
live here.

Pseudo-appliances (`__residual__`, `unknown_N`) appear in the ledger and are
structurally uncontrollable - but NOT because anything here refuses them.
Nothing does: `seed_defaults()` simply never inserts one, and
`upsert_appliance` would accept a row for `unknown_3` as readily as for the
kettle. Their uncontrollability is enforced one layer up, and by a stronger
mechanism than a missing row: `Gate.send()` runs `is_pseudo()` FIRST
(gate.py, check 1), before it ever looks the appliance up, so a pseudo id is
refused `REFUSED_NOT_CONTROLLABLE` whether or not a row exists for it - even
one with a real plug attached. `RulesEngine._warn()` makes the same check,
independently, before it will create a pending cut.

That ordering is the whole point, and it is why the check must stay where it
is: a guarantee that depended on the table not containing something would be
undone the first time anything wrote that row.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Store

#: Ids the tracker generates for detected-but-unnamed loads.
PSEUDO_PREFIXES = ("unknown_",)
#: Ledger-only ids that are not appliances at all.
PSEUDO_IDS = ("__residual__",)


@dataclass(frozen=True, slots=True)
class Device:
    appliance_id: str
    display_name: str
    plug_device: str | None
    protected: bool
    heating: bool


#: The EXAMPLE household: the five appliance classes the PRD specifies, which
#: the test suite and the simulator's scenarios run against. A real
#: installation does not use this tuple. The server reads the household's own
#: list from appliances.toml at the repository root (appliances.py) and
#: installs it with `sync()`. That file ships holding these same five as a
#: template, and server/tests/example_appliances.toml is pinned to them.
_DEFAULTS: tuple[Device, ...] = (
    # Heating: the system may de-energise it, never energise it.
    Device("kettle", "Kettle", "plug_kettle", protected=False, heating=True),
    Device("desk_fan", "Desk fan", None, protected=False, heating=False),
    # The left_on rule's target, and the appliance the tariff-cliff worked
    # example already names as the one pushing the household over 400 kWh.
    Device("incandescent_lamp", "Incandescent lamp", "plug_lamp",
           protected=False, heating=False),
    Device("led_bulb", "LED bulb", "plug_led", protected=False, heating=False),
    # PROTECTED. A real device with a real reason.
    Device("laptop_charger", "Laptop charger", "plug_laptop",
           protected=True, heating=False),
)


class Registry:
    def __init__(self, store: Store) -> None:
        self._store = store

    def seed_defaults(self) -> None:
        for device in _DEFAULTS:
            self._store.upsert_appliance(
                appliance_id=device.appliance_id,
                display_name=device.display_name,
                protected=device.protected,
                heating=device.heating,
                plug_device=device.plug_device,
            )

    def sync(self, devices: tuple[Device, ...]) -> None:
        """Make the registry hold exactly ``devices``: the household's list.

        Unlike `seed_defaults()`, this REMOVES appliances that are no longer
        listed. Protected and heating are the gate's only inputs beyond the
        command itself, so a row the owner deleted from appliances.toml must
        not stay switchable under the flags it used to have. One atomic
        replacement (Store.replace_appliances), under the store's write lock.
        """
        self._store.replace_appliances([
            {
                "appliance_id": device.appliance_id,
                "display_name": device.display_name,
                "protected": device.protected,
                "heating": device.heating,
                "plug_device": device.plug_device,
            }
            for device in devices
        ])

    @staticmethod
    def is_pseudo(appliance_id: str) -> bool:
        return appliance_id in PSEUDO_IDS or any(
            appliance_id.startswith(prefix) for prefix in PSEUDO_PREFIXES
        )

    def all(self) -> list[Device]:
        return [
            Device(
                appliance_id=row["id"],
                display_name=row["display_name"],
                plug_device=row["plug_device"],
                protected=bool(row["protected"]),
                heating=bool(row["heating"]),
            )
            for row in self._store.appliances()
        ]

    def get(self, appliance_id: str) -> Device | None:
        for device in self.all():
            if device.appliance_id == appliance_id:
                return device
        return None

    def by_plug(self, plug_device: str) -> Device | None:
        for device in self.all():
            if device.plug_device == plug_device:
                return device
        return None
