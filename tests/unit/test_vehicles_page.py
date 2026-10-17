import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2

from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimits
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Control.HTTPControl import (
    ageText,
    addVehicleByVIN,
    canForgetVehicle,
    heldLimitText,
    providerLines,
    sourceName,
)

VIN = "5YJ3E1EA1KF000001"
THEME = Path(__file__).resolve().parents[2] / "lib/TWCManager/Control/themes/Default"


def env(master):
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(THEME)), autoescape=True)
    e.globals.update(
        navbarItem=lambda *a: "",
        checkForUpdates=lambda: False,
        ageText=ageText,
        heldLimitText=lambda v: heldLimitText(master, v),
        sourceName=sourceName,
        providerLines=lambda v: providerLines(master, v),
        registry=master.vehicleRegistry,
        vehicles=master.vehicleRegistry.vehicles,
        canForget=lambda vin: canForgetVehicle(master, vin),
    )
    return e


def master():
    reg = VehicleRegistry({})
    v = reg.getOrCreate(VIN, "TWC")
    v.observe("name", "TestCar", "TeslaAPI")
    v.claim("TeslaBLE", True, "paired")
    reg.beginPlugSession("8244")
    m = SimpleNamespace(
        vehicleRegistry=reg,
        version="1.4.0",
        modules={},
        settingsLock=threading.RLock(),
        getModuleByName=lambda name: None,
        queue_background_task=lambda *a, **k: None,
        settings={
            "Vehicles": {VIN: {"chargeSessions": 3, "totalkWh": 12}},
            "VehicleGroups": {},
        },
    )
    m.heldLimits = HeldLimits(m)
    m.heldLimits.set(VIN, 90, 80)
    return m


def render(m, error=""):
    return (
        env(m)
        .get_template("vehicles.html.j2")
        .render(
            master=m,
            url=SimpleNamespace(path="/vehicles", query=""),
            addVehicleError=error,
        )
    )


def test_held_limit_reads_the_vin_store_after_adoption():
    m = master()
    vin2 = "5YJ3E1EA1KF000002"
    v2 = m.vehicleRegistry.getOrCreate(vin2, "TeslaAPI")
    m.settings["chargeLimits"]["42"] = [85, 60]
    m.heldLimits.load()
    # a legacy numeric-ID entry shows once a vehicle list resolves it
    assert heldLimitText(m, v2) == "—"
    m.heldLimits.resolveLegacyIds({42: vin2})
    assert heldLimitText(m, v2) == "60% (car's own limit: 85%, restored when released)"
    assert "80% (car's own limit: 90%" in heldLimitText(m, m.vehicleRegistry.get(VIN))
    m.heldLimits.remove(VIN)
    assert heldLimitText(m, m.vehicleRegistry.get(VIN)) == "—"


def test_list_renders_registry_rows():
    m = master()
    html = render(m)
    assert f"/vehicleDetail/{VIN}" in html and "TestCar" in html
    assert "BLE: paired" in html and "TeslaBLE" not in html
    assert "80% (car&#39;s own limit: 90%, restored when released)" in html
    assert "Charge limit set by TWCManager" in html and "Held limit" not in html
    assert "Unknown car on TWC 8244" in html and "Charge history" in html
    assert 'action="/vehicle/add"' in html


def test_names_and_error_are_escaped():
    m = master()
    m.vehicleRegistry.get(VIN).observe("name", "<script>x</script>", "TeslaAPI")
    html = render(m, error="<b>bad</b>")
    assert "<script>x</script>" not in html and "<b>bad</b>" not in html
    assert "&lt;b&gt;bad&lt;/b&gt;" in html


def test_age_text():
    assert ageText(None) == "never" and ageText(30) == "30 s ago"
    assert ageText(90) == "1 min ago" and ageText(3600 * 5) == "5 h ago"
    assert ageText(86400 * 3) == "3 d ago"


def test_add_vehicle_by_vin():
    m = master()
    m.queue_background_task = Mock()
    ble = Mock()
    m.getModuleByName = lambda n: ble if n == "TeslaBLE" else None
    assert addVehicleByVIN(m, "5yj3e1ea1kf000002") == "/vehicleDetail/5YJ3E1EA1KF000002"
    m.queue_background_task.assert_called_once_with({"cmd": "saveSettings"})
    assert m.settings["Vehicles"]["5YJ3E1EA1KF000002"]["chargeSessions"] == 0
    ble.scanForVehicles.assert_called_once()
    bad = addVehicleByVIN(m, "5YJ3E1EA1KF00000I")
    assert bad.startswith("/vehicles?error=") and "without+I%2C+O+or+Q" in bad
    addVehicleByVIN(m, "5YJ3E1EA1KF000002")
    assert (
        len([v for v in m.vehicleRegistry.vehicles() if v.vin == "5YJ3E1EA1KF000002"])
        == 1
    )


def test_add_vehicle_numeric_input_does_not_crash():
    m = master()
    m.queue_background_task = Mock()
    # getFieldValue returns an int for an all-digit string
    assert addVehicleByVIN(m, 12345).startswith("/vehicles?error=")
    # 17 digits is a well-formed VIN
    assert addVehicleByVIN(m, 12345678901234567) == "/vehicleDetail/12345678901234567"


def test_add_known_vin_marks_user_asserted_without_duplicate():
    m = master()
    m.queue_background_task = Mock()
    assert addVehicleByVIN(m, VIN) == "/vehicleDetail/" + VIN
    assert m.vehicleRegistry.get(VIN).userAsserted
    assert len([v for v in m.vehicleRegistry.vehicles() if v.vin == VIN]) == 1


def test_add_vin_dedupes_settings_case_insensitively():
    m = master()
    m.queue_background_task = Mock()
    lower = "5yj3e1ea1kf000003"
    m.settings["Vehicles"][lower] = {"chargeSessions": 7}
    addVehicleByVIN(m, lower.upper())
    assert list(k for k in m.settings["Vehicles"] if k.upper() == lower.upper()) == [
        lower
    ]
    assert m.settings["Vehicles"][lower]["chargeSessions"] == 7


def test_providers_one_per_line_with_source_names_and_no_tier():
    m = master()
    v = m.vehicleRegistry.get(VIN)
    v.claim("TeslaMateVehicle", False, "reports", keepsAlive=False)
    v.claim("Gen3TWC", True, "")
    v.observe("chargeLimit", 1, "FleetTelemetryMQTT")
    html = render(m)
    assert "BLE: paired<br>TeslaMate: reports, read-only<br>Gen3 Wall Connector: commands" in html
    assert "via Fleet Telemetry" in html
    assert "Tier" not in html and "Gen3TWC" not in html and "TeslaMateVehicle" not in html
    assert sourceName("SomethingNew") == "SomethingNew"
    assert sourceName("Gen2TWC") == "TWC" and sourceName("TeslaBLE") == "BLE"


def test_provider_capability_text_for_tesla_cloud_and_ble():
    m = master()
    v = m.vehicleRegistry.get(VIN)
    api = Mock()
    api.ownerReadOnlyReason.return_value = "car requires signed commands"
    api.fleetReadOnlyReason.return_value = "token lacks command scope"
    m.getModuleByName = lambda n: api if n == "TeslaAPI" else None
    v.claim("OwnerAPI", False, "listed")
    v.claim("FleetAPI", False, "listed")
    v.claim("TeslaBLE", False, "unproven", keepsAlive=False)
    v.claim("HomeAssistant", True, "entities")
    assert sorted(providerLines(m, v)) == [
        "BLE: unproven",
        "Fleet API: read-only (token lacks command scope)",
        "Home Assistant: entities, commands",
        "Owner API: read-only (car requires signed commands)",
    ]
    api.ownerReadOnlyReason.return_value = None
    v.claim("OwnerAPI", True, "listed")
    v.claim("FleetAPI", True, "listed")
    v.claim("TeslaBLE", False, "paired")
    assert sorted(providerLines(m, v))[:3] == [
        "BLE: paired, read-only",
        "Fleet API: commands",
        "Home Assistant: entities, commands",
    ] and "Owner API: commands" in providerLines(m, v)
