import json
import pathlib
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler
from unittest.mock import Mock, patch

from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.TWCSlave import TWCSlave

VIN = "5YJ3E1EA1KF000001"
REPO = pathlib.Path(__file__).resolve().parents[2]


def test_no_caller_outside_teslaapi_reaches_for_its_list():  # Review Focus 2
    out = subprocess.run(
        # The dissolved CarApiVehicle's names stay gone: no module keeps a car
        # list (registry.attached() is the list), and "id:" keys are not written
        ["git", "grep", "-lE",
         r"carApiVehicles|getCarApiVehicles|CarApiVehicle|RegistryField|getVehicleByVIN"
         r"|lastVehicleAwake|_liveVehicle|startswith\(\"id:\"\)"
         r"|\.VIN\b([^ ]|$)",  # a car object's .VIN (not "from .VIN import")
         "--", "lib/TWCManager", "html", "tests/integration", "tests/API"],
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout.split()
    assert out == []
    # Forget is atomic: the one caller re-checks "unattached" under the lock
    # (DesiredStore.forget is the registry's removal callback, not a caller)
    forgets = subprocess.run(
        ["git", "grep", "-n", r"[rR]egistry\.forget(", "--", "lib/TWCManager"],
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout.splitlines()
    assert len(forgets) == 1 and "ifUnattached=True" in forgets[0]


def test_get_last_vehicle_uses_registry_attachment():
    reg = VehicleRegistry({})
    reg.getOrCreate(VIN, "OwnerAPI").attach("TeslaAPI", object())
    m = Mock()
    m.vehicleRegistry = reg
    m.getModuleByName = Mock(return_value=None)
    s = TWCSlave(
        bytearray(b"AB"),
        80,
        {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        },
        m,
    )
    s.currentVIN = ""
    s.lastVIN = VIN
    assert s.getLastVehicle() is reg.get(VIN)
    s.lastVIN = "5YJ3E1EA1KF000009"
    assert s.getLastVehicle() is None
    # A lookup never creates a vehicle
    assert [v.key for v in reg.vehicles()] == [VIN]


def _master(reg, teslaapi=None):
    master = Mock()
    master.vehicleRegistry = reg
    master.config = {"config": {"minAmpsPerTWC": 6, "wiringMaxAmpsPerTWC": 32}}
    master.settings = {"homeLat": 1.0, "homeLon": 2.0}
    master.getModuleByName = Mock(return_value=teslaapi)
    master.chargingPoints = Mock(return_value=[])
    return master


def _handler(master):
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

    cls = CreateHTTPHandlerClass(master)
    with patch.object(BaseHTTPRequestHandler, "__init__", lambda *a, **k: None):
        return cls()


def _registry():
    reg = VehicleRegistry({})
    v = reg.getOrCreate(VIN, "OwnerAPI")
    v.observe("name", "TestCar", "OwnerAPI")
    v.observe("lat", 47.5, "OwnerAPI")
    v.observe("lon", -122.25, "OwnerAPI")
    v.attach("TeslaAPI", object())
    # No name, no location, but cloud-listed
    reg.getOrCreate("5YJ3E1EA1KF000002", "OwnerAPI").attach("TeslaAPI", object())
    # Known only by VIN (e.g. a BLE claim): the debug page lists cloud cars only
    reg.getOrCreate("5YJ3E1EA1KF000003", "TeslaBLE")
    reg.beginPlugSession("AB")  # anonymous anon:AB
    return reg


def test_template_vehicles_come_from_registry_without_teslaapi():
    reg = _registry()
    h = _handler(_master(reg, teslaapi=None))
    assert h.templateEnv.globals["vehicles"]() == reg.vehicles()


def _render_loop(h, template, start, end):
    # Render the vehicle loop of a real theme template, cut out of its page
    # so the navbar and other includes do not need their globals.
    src = h.templateEnv.loader.get_source(h.templateEnv, template)[0]
    i = src.index(start)
    j = src.index(end, i)
    return h.templateEnv.from_string(src[i:j]).render(master=h.master)


def test_debug_page_lists_registry_vehicles_by_vin():
    h = _handler(_master(_registry()))
    out = _render_loop(h, "debug.html.j2", '<select id="TeslaAPIVehicle">', "</select>")
    assert '<option value="%s">TestCar</option>' % VIN in out
    assert '<option value="5YJ3E1EA1KF000002">5YJ3E1EA1KF000002</option>' in out
    assert "anon:" not in out
    assert "000003" not in out


def test_settings_page_lists_only_located_vehicles():
    h = _handler(_master(_registry()))
    out = _render_loop(h, "settings.html.j2", "L.marker({lon: {{ master", "</form>")
    assert "{lon: -122.25, lat: 47.5}" in out
    assert "TestCar Current Location" in out
    assert '<option value="-122.25,47.5">TestCar</option>' in out
    assert "000002" not in out and "anon:" not in out
    assert "name=learn" in out


def test_settings_page_hides_learn_without_a_located_vehicle():
    reg = VehicleRegistry({})
    reg.getOrCreate(VIN, "OwnerAPI").attach("TeslaAPI", object())
    h = _handler(_master(reg))
    out = _render_loop(h, "settings.html.j2", "L.marker({lon: {{ master", "</form>")
    assert "name=unset" in out
    assert "name=learn" not in out and "<select name=vehicle>" not in out


def _home(h, fields, value):
    request = Mock()
    request.fields = fields
    request.getFieldValue = lambda key: value
    h.process_home_location.__func__(request)
    return request


def test_learn_home_location_without_a_selection_is_a_noop():
    master = _master(VehicleRegistry({}))
    h = _handler(master)
    request = _home(h, {"learn": ["x"]}, None)
    master.setHomeLon.assert_not_called()
    master.setHomeLat.assert_not_called()
    request.send_response.assert_called_with(302)


def test_learn_home_location_from_selection():
    master = _master(VehicleRegistry({}))
    h = _handler(master)
    _home(h, {"learn": ["x"]}, "-122.25,47.5")
    master.setHomeLon.assert_called_once_with("-122.25")
    master.setHomeLat.assert_called_once_with("47.5")


def _post(h, body):
    request = Mock()
    request.url = urllib.parse.urlparse("/api/sendTeslaAPICommand")
    request.post_data = json.dumps(body).encode()
    h.do_API_POST.__func__(request)
    return request


def test_send_teslaapi_command_takes_a_vin():
    master = _master(VehicleRegistry({}))
    # True: a one-shot attempt was submitted to a lane
    master.providerOrder.debugCommand.return_value = True
    h = _handler(master)
    request = _post(
        h,
        {"commandName": "wakeVehicle", "parameters": "{}", "vehicleVIN": VIN},
    )
    master.providerOrder.debugCommand.assert_called_once_with("wakeVehicle", VIN, "{}")
    request.send_response.assert_called_with(200)


def test_send_teslaapi_command_without_teslaapi_is_400():
    # No TeslaAPI is no longer a 400 by itself (BLE can wake); a command
    # the order cannot carry (no attempt submitted) is
    master = _master(VehicleRegistry({}), teslaapi=None)
    master.providerOrder.debugCommand.return_value = False
    h = _handler(master)
    request = _post(
        h,
        {"commandName": "wakeVehicle", "parameters": "{}", "vehicleVIN": VIN},
    )
    request.send_response.assert_called_with(400)


def test_unset_home_location_twice_is_not_an_error():  # M5
    import threading

    master = _master(VehicleRegistry({}))
    master.settingsLock = threading.RLock()
    h = _handler(master)
    _home(h, {"unset": ["x"]}, None)
    assert "homeLat" not in master.settings and "homeLon" not in master.settings
    request = _home(h, {"unset": ["x"]}, None)  # a second click
    request.send_response.assert_called_with(302)


def _listed_car_slave():
    # A car listed by a real TeslaAPI, on one TWC's lastVIN
    from tests.unit.test_teslaapi_registry import api_with_registry

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    m = Mock()
    m.vehicleRegistry = reg
    m.getModuleByName = Mock(return_value=None)
    s = TWCSlave(
        bytearray(b"AB"),
        80,
        {
            "config": {
                "wiringMaxAmpsPerTWC": 6,
                "useFlexAmpsToStartCharge": False,
                "startStopDelay": 60,
                "fakeMaster": False,
            }
        },
        m,
    )
    s.currentVIN = ""
    s.lastVIN = VIN
    return api, reg, s


def test_get_slave_twcs_reports_the_listed_cars_fields_and_legacy_defaults():
    api, reg, s = _listed_car_slave()
    s.getCurrentChargerLoad = lambda: 0
    master = _master(reg)
    master.getSlaveTWCs = Mock(return_value=[s])
    master.getHomeLatLon = lambda: (1.0, 2.0)
    handler = _handler(master)

    def get():
        request = Mock()
        request.url = urllib.parse.urlparse("/api/getSlaveTWCs")
        handler.do_API_GET.__func__(request)
        body = request.wfile.write.call_args[0][0]
        return json.loads(body.decode("utf-8"))["4142"]

    row = get()
    assert (
        row["lastAtHome"],
        row["lastBatterySOC"],
        row["lastChargeLimit"],
        row["lastTimeToFullCharge"],
    ) == (False, 10000, -1, 0.0)
    v = reg.get(VIN)
    v.observe("batteryLevel", 61, "OwnerAPI")
    v.observe("chargeLimit", 80, "OwnerAPI")
    v.observe("timeToFullCharge", 1.5, "OwnerAPI")
    assert get()["lastAtHome"] is False  # no position: presumed, not home
    v.observe("lat", 1.0, "OwnerAPI")
    v.observe("lon", 2.0, "OwnerAPI")  # parked at home
    row = get()
    assert (
        row["lastAtHome"],
        row["lastBatterySOC"],
        row["lastChargeLimit"],
        row["lastTimeToFullCharge"],
    ) == (True, 61, 80, 1.5)


def _flex_master(slave):
    from TWCManager.TWCMaster import TWCMaster

    m = TWCMaster(
        "AB",
        {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 32,
                "minAmpsPerTWC": 6,
                "maxAmpsAllowedFromGrid": None,
            }
        },
    )
    m.getModuleByName = Mock(return_value=None)
    m.slaveTWCs = {"AB": slave}
    m.slaveTWCRoundRobin = [slave]
    slave.voltsPhaseA = slave.voltsPhaseB = slave.voltsPhaseC = 0
    m.settings.update(
        {
            "scheduledAmpsStartHour": 20,
            "scheduledAmpsEndHour": 7,
            "scheduledAmpsFlexStart": True,
            # > 0, else hoursForFullCharge divides by zero
            "scheduledAmpsMax": 32,
            "scheduledAmpsBatterySize": 100,
        }
    )
    return m


def test_flex_start_reads_the_listed_cars_limit_and_battery():
    api, reg, s = _listed_car_slave()
    m = _flex_master(s)
    # No observations: the legacy -1 / 10000 defaults
    assert m.getScheduledAmpsTimeFlex() == (1308.96, 7, 0x7F)
    v = reg.get(VIN)
    v.observe("chargeLimit", 80, "OwnerAPI")
    v.observe("batteryLevel", 40, "OwnerAPI")
    assert m.getScheduledAmpsTimeFlex() == (1.54, 7, 0x7F)


def test_the_offered_total_counts_cars_on_chargers_no_twc_reports():
    # A TWC-less install offers each car its share as the car's own rate
    from types import SimpleNamespace

    from TWCManager.Vehicle.Reconcile.Desired import AMPS

    reg = _registry()
    master = _master(reg)
    master.getSlaveTWCs = Mock(return_value=[])
    car = SimpleNamespace(vehicle=SimpleNamespace(key="car"))
    onTWC = SimpleNamespace(vehicle=SimpleNamespace(key="named"))
    master.chargingPoints = Mock(
        return_value=[
            SimpleNamespace(localEndpoints=[], vehicleEndpoints=[car]),
            SimpleNamespace(localEndpoints=[object()], vehicleEndpoints=[onTWC]),
        ]
    )
    offers = {("car", AMPS): 32, ("named", AMPS): 16}
    master.desired.get = lambda key, aspect: (
        SimpleNamespace(value=offers[(key, aspect)])
        if (key, aspect) in offers
        else None
    )
    request = Mock()
    request.url = urllib.parse.urlparse("/api/getSlaveTWCs")
    _handler(master).do_API_GET.__func__(request)
    body = request.wfile.write.call_args[0][0]
    assert json.loads(body.decode("utf-8"))["total"]["lastAmpsOffered"] == 32


def test_the_history_api_shows_the_current_bucket_with_no_twc():
    # The live bucket is the charger load's mean, Gen2 or not
    master = _master(_registry())
    master.getSlaveTWCs = Mock(return_value=[])
    master.settings = {}
    master.historyWatts = Mock(return_value=3840.0)
    request = Mock()
    request.url = urllib.parse.urlparse("/api/getHistory")
    _handler(master).do_API_GET.__func__(request)
    rows = json.loads(request.wfile.write.call_args[0][0].decode("utf-8"))
    assert len(rows) == 48 * 12
    assert rows[-1]["charger_power"] == 3840.0
    assert {r["charger_power"] for r in rows[:-1]} == {0}
