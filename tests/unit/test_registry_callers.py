import json
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler
from unittest.mock import Mock, patch

from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.TWCSlave import TWCSlave

VIN = "5YJ3E1EA1KF000001"


def test_no_caller_outside_teslaapi_reaches_for_its_list():  # Review Focus 2
    out = subprocess.run(
        ["git", "grep", "-l", "getCarApiVehicles", "--", "lib/TWCManager"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(out) == [
        "lib/TWCManager/EVSEController/TeslaAPIController.py",
        "lib/TWCManager/Vehicle/HomeAssistant.py",
        "lib/TWCManager/Vehicle/TeslaAPI.py",
    ]


def test_get_last_vehicle_uses_registry_attachment():
    reg = VehicleRegistry({})
    car = object()
    reg.getOrCreate(VIN, "TeslaAPI").attach("TeslaAPI", car)
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
    assert s.getLastVehicle() is car
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
    return master


def _handler(master):
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

    cls = CreateHTTPHandlerClass(master)
    with patch.object(BaseHTTPRequestHandler, "__init__", lambda *a, **k: None):
        return cls()


def _registry():
    reg = VehicleRegistry({})
    v = reg.getOrCreate(VIN, "TeslaAPI")
    v.observe("name", "Callandor", "TeslaAPI")
    v.observe("lat", 47.5, "TeslaAPI")
    v.observe("lon", -122.25, "TeslaAPI")
    reg.getOrCreate("5YJ3E1EA1KF000002", "TeslaAPI")  # no name, no location
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
    assert '<option value="%s">Callandor</option>' % VIN in out
    assert '<option value="5YJ3E1EA1KF000002">5YJ3E1EA1KF000002</option>' in out
    assert "anon:" not in out


def test_settings_page_lists_only_located_vehicles():
    h = _handler(_master(_registry()))
    out = _render_loop(h, "settings.html.j2", "L.marker({lon: {{ master", "</form>")
    assert "{lon: -122.25, lat: 47.5}" in out
    assert "Callandor Current Location" in out
    assert '<option value="-122.25,47.5">Callandor</option>' in out
    assert "000002" not in out and "anon:" not in out


def _post(h, body):
    request = Mock()
    request.url = urllib.parse.urlparse("/api/sendTeslaAPICommand")
    request.post_data = json.dumps(body).encode()
    h.do_API_POST.__func__(request)
    return request


def test_send_teslaapi_command_takes_a_vin():
    api = Mock()
    api.apiDebugInterface.return_value = True
    h = _handler(_master(VehicleRegistry({}), teslaapi=api))
    request = _post(
        h,
        {"commandName": "wakeVehicle", "parameters": "{}", "vehicleVIN": VIN},
    )
    api.apiDebugInterface.assert_called_once_with("wakeVehicle", VIN, "{}")
    request.send_response.assert_called_with(200)


def test_send_teslaapi_command_without_teslaapi_is_400():
    h = _handler(_master(VehicleRegistry({}), teslaapi=None))
    request = _post(
        h,
        {"commandName": "wakeVehicle", "parameters": "{}", "vehicleVIN": VIN},
    )
    request.send_response.assert_called_with(400)
