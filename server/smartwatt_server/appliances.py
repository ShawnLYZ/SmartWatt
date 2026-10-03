"""The household's own appliance list: appliances.toml.

A household changes one plain-text file, at the repository root, to use
SmartWatt with its own appliances. The server reads it once at startup:

  * the registry rows (name, plug, protected, heating), which are the safety
    gate's inputs, installed with `Registry.sync()`;
  * which appliances the setup wizard trains;
  * which appliances the two automatic rules (`left_on`, `standby_draw`)
    watch.

People edit the file by hand, often without having seen TOML before. So every
refusal says where the problem is (file, appliance, key), what is wrong, and
what a correct line looks like, and all the problems in the file are reported
together instead of one per restart.

A file that cannot be used stops the server from starting. Its protected and
heating flags feed the gate, and the server must not switch relays from a
registry built out of a misread file.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .registry import PSEUDO_PREFIXES, Device

#: The device's own label rule (firmware is_emittable_label, and
#: FingerprintRow's 24-byte label): a lowercase letter first, then lowercase
#: letters, digits and underscores, at most 23 characters. The id is the
#: label the device classifies with, so an id the device would drop could
#: never be recognised.
_ID = re.compile(r"[a-z][a-z0-9_]*")
MAX_ID = 23
#: A Tasmota Topic becomes one segment of the plug's MQTT topics, so it
#: cannot contain a slash, a wildcard (+ or #) or a space.
_PLUG = re.compile(r"[A-Za-z0-9_-]+")

_REQUIRED = ("id", "name", "protected", "heating")
_OPTIONAL = {"plug": "", "train": True, "left_on_rule": False, "standby_rule": False}
_KEYS = _REQUIRED + tuple(_OPTIONAL)
_FLAGS = ("protected", "heating", "train", "left_on_rule", "standby_rule")


class ApplianceFileError(ValueError):
    """appliances.toml cannot be used. The message is written for the person
    who edits the file, not for a developer."""


@dataclass(frozen=True, slots=True)
class Catalogue:
    """Everything the server takes from appliances.toml, in file order."""

    devices: tuple[Device, ...]
    trained: tuple[str, ...]
    left_on_targets: tuple[str, ...]
    standby_targets: tuple[str, ...]


def _suggest(word: str, choices: tuple[str, ...]) -> str:
    close = difflib.get_close_matches(word, choices, n=1)
    return f' Did you mean "{close[0]}"?' if close else ""


def _check_appliance(
    number: int, block: object, problems: list[str]
) -> tuple[Device, dict[str, bool]] | None:
    """One [[appliance]] block, or None after appending why it is unusable."""
    if not isinstance(block, dict):
        problems.append(f"appliance #{number} is not a block of settings.")
        return None

    raw_id = block.get("id")
    where = f'appliance #{number} ("{raw_id}")' if isinstance(raw_id, str) else (
        f"appliance #{number}"
    )
    before = len(problems)

    for key in block:
        if key not in _KEYS:
            problems.append(
                f'{where} has a setting called "{key}", which SmartWatt does not '
                f"know.{_suggest(key, _KEYS)} The settings are: {', '.join(_KEYS)}."
            )
    for key in _REQUIRED:
        if key not in block:
            hint = {
                "id": 'for example  id = "rice_cooker"',
                "name": 'for example  name = "Rice cooker"',
                "protected": "protected = true  if SmartWatt must never switch it off, "
                "otherwise  protected = false",
                "heating": "heating = true  for a kettle, iron or heater (SmartWatt may "
                "switch it off but never on), otherwise  heating = false",
            }[key]
            problems.append(f'{where} is missing its "{key}" line: {hint}.')

    values = {**_OPTIONAL, **block}

    for key in _FLAGS:
        if key in block and not isinstance(block[key], bool):
            problems.append(
                f'{where}: {key} = {block[key]!r} must be  true  or  false, '
                "lowercase and without quote marks."
            )

    appliance_id = values.get("id")
    if "id" in block:
        if not isinstance(appliance_id, str):
            problems.append(f'{where}: id must be text in quote marks, e.g. id = "fan".')
        elif not _ID.fullmatch(appliance_id):
            problems.append(
                f'{where}: id "{appliance_id}" may use only lowercase letters a-z, '
                "digits 0-9 and underscores _, and must start with a letter. "
                'No spaces or capitals: for example "rice_cooker".'
            )
        elif len(appliance_id) > MAX_ID:
            problems.append(
                f'{where}: id "{appliance_id}" is {len(appliance_id)} characters; '
                f"the device holds at most {MAX_ID}. Shorten it."
            )
        elif appliance_id.startswith(PSEUDO_PREFIXES):
            problems.append(
                f'{where}: ids starting with "unknown_" are reserved for loads '
                "SmartWatt has not been taught. Pick another id."
            )

    name = values.get("name")
    if "name" in block and (not isinstance(name, str) or not name.strip()):
        problems.append(
            f'{where}: name must be text in quote marks, e.g. name = "Rice cooker".'
        )

    plug = values["plug"]
    if not isinstance(plug, str):
        problems.append(
            f'{where}: plug must be text in quote marks, e.g. plug = "plug_fan", '
            'or plug = "" if it is not on a smart plug.'
        )
    elif plug and not _PLUG.fullmatch(plug):
        problems.append(
            f'{where}: plug "{plug}" must be the smart plug\'s Tasmota Topic, using '
            "only letters, digits, _ and -. No spaces, slashes, + or #."
        )

    if values.get("protected") is True and (
        values.get("left_on_rule") is True or values.get("standby_rule") is True
    ):
        problems.append(
            f"{where} is protected, so SmartWatt will never switch it off, but it "
            "also has an automatic switch-off rule turned on. Set left_on_rule and "
            "standby_rule to false, or protected to false."
        )

    if len(problems) > before:
        return None
    device = Device(
        appliance_id=appliance_id,
        display_name=name.strip(),
        plug_device=plug or None,
        protected=values["protected"],
        heating=values["heating"],
    )
    return device, {key: values[key] for key in ("train", "left_on_rule", "standby_rule")}


def load_appliances(path: Path) -> Catalogue:
    """Reads and checks appliances.toml. Raises ApplianceFileError, listing
    every problem found, rather than return a partly-read household."""
    path = Path(path)
    try:
        # utf-8-sig: some editors put a byte-order mark at the start of a
        # UTF-8 file, and the TOML parser would reject it.
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ApplianceFileError(
            f"The appliance list {path} does not exist. SmartWatt needs it to know "
            "your appliances: restore appliances.toml from the download, or set "
            "SMARTWATT_APPLIANCES to where yours is."
        ) from None
    except UnicodeDecodeError:
        raise ApplianceFileError(
            f"{path.name} is not saved as UTF-8 text. In Notepad: File > Save as, "
            'and choose "UTF-8" under Encoding.'
        ) from None

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ApplianceFileError(
            f"{path.name} has a typing mistake: {exc}. Look at that line: text "
            'values need quote marks (name = "Fan"), true and false are lowercase '
            "with no quote marks, and each block starts with [[appliance]]."
        ) from None

    problems: list[str] = []
    for key in data:
        if key != "appliance":
            problems.append(
                f'{path.name} has a section called "{key}", which SmartWatt does '
                f'not know.{_suggest(key, ("appliance",))} Every block starts with '
                "[[appliance]]."
            )
    blocks = data.get("appliance", [])
    if not isinstance(blocks, list):
        problems.append(
            f"{path.name}: start each appliance with a line reading [[appliance]], "
            "with TWO square brackets on each side."
        )
        blocks = []
    elif not blocks and not problems:
        problems.append(
            f"{path.name} lists no appliances. Add at least one [[appliance]] block."
        )

    checked = [_check_appliance(n, block, problems) for n, block in enumerate(blocks, 1)]
    usable = [entry for entry in checked if entry is not None]

    seen_ids: dict[str, int] = {}
    seen_plugs: dict[str, str] = {}
    for device, _ in usable:
        seen_ids[device.appliance_id] = seen_ids.get(device.appliance_id, 0) + 1
        if device.plug_device is None:
            continue
        owner = seen_plugs.setdefault(device.plug_device, device.appliance_id)
        if owner != device.appliance_id:
            problems.append(
                f'"{owner}" and "{device.appliance_id}" both use plug '
                f'"{device.plug_device}". One smart plug switches one appliance.'
            )
    for appliance_id, count in seen_ids.items():
        if count > 1:
            problems.append(
                f'The id "{appliance_id}" is used {count} times. Each appliance '
                "needs its own id."
            )

    if problems:
        raise ApplianceFileError(
            f"{path} cannot be used ({len(problems)} problem"
            f"{'s' if len(problems) > 1 else ''}):\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )

    return Catalogue(
        devices=tuple(device for device, _ in usable),
        trained=tuple(d.appliance_id for d, flags in usable if flags["train"]),
        left_on_targets=tuple(
            d.appliance_id for d, flags in usable if flags["left_on_rule"]
        ),
        standby_targets=tuple(
            d.appliance_id for d, flags in usable if flags["standby_rule"]
        ),
    )
