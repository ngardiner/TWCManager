import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2

from TWCManager.Vehicle.Reconcile.Desired import DesiredStore
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimits
from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Control import Views
from TWCManager.Control.HTTPControl import (
    addVehicleByVIN,
    addVehicleGroup,
    canForgetVehicle,
)
from TWCManager.Control.Views import pageFingerprint, vehiclesView

VIN = "5YJ3E1EA1KF000001"
THEME = Path(__file__).resolve().parents[2] / "lib/TWCManager/Control/themes/Default"


def env(master):
    e = jinja2.Environment(loader=jinja2.FileSystemLoader(str(THEME)), autoescape=True)
    e.globals.update(
        navbarItem=lambda *a: "",
        checkForUpdates=lambda: False,
        registry=master.vehicleRegistry,
        vehicles=master.vehicleRegistry.vehicles,
        canForget=lambda vin: canForgetVehicle(master, vin),
        fingerprint=lambda page, vin=None: pageFingerprint(master, page, vin),
        spendShown=lambda: Views.spendShown(master),
    )
    return e


def master():
    reg = VehicleRegistry({})
    v = reg.getOrCreate(VIN, "TWC")
    v.observe("name", "TestCar", "OwnerAPI")
    v.claim("TeslaBLE", True, "paired")
    reg.beginPlugSession("8244")
    m = SimpleNamespace(
        vehicleRegistry=reg,
        watch=None,
        watchBudget=Mock(tokens=lambda: 2.5),
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
    m.desired = DesiredStore()
    m.reconciler = VehicleReconciler(m)
    return m


def render(m, error="", groupError=""):
    return (
        env(m)
        .get_template("vehicles.html.j2")
        .render(
            master=m,
            url=SimpleNamespace(path="/vehicles", query=""),
            addVehicleError=error,
            addGroupError=groupError,
        )
    )


def heldLimit(m, key):
    return next(e for e in vehiclesView(m)["vehicles"] if e["key"] == key)["heldLimit"]


def test_held_limit_reads_the_vin_store_after_adoption():
    m = master()
    vin2 = "5YJ3E1EA1KF000002"
    m.vehicleRegistry.getOrCreate(vin2, "OwnerAPI")
    m.settings["chargeLimits"]["42"] = [85, 60]
    m.heldLimits.load()
    # a legacy numeric-ID entry shows once a vehicle list resolves it
    assert heldLimit(m, vin2) is None
    m.heldLimits.resolveLegacyIds({42: vin2})
    assert heldLimit(m, vin2) == {"held": 60, "outside": 85}
    assert heldLimit(m, VIN) == {"held": 80, "outside": 90}
    m.heldLimits.remove(VIN)
    assert heldLimit(m, VIN) is None


def test_list_renders_registry_rows():
    m = master()
    html = render(m)
    assert f"/vehicleDetail/{VIN}" in html
    assert "Unknown car on TWC 8244" in html and "Charge history" in html
    assert 'action="/vehicle/add"' in html
    assert 'data-field="vehicles.0.presence"' in html
    for th in ("Presence", "Watch", "Providers", "Last seen"):
        assert f"<th>{th}</th>" in html
    assert 'data-field="vehicles.1.presence"' in html  # anon:8244, by position
    assert 'data-field="vehicles.0.name"' in html
    assert 'data-field="vehicles.0.watch" data-format="watchLine"' in html
    assert 'data-field="vehicles.0" data-format="lastSeen"' in html
    assert 'data-field="vehicles.0.heldLimit" data-format="heldLimit"' in html
    providers = 'data-field="vehicles.0.providers" data-format="providerLines"'
    assert providers in html and "white-space: pre-line" in html
    assert 'data-field="history.0.chargeSessions"' in html
    assert 'data-field="history.0.totalkWh"' in html


def test_names_and_error_are_escaped():
    m = master()
    m.vehicleRegistry.get(VIN).observe("name", "<script>x</script>", "OwnerAPI")
    html = render(m, error="<b>bad</b>")
    assert "<script>x</script>" not in html and "<b>bad</b>" not in html
    assert "&lt;b&gt;bad&lt;/b&gt;" in html
    # names arrive by the API and are written as text
    assert "TestCar" not in render(master())


def test_page_carries_fingerprint_and_starts_live():
    m = master()
    html = render(m)
    assert "Live.start" in html and pageFingerprint(m, "vehicles") in html
    assert "/static/js/format.js" in html and "/static/js/live.js" in html
    assert '"/api/getVehicles"' in html and "interval: 5000" in html


def test_display_helpers_left_the_lib():
    import TWCManager.Control.HTTPControl as H

    for name in (
        "ageText",
        "sourceName",
        "providerLines",
        "heldLimitText",
        "watchLine",
        "spendLines",
        "carSpend",
        "PRESENCE_TEXT",
        "pendingCommands",
        "_usd",
    ):
        assert not hasattr(H, name)


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


def test_add_vehicle_group():
    m = master()
    m.queue_background_task = Mock()
    assert addVehicleGroup(m, " Guests ", "Visitors' cars") == "/vehicles"
    assert m.settings["VehicleGroups"]["Guests"] == {
        "Description": "Visitors' cars",
        "Built-in": 0,
        "Members": [],
    }
    m.queue_background_task.assert_called_once_with({"cmd": "saveSettings"})
    # a numeric name arrives from the form as an int
    assert addVehicleGroup(m, 2024, None) == "/vehicles"
    assert "2024" in m.settings["VehicleGroups"]
    assert render(m).count('href="/vehicles/deleteGroup/Guests"') == 1
    # Delete's link must carry a name a URL would otherwise cut short
    addVehicleGroup(m, "Mine? #1", "")
    assert 'href="/vehicles/deleteGroup/Mine%3F%20%231"' in render(m)


def test_add_vehicle_group_refuses_bad_names():
    m = master()
    m.settings["VehicleGroups"]["Guests"] = {"Members": ["X"]}
    for name in ("", "  ", None, "a/b", "Guests"):
        location = addVehicleGroup(m, name, "")
        assert location.startswith("/vehicles?groupError=")
    assert m.settings["VehicleGroups"]["Guests"] == {"Members": ["X"]}
    assert len(m.settings["VehicleGroups"]) == 1
    html = render(m, groupError="<b>bad</b>")
    assert "&lt;b&gt;bad&lt;/b&gt;" in html
    assert 'action="/vehicle/addGroup"' in html
    assert 'name="name"' in html and 'name="description"' in html


def spending(m, fleet=True, telemetry=False, clock=None):
    from TWCManager.Vehicle.FleetSpend import FleetSpend

    slot = SimpleNamespace(accessToken="tok" if fleet else "")
    mods = {"TeslaAPI": SimpleNamespace(fleetSlot=slot)}
    if telemetry:
        mods["FleetTelemetryMQTT"] = object()
    m.getModuleByName = mods.get
    m.fleetSpend = FleetSpend(
        clock=clock or (lambda: time.mktime((2026, 11, 16, 0, 0, 0, 0, 0, -1)))
    )
    return m.fleetSpend


def test_no_fleet_no_spend_shown():
    m = master()
    spending(m, fleet=False)
    html = render(m)
    assert "Fleet this month" not in html and "spendCell" not in html


def test_spend_lines_and_column_are_hooks():
    m = master()
    spending(m)
    html = render(m)
    assert "<th>Fleet this month</th>" in html
    assert 'data-field="vehicles.0" data-format="spendCell"' in html
    assert 'data-field="spend" data-format="fleetLine" data-show="spend.fleet"' in html
    assert (
        'data-field="spend" data-format="telemetryLine" data-show="spend.telemetry"'
        in html
    )
    # no formatted amount on the server
    assert "$" not in html.split("<body>")[1]
    spending(m, fleet=False, telemetry=True)
    assert "<th>Fleet this month</th>" in render(m)
