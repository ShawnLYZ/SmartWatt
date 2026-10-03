"""Settings has no coverage elsewhere in S3; these are its only guards."""

from pathlib import Path

from smartwatt_server.config import PACKAGE_DIR, settings


def test_static_dir_resolves_outside_the_package():
    """The brief's own snippet got this line wrong once (PACKAGE_DIR
    instead of PACKAGE_DIR.parent); both produce a path ending in
    ``static``, so the parent directory is what actually distinguishes
    them.
    """
    result = settings().static_dir
    assert result == PACKAGE_DIR.parent / "static"
    assert result.name == "static"
    assert result.parent.name == "server"


def test_default_broker_is_an_ip_literal(monkeypatch):
    """Non-negotiable: no hostname resolution, anywhere."""
    monkeypatch.delenv("SMARTWATT_BROKER", raising=False)
    monkeypatch.delenv("SMARTWATT_BROKER_PORT", raising=False)
    s = settings()
    assert s.broker_ip == "127.0.0.1"
    assert s.broker_port == 1883


def test_environment_overrides_are_read(monkeypatch):
    monkeypatch.setenv("SMARTWATT_BROKER", "10.0.0.5")
    monkeypatch.setenv("SMARTWATT_BROKER_PORT", "8883")
    monkeypatch.setenv("SMARTWATT_DB", "custom.db")
    monkeypatch.setenv("SMARTWATT_HZ_RETENTION_S", "3600")
    monkeypatch.setenv("SMARTWATT_ROLLUP_INTERVAL_S", "10")
    s = settings()
    assert s.broker_ip == "10.0.0.5"
    assert s.broker_port == 8883
    assert isinstance(s.broker_port, int)
    assert s.db_path == Path("custom.db")
    assert s.hz_retention_s == 3600.0
    assert s.rollup_interval_s == 10.0


def test_retention_defaults(monkeypatch):
    """48 h and five minutes, and both overridable.

    The manual acceptance gate asks for a one-hour soak and then "verify
    telemetry_1min has ~60 rows", which the 48 h default makes impossible
    from a fresh database: the retention loop only rolls rows older than
    the window. Without an override the operator would have to edit
    source to run the gate at all.
    """
    monkeypatch.delenv("SMARTWATT_HZ_RETENTION_S", raising=False)
    monkeypatch.delenv("SMARTWATT_ROLLUP_INTERVAL_S", raising=False)
    s = settings()
    assert s.hz_retention_s == 48 * 3600.0
    assert s.rollup_interval_s == 300.0


def test_rules_demo_defaults_to_false(monkeypatch):
    """A demonstration-length threshold must be turned on deliberately,
    never be what an unconfigured deployment runs with by accident."""
    monkeypatch.delenv("SMARTWATT_RULES_DEMO", raising=False)
    assert settings().rules_demo is False


def test_rules_demo_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SMARTWATT_RULES_DEMO", "1")
    assert settings().rules_demo is True


def test_rules_demo_only_the_exact_value_1_enables_it(monkeypatch):
    """Guards the parsing itself: a stray "0", "false" or empty string
    must not be read as truthy, unlike Python's own bare truthiness of a
    non-empty string (`bool("0")` is True, `bool("false")` is True)."""
    monkeypatch.setenv("SMARTWATT_RULES_DEMO", "0")
    assert settings().rules_demo is False
    monkeypatch.setenv("SMARTWATT_RULES_DEMO", "false")
    assert settings().rules_demo is False


def test_trained_classes_default_to_the_appliance_list(monkeypatch):
    """Unset, the wizard trains whatever appliances.toml marks `train = true`
    (create_app resolves None against the loaded list). No appliance names
    are built in."""
    monkeypatch.delenv("SMARTWATT_TRAINED_CLASSES", raising=False)
    assert settings().trained_classes is None


def test_trained_classes_are_read_comma_separated(monkeypatch):
    monkeypatch.setenv("SMARTWATT_TRAINED_CLASSES", " kettle, desk_fan ,,led_bulb")
    assert settings().trained_classes == ("kettle", "desk_fan", "led_bulb")


def test_fingerprints_path_defaults_to_the_firmware_data_image(monkeypatch):
    """firmware/data/ is the LittleFS image source, so the file the wizard
    writes is the file `pio run -t uploadfs` puts on the device."""
    monkeypatch.delenv("SMARTWATT_FINGERPRINTS", raising=False)
    path = settings().fingerprints_path
    assert path == PACKAGE_DIR.parents[1] / "firmware" / "data" / "fingerprints.csv"


def test_fingerprints_path_is_overridable(monkeypatch):
    monkeypatch.setenv("SMARTWATT_FINGERPRINTS", "elsewhere/f.csv")
    assert settings().fingerprints_path == Path("elsewhere/f.csv")


def test_appliance_list_defaults_to_the_repository_root(monkeypatch):
    """The file the README tells people to edit, next to README.md --
    anchored to the repository, not to the working directory the server
    happens to start in."""
    monkeypatch.delenv("SMARTWATT_APPLIANCES", raising=False)
    assert settings().appliances_path == PACKAGE_DIR.parents[1] / "appliances.toml"


def test_appliance_list_is_overridable(monkeypatch):
    monkeypatch.setenv("SMARTWATT_APPLIANCES", "elsewhere/mine.toml")
    assert settings().appliances_path == Path("elsewhere/mine.toml")
