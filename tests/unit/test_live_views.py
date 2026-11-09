import json

from TWCManager.Control import Views
from TWCManager.Control.Views import fingerprint, jsonSafe
from tests.unit.live_helpers import callAPI
from unittest.mock import Mock


def test_fingerprint_is_stable_and_shape_sensitive():
    assert fingerprint("a", ["x"]) == fingerprint("a", ["x"])
    assert fingerprint("a", ["x"]) != fingerprint("a", ["y"])
    assert len(fingerprint()) == 12


def test_fingerprint_changes_with_start_time(monkeypatch):
    before = fingerprint("a")
    monkeypatch.setattr(Views, "STARTED", Views.STARTED + 1)
    assert fingerprint("a") != before


def test_json_safe_non_finite_and_objects():
    out = jsonSafe({"a": float("nan"), "b": [float("inf"), 1.5], 3: object, "c": (1,)})
    assert out["a"] is None and out["b"] == [None, 1.5] and out["c"] == [1]
    assert out["3"].startswith("<class")
    json.loads(json.dumps(out, allow_nan=False))


def test_call_api_records_status_headers_body():
    m = Mock()
    m.config = {"config": {}}
    status, headers, body = callAPI(m, "/api/getConfig")
    assert status == 200 and headers["Content-type"] == "application/json"
    assert isinstance(json.loads(body), dict)


def test_send_json_headers_and_safety():
    import io
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass
    cls = CreateHTTPHandlerClass(Mock())
    h = cls.__new__(cls)
    h.url = Mock(path="/x"); h.client_address = ("127.0.0.1", 0); h.wfile = io.BytesIO()
    seen = {}
    h.send_response = lambda c, m=None: seen.__setitem__("code", c)
    h.send_header = lambda k, v: seen.__setitem__(k, v)
    h.end_headers = lambda: None
    h.sendJSON({"a": float("nan")}, 201)
    assert seen["code"] == 201 and seen["Cache-Control"] == "no-store"
    assert json.loads(h.wfile.getvalue()) == {"a": None}


# /api/getChargingPoints

import pytest
from unittest.mock import MagicMock, patch
from TWCManager.Control.Views import _gen3View, _pointView, chargingPointsView, slaveTotals
from TWCManager.EVSEInstance.Gen3TWC import Gen3TWC
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from TWCManager.Vehicle.Registry.Vehicle import Vehicle
from tests.unit.test_charging_points import VIN, VIN2, car, real
from tests.unit.test_gen3twc_provider import vitals


def rawVitals(unit, data):
    # A successful fetch whose vitals are exactly data
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(data).encode()
    with patch("urllib.request.urlopen", return_value=resp):
        assert unit.refreshVitals()


def emptyVitals(unit):
    # A reachable unit whose vitals carry none of the fields read
    rawVitals(unit, {})


def withGen2AndCars(m):
    # A Gen2 TWC with its car, and a car on no TWC rated 16 A
    s = m.newSlave(b"\x82\x44", 80)
    s.currentVIN = VIN; s.reportedAmpsMax = 32; s.reportedAmpsActual = 10.5
    s.lastAmpsOffered = 16.333; s.lifetimekWh = 1234; s.isCharging = 1
    car(m, VIN, "Charging")
    car(m, VIN2, "Charging")
    m.desired.set(VIN2, AMPS, 16, "distributor")
    return m


@pytest.fixture
def gen2_master():
    return withGen2AndCars(real())


@pytest.fixture
def mixed_master():
    # ...and a Gen3 unit with no car
    m = withGen2AndCars(real(gen3=True))
    (u,) = m.getModuleByName("Gen3TWCs").devices()
    vitals(u, False, 0.0, state="not_connected")
    return m


@pytest.fixture
def idle_master():
    m = real(gen3=True)
    m.newSlave(b"\x82\x44", 80)
    (u,) = m.getModuleByName("Gen3TWCs").devices()
    emptyVitals(u)
    return m


def test_slave_totals_match_get_slave_twcs(gen2_master):
    status, _, body = callAPI(gen2_master, "/api/getSlaveTWCs")
    total = json.loads(body)["total"]
    assert total == slaveTotals(gen2_master)
    assert total["lastAmpsOffered"] == 32.33  # the TWC's 16.333 and the car's 16


def test_points_list_every_kind(mixed_master):  # one Gen2 with car, one Gen3, one car-only
    view = chargingPointsView(mixed_master)
    kinds = sorted(tuple(p["controllers"]) for p in view["points"])
    assert ("Gen3TWCs",) in kinds and ("VehicleEndpoints",) in kinds
    gen2 = next(p for p in view["points"] if p["gen2"])
    assert gen2["gen2"]["twcid"] == "8244" and gen2["vin"] == VIN
    assert gen2["hasTWC"] is True and gen2["ampsOffered"] == 16.333
    assert gen2["ampsDrawn"] == 10.5 and gen2["gen2"]["chargingSince"] is None
    car = next(p for p in view["points"] if p["controllers"] == ["VehicleEndpoints"])
    assert car["hasTWC"] is False and car["ampsOffered"] == 16  # its desired AMPS
    assert car["gen2"] is None and car["gen3"] is None and car["vin"] == VIN2
    assert gen2["gen3"] is None
    gen3 = next(p for p in view["points"] if p["controllers"] == ["Gen3TWCs"])
    assert gen3["hasTWC"] is True and gen3["gen2"] is None and gen3["gen3"]


def test_total_covers_every_point(mixed_master):
    (u,) = mixed_master.getModuleByName("Gen3TWCs").devices()
    u.lastTarget = u.convertAmpsToWatts(8, u.currentVoltage)
    vitals(u, True, 7.25)  # the Gen3's car takes 7.25 A of the 8 A offered
    view = chargingPointsView(mixed_master)
    kinds = sorted(p["controllers"][0] for p in view["points"])
    # No car-only point: while the Gen3 has an unnamed car plugged in, that
    # car could be any car at home, so no car is listed on its own
    assert kinds == ["Gen2TWCs", "Gen3TWCs"]
    assert view["total"] == {
        "ampsOffered": 24.33,  # 16.333 + 8
        "ampsDrawn": 17.75,  # 10.5 + 7.25
        "maxAmps": 128.0,  # the Gen2's own 80 A, the Gen3's 48 A fuse
        "lifetimekWh": 1234,  # Gen2 TWCs only
        "carsCharging": 2,  # both points draw current
    }
    # /api/getSlaveTWCs keeps its own total
    total = json.loads(callAPI(mixed_master, "/api/getSlaveTWCs")[2])["total"]
    assert total == slaveTotals(mixed_master) and total["TWCID"] == "total"


def test_total_counts_a_point_known_only_through_the_car(mixed_master):
    view = chargingPointsView(mixed_master)
    car = next(p for p in view["points"] if p["controllers"] == ["VehicleEndpoints"])
    total = view["total"]
    assert total["ampsOffered"] == 32.33  # the Gen2's 16.333, the car's 16
    assert total["maxAmps"] == round(80 + 48 + car["maxAmps"], 2)


def test_total_with_no_points_is_zero_floats():
    total = chargingPointsView(real())["total"]
    assert total == {"ampsOffered": 0.0, "ampsDrawn": 0.0, "maxAmps": 0.0,
                     "lifetimekWh": 0, "carsCharging": 0}
    assert all(isinstance(total[k], float) for k in ("ampsOffered", "ampsDrawn", "maxAmps"))


def test_cars_charging_counts_points_drawing_current(idle_master):
    view = chargingPointsView(idle_master)  # idle Gen2, Gen3 with empty vitals
    assert view["total"]["carsCharging"] == 0


def test_gen3_point_reports_its_cached_vitals(mixed_master):
    (u,) = mixed_master.getModuleByName("Gen3TWCs").devices()
    rawVitals(u, {"vehicle_connected": True, "vehicle_current_a": 16.0,
                  "grid_v": 240.0, "evse_state": 11, "session_energy_wh": 5123.4,
                  "session_s": 1200})
    view = chargingPointsView(mixed_master)
    gen3 = next(p for p in view["points"] if p["controllers"] == ["Gen3TWCs"])
    assert gen3["gen3"] == {
        "id": "Gen3TWC-10.0.0.1",
        "reachable": True,
        "state": 11,  # raw, as the unit reports it
        "vehicleConnected": True,
        "sessionEnergyWh": 5123.4,
    }
    assert gen3["ampsDrawn"] == 16.0


def test_gen3_after_a_failed_fetch_reports_no_vitals(mixed_master):
    (u,) = mixed_master.getModuleByName("Gen3TWCs").devices()
    rawVitals(u, {"vehicle_connected": True, "evse_state": 11,
                  "session_energy_wh": 5123.4})
    # An unreachable unit leaves the point list, so hold its point from before
    point = next(x for x in mixed_master.chargingPoints() if u in x.endpoints)
    with patch("urllib.request.urlopen", side_effect=OSError("timed out")):
        assert u.refreshVitals() is False
    assert u.vitals  # the cache still holds the last successful fetch
    assert _pointView(mixed_master, point)["gen3"] == {"id": "Gen3TWC-10.0.0.1", "reachable": False,
                            "state": None, "vehicleConnected": None,
                            "sessionEnergyWh": None}


def test_gen3_without_vitals_reports_none(idle_master):
    view = chargingPointsView(idle_master)  # its vitals carry no field
    gen3 = next(p for p in view["points"] if p["controllers"] == ["Gen3TWCs"])
    assert gen3["gen3"] == {"id": "Gen3TWC-10.0.0.1", "reachable": True,
                            "state": None, "vehicleConnected": None,
                            "sessionEnergyWh": None}
    # A unit never yet heard from is not a point; its view is all unknown
    unit = real(gen3=True).getModuleByName("Gen3TWCs").devices()[0]
    assert _gen3View(unit) == {"id": "Gen3TWC-10.0.0.1", "reachable": False,
                               "state": None, "vehicleConnected": None,
                               "sessionEnergyWh": None}


def test_points_with_missing_facts(idle_master):  # Review Focus 4
    view = chargingPointsView(idle_master)  # idle Gen2 (no car), Gen3 with empty vitals
    assert len(view["points"]) == 2
    for p in view["points"]:
        assert p["vin"] is None and p["name"] == "" and p["soc"] is None
        assert isinstance(p["ampsDrawn"], float)
    json.dumps(view, allow_nan=False)


def test_points_route_headers(mixed_master):
    status, headers, body = callAPI(mixed_master, "/api/getChargingPoints")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert json.loads(body)["fingerprint"] == fingerprint("home")


def test_points_never_fetch(mixed_master, monkeypatch):
    monkeypatch.setattr(Gen3TWC, "refreshVitals", Mock(side_effect=AssertionError))
    monkeypatch.setattr(Vehicle, "read", Mock(side_effect=AssertionError))
    chargingPointsView(mixed_master)
