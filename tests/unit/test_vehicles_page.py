import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2
import pytest

from TWCManager.Vehicle.Presence import presence
from TWCManager.Vehicle.Reconcile.Desired import DesiredStore
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimits
from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Control.HTTPControl import (
    ageText,
    addVehicleByVIN,
    addVehicleGroup,
    canForgetVehicle,
    carSpend,
    heldLimitText,
    PRESENCE_TEXT,
    pendingCommands,
    providerLines,
    sourceName,
    spendLines,
    watchLine,
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
        watchLine=lambda v: watchLine(master, v),
        presence=lambda v: PRESENCE_TEXT[presence(master, v)],
        pendingCommands=lambda v: pendingCommands(master, v),
        registry=master.vehicleRegistry,
        vehicles=master.vehicleRegistry.vehicles,
        canForget=lambda vin: canForgetVehicle(master, vin),
        spendLines=lambda: spendLines(master),
        carSpend=lambda vin: carSpend(master, vin),
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


def test_held_limit_reads_the_vin_store_after_adoption():
    m = master()
    vin2 = "5YJ3E1EA1KF000002"
    v2 = m.vehicleRegistry.getOrCreate(vin2, "OwnerAPI")
    m.settings["chargeLimits"]["42"] = [85, 60]
    m.heldLimits.load()
    # a legacy numeric-ID entry shows once a vehicle list resolves it
    assert heldLimitText(m, v2) == "—"
    m.heldLimits.resolveLegacyIds({42: vin2})
    assert heldLimitText(m, v2) == "60% (85%)"
    assert heldLimitText(m, m.vehicleRegistry.get(VIN)) == "80% (90%)"
    m.heldLimits.remove(VIN)
    assert heldLimitText(m, m.vehicleRegistry.get(VIN)) == "—"


def test_list_renders_registry_rows():
    m = master()
    html = render(m)
    assert f"/vehicleDetail/{VIN}" in html and "TestCar" in html
    assert "BLE: paired" in html and "TeslaBLE" not in html
    assert cell(html, "Charge limit (car's own)") == "80% (90%)"
    assert "Charge limit (car's own)" in html and "set by TWCManager" not in html
    assert "Unknown car on TWC 8244" in html and "Charge history" in html
    assert 'action="/vehicle/add"' in html


def test_names_and_error_are_escaped():
    m = master()
    m.vehicleRegistry.get(VIN).observe("name", "<script>x</script>", "OwnerAPI")
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
    assert (
        "BLE: paired<br>TeslaMate: reports, read-only<br>Gen3 Wall Connector: commands"
        in html
    )
    assert "via Fleet Telemetry" in html
    assert (
        "Tier" not in html and "Gen3TWC" not in html and "TeslaMateVehicle" not in html
    )
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


@pytest.mark.parametrize("held", ["Owner", "Fleet"])
def test_a_cloud_api_without_its_token_is_not_listed(held):
    # The account listing claims the car for both APIs; without its token an
    # API can neither read nor command the car
    m = master()
    v = m.vehicleRegistry.get(VIN)
    api = Mock()
    api.ownerTokenHeld.return_value = held == "Owner"
    api.fleetTokenHeld.return_value = held == "Fleet"
    api.ownerReadOnlyReason.return_value = None
    m.getModuleByName = lambda n: api if n == "TeslaAPI" else None
    v.claim("OwnerAPI", held == "Owner", "listed")
    v.claim("FleetAPI", held == "Fleet", "listed")
    other = "Fleet" if held == "Owner" else "Owner"
    lines = providerLines(m, v)
    assert "%s API: commands" % held in lines
    assert not [line for line in lines if line.startswith(other)]


def cell(html, label):
    row = html[html.index("/vehicleDetail/" + VIN) :].split("</tr>")[0]
    return row.split(f'<td data-label="{label}">')[1].split("</td>")[0].strip()


def presenceCell(html):
    return cell(html, "Presence")


def test_presence_column_shows_each_state():
    m = master()
    v = m.vehicleRegistry.get(VIN)
    assert "<th>Presence</th>" in render(m)
    assert presenceCell(render(m)) == "Probably home"  # no evidence either way
    v.mark("plugIn", 100.0)
    assert presenceCell(render(m)) == "Home"
    v.mark("unplug", 200.0)
    assert presenceCell(render(m)) == "Away"
    # an unknown car on a TWC is here by definition
    anon = m.vehicleRegistry.get("anon:8244")
    assert PRESENCE_TEXT[presence(m, anon)] == "Home"


def watchCell(html):
    return cell(html, "Watch")


def test_the_vehicles_page_shows_the_watch_line_after_presence():
    m = master()
    assert "<th>Watch</th>" in render(m)
    assert watchCell(render(m)) == "Not watched: off"
    status = dict(watched=False, why="named by TWC 8244")
    m.watch = Mock(status=lambda v: status)
    assert watchCell(render(m)) == "Not watched: named by TWC 8244"
    status["why"] = "covered by TeslaMateVehicle"
    assert watchCell(render(m)) == "Not watched: covered by TeslaMate"
    status = dict(watched=True, why="", via="ble", last="asleep", nextAt=None)
    status["lastAt"] = time.time() - 120
    assert watchCell(render(m)) == "Watched: asleep 2 min ago via ble; budget $2.50"
    status.update(via=None, why="no status source")
    assert watchCell(render(m)) == "Not watched: no status source"
    status.update(via="ble")
    status["nextAt"] = time.time() + 150
    assert ", next in 3 min;" in watchCell(render(m))


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
    assert "Fleet API this month" not in html and "Fleet this month" not in html


def test_fleet_spend_line_and_column():
    m = master()
    s = spending(m)
    s.request(VIN, "vehicle_data", 0.30)
    s.request(VIN, "command:set_charge_limit", 0.01)
    html = render(m)
    assert "Fleet API this month: $0.31 · on pace for $0.62 by Dec 1" in html
    assert "<th>Fleet this month</th>" in html
    row = html[html.index("/vehicleDetail/" + VIN) :].split("</tr>")[0]
    assert "$0.31" in row


def test_zero_spend_still_shows_with_a_fleet_token():
    m = master()
    spending(m)
    assert "Fleet API this month: $0.00" in render(m)


def test_telemetry_line_is_approximate():
    m = master()
    s = spending(m, telemetry=True)
    for _ in range(41800):
        s.signal(VIN, "Location")
    html = render(m)
    assert "Fleet Telemetry this month: approx. 41,800 signals, $0.28" in html
    row = html[html.index("/vehicleDetail/" + VIN) :].split("</tr>")[0]
    assert "approx. $0.28 telemetry" in row


def test_last_month_shows_only_when_nonzero():
    m = master()
    clock = SimpleNamespace(t=time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1)))
    s = spending(m, clock=lambda: clock.t)
    assert "Last month" not in render(m)
    s.request(VIN, "wake_up", 0.02)
    clock.t = time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))
    assert "Last month: $0.02" in render(m)


def test_small_telemetry_costs_and_single_requests_read_right():
    from tests.unit.test_vehicle_detail_page import render as detail, status

    m = master()
    s = spending(m, telemetry=True)
    s.request(VIN, "wake_up", 0.02)
    for _ in range(300):
        s.signal(VIN, "Location")
    html = render(m)
    assert "approx. $0.002 telemetry" in html and "approx. 300 signals, $0.002" in html
    page = detail(m, status())
    assert "Fleet API this month: $0.02 in 1 request" in page
    assert "1 requests" not in page
