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


# /api/getVehicles and /api/getVehicle/<VIN>

import time
from TWCManager.Control.Views import (
    pageFingerprint,
    pendingCommandData,
    providerClaims,
    spendShown,
    vehiclesView,
    vehicleView,
)
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT, CHARGING, RELEASE, Attempt
from TWCManager.Vehicle.Reconcile.Outcome import HELD, UNREACHABLE, Outcome
from tests.unit.test_vehicles_page import VIN as CAR, master, spending


def entry(view, key=CAR):
    return next(e for e in view["vehicles"] if e["key"] == key)


def test_vehicle_entry_raw_fields():
    m = master()
    e = entry(vehiclesView(m))
    assert e["vin"] == CAR and e["presence"] == "presumed" and e["name"] == "TestCar"
    assert e["anonymous"] is False
    assert e["heldLimit"] == {"held": 80, "outside": 90}
    assert e["providers"] == [
        {"provider": "TeslaBLE", "status": "paired", "commands": True, "readOnlyReason": None}
    ]
    assert e["watch"] == {"watched": False, "why": "off"} and e["spend"] is None
    assert abs(e["lastSeenAt"] - time.time()) < 5
    anon = entry(vehiclesView(m), "anon:8244")
    assert anon["anonymous"] is True and anon["point"] == "8244"
    assert anon["presence"] == "home" and anon["heldLimit"] is None


def test_vehicles_view_top_level():
    m = master()
    v = vehiclesView(m)
    assert v["budget"] == 2.5 and abs(v["now"] - time.time()) < 5
    assert v["history"] == [{"key": CAR, "chargeSessions": 3, "totalkWh": 12}]
    assert [e["key"] for e in v["vehicles"]] == [CAR, "anon:8244"]


def test_watch_status_passes_through():
    m = master()
    status = dict(watched=True, why="", via="ble", last="asleep", nextAt=None, lastAt=1.0)
    m.watch = Mock(status=lambda v: status)
    assert entry(vehiclesView(m))["watch"] == status


def test_presence_entry_follows_marks():
    m = master()
    v = m.vehicleRegistry.get(CAR)
    v.mark("plugIn", 100.0)
    assert entry(vehiclesView(m))["presence"] == "home"
    v.mark("unplug", 200.0)
    assert entry(vehiclesView(m))["presence"] == "notHome"


def test_providers_in_claim_order_with_raw_names():
    m = master()
    v = m.vehicleRegistry.get(CAR)
    v.claim("TeslaMateVehicle", False, "reports", keepsAlive=False)
    v.claim("Gen3TWC", True, "")
    assert providerClaims(m, v) == [
        {"provider": "TeslaBLE", "status": "paired", "commands": True, "readOnlyReason": None},
        {"provider": "TeslaMateVehicle", "status": "reports", "commands": False, "readOnlyReason": None},
        {"provider": "Gen3TWC", "status": "", "commands": True, "readOnlyReason": None},
    ]


@pytest.mark.parametrize("held", ["Owner", "Fleet"])
def test_a_cloud_api_without_its_token_is_not_listed(held):
    m = master()
    v = m.vehicleRegistry.get(CAR)
    api = Mock()
    api.ownerTokenHeld.return_value = held == "Owner"
    api.fleetTokenHeld.return_value = held == "Fleet"
    api.ownerReadOnlyReason.return_value = None
    m.getModuleByName = lambda n: api if n == "TeslaAPI" else None
    v.claim("OwnerAPI", held == "Owner", "listed")
    v.claim("FleetAPI", held == "Fleet", "listed")
    other = "Fleet" if held == "Owner" else "Owner"
    claims = {c["provider"]: c for c in providerClaims(m, v)}
    assert claims[held + "API"]["commands"] is True
    assert other + "API" not in claims


def test_read_only_reason_only_when_it_cannot_command():
    m = master()
    v = m.vehicleRegistry.get(CAR)
    api = Mock()
    api.ownerReadOnlyReason.return_value = "car requires signed commands"
    api.fleetReadOnlyReason.return_value = "token lacks command scope"
    m.getModuleByName = lambda n: api if n == "TeslaAPI" else None
    v.claim("OwnerAPI", False, "listed")
    v.claim("FleetAPI", False, "listed")
    v.claim("TeslaBLE", False, "unproven", keepsAlive=False)
    v.claim("HomeAssistant", True, "entities")
    claims = {c["provider"]: c for c in providerClaims(m, v)}
    assert claims["OwnerAPI"] == {
        "provider": "OwnerAPI", "status": "listed", "commands": False,
        "readOnlyReason": "car requires signed commands",
    }
    assert claims["FleetAPI"]["readOnlyReason"] == "token lacks command scope"
    assert claims["TeslaBLE"] == {
        "provider": "TeslaBLE", "status": "unproven", "commands": False, "readOnlyReason": None,
    }
    assert claims["HomeAssistant"]["commands"] is True
    assert claims["HomeAssistant"]["readOnlyReason"] is None
    api.ownerReadOnlyReason.return_value = None
    v.claim("OwnerAPI", True, "listed")
    v.claim("FleetAPI", True, "listed")
    claims = {c["provider"]: c for c in providerClaims(m, v)}
    assert claims["OwnerAPI"]["readOnlyReason"] is None
    assert claims["FleetAPI"] == {
        "provider": "FleetAPI", "status": "listed", "commands": True, "readOnlyReason": None,
    }


def attempt(aspect, value, provider="TeslaBLE"):
    return Attempt(CAR, aspect, "x", value, provider, False, None, False, time.time() - 12)


def pendingRows(m, v=None):
    return {r["aspect"]: r for r in pendingCommandData(m, v or m.vehicleRegistry.get(CAR))}


def test_pending_data_rows():
    m = master()
    assert pendingRows(m) == {}

    # in flight
    m.desired.set(CAR, CHARGE_LIMIT, 50, "policy")
    a = attempt(CHARGE_LIMIT, 50)
    m.desired.record(CAR, CHARGE_LIMIT).inFlight = a
    row = pendingRows(m)[CHARGE_LIMIT]
    assert row == {
        "aspect": CHARGE_LIMIT, "value": 50,
        "inFlight": {"provider": "TeslaBLE", "since": a.since},
        "said": None, "lastOutcome": None,
    }

    # every aspect, not tried yet
    m = master()
    m.desired.set(CAR, CHARGE_LIMIT, RELEASE, "policy")
    m.desired.set(CAR, CHARGING, True, "distributor")
    m.desired.set(CAR, AMPS, 16, "distributor")
    rows = pendingRows(m)
    assert {a: r["value"] for a, r in rows.items()} == {CHARGE_LIMIT: RELEASE, CHARGING: True, AMPS: 16}
    for r in rows.values():
        assert r["inFlight"] is None and r["said"] is None and r["lastOutcome"] is None

    # held by a real hold outcome
    m = master()
    m.desired.set(CAR, CHARGING, True, "distributor")
    a = attempt(CHARGING, True, "Gen2TWC")._replace(since=time.time())
    m.desired.record(CAR, CHARGING).inFlight = a
    m.desired.noteOutcome(CAR, CHARGING, a, Outcome("satisfied", HELD + "driving"), time.time(), paid=False)
    rec = m.desired.record(CAR, CHARGING)
    last = pendingRows(m)[CHARGING]["lastOutcome"]
    assert last["detail"] == "held: driving" and last["provider"] == "Gen2TWC"
    assert last["kind"] == "satisfied" and last["at"] == rec.lastOutcomeAt

    # held with no outcome yet
    m = master()
    m.desired.set(CAR, CHARGING, True, "distributor")
    m.desired.record(CAR, CHARGING).heldAt = time.time()
    assert pendingRows(m)[CHARGING]["lastOutcome"] is None

    # what the reconciler said, with the last outcome alongside
    m = master()
    m.desired.set(CAR, CHARGING, False, "distributor")
    rec = m.desired.record(CAR, CHARGING)
    rec.lastOutcome, rec.lastOutcomeAt = Outcome(UNREACHABLE), time.time() - 5
    m.reconciler._blockedReasons[(CAR, CHARGING)] = "hands off: battery below minChargeLevel"
    row = pendingRows(m)[CHARGING]
    assert row["said"] == "hands off: battery below minChargeLevel"
    assert row["lastOutcome"]["kind"] == UNREACHABLE and row["lastOutcome"]["detail"] == ""

    # last outcome with its source and time
    m = master()
    m.desired.set(CAR, CHARGE_LIMIT, 70, "policy")
    rec = m.desired.record(CAR, CHARGE_LIMIT)
    rec.lastOutcome, rec.lastOutcomeAt = Outcome("unreachable", "backoff"), time.time() - 120
    rec.lastProvider = "TeslaBLE"
    assert pendingRows(m)[CHARGE_LIMIT]["lastOutcome"] == {
        "kind": "unreachable", "detail": "backoff", "provider": "TeslaBLE", "at": rec.lastOutcomeAt,
    }


def test_pending_data_hides_satisfied_and_settled():
    m = master()
    m.vehicleRegistry.get(CAR).observe("chargeLimit", 70, "TeslaBLE")
    m.desired.set(CAR, CHARGE_LIMIT, 70, "policy")
    m.desired.set(CAR, AMPS, 16, "distributor")
    m.desired.record(CAR, AMPS).settledAt = time.time()
    assert pendingRows(m) == {}


def test_pending_data_shows_a_settled_desire_the_car_drifted_from():
    from tests.unit.test_limit_reconciler import ble, setup
    from TWCManager.Vehicle.Reconcile import Outcome as O
    from TWCManager.Vehicle.Reconcile.Reconciler import RETRY

    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE)))
    v.observe("chargeCurrentRequest", 32, "TeslaBLE")
    m.desired.set(CAR, AMPS, 16, "distributor")
    assert r.pass_() == 1 and m.desired.record(CAR, AMPS).settledAt is not None
    assert pendingCommandData(m, v) == []
    c.t += 10
    v.observe("chargeCurrentRequest", 32, "TeslaMateVehicle")
    assert [row["aspect"] for row in pendingCommandData(m, v)] == [AMPS]
    c.t += RETRY[AMPS]
    assert r.pass_() == 1
    c.t += 10
    v.observe("chargeCurrentRequest", 16, "TeslaMateVehicle")
    assert pendingCommandData(m, v) == [] and r.pass_() == 0


def test_pending_data_shows_a_limit_held_asleep_until_reported():
    from tests.unit.test_limit_reconciler import ble, setup

    m, r, v, c = setup(ble())
    v.observe("state", "asleep", "TeslaBLE")
    m.desired.set(CAR, CHARGE_LIMIT, 50, "policy")
    assert r.pass_() == 1 and m.desired.record(CAR, CHARGE_LIMIT).heldAt is not None
    last = pendingRows(m, v)[CHARGE_LIMIT]["lastOutcome"]
    assert last["detail"].startswith("held: asleep") and last["provider"] == "TeslaBLE"
    c.t += 30
    v.observe("chargeLimit", 50, "TeslaMateVehicle")
    assert pendingRows(m, v) == {} and r.pass_() == 0


def test_vehicle_views_create_no_desired_state():
    m = master()
    vehiclesView(m)
    vehicleView(m, CAR)
    assert m.desired.keys() == []


def test_spend_in_views():
    m = master(); s = spending(m); s.request(CAR, "vehicle_data", 0.30)
    assert spendShown(m) == (True, False)
    v = vehiclesView(m)
    assert v["spend"]["fleet"] and not v["spend"]["telemetry"]
    assert v["spend"]["month"]["api"] == pytest.approx(0.30)
    assert entry(v)["spend"]["requests"] == 1
    assert entry(v, "anon:8244")["spend"] is None
    d = vehicleView(m, CAR)
    assert d["spend"] == {"fleet": True, "telemetry": False}
    assert d["vehicle"]["spend"]["requests"] == 1


def test_vehicles_fingerprint_follows_shape():
    m = master(); before = vehiclesView(m)["fingerprint"]
    assert before == pageFingerprint(m, "vehicles")
    m.settings["VehicleGroups"]["Guests"] = {"Members": []}
    assert vehiclesView(m)["fingerprint"] != before
    m2 = master(); spending(m2)  # a Fleet token now held: the spend column shows
    assert vehiclesView(m2)["fingerprint"] != before


def test_vehicle_fingerprint_matches_page():
    m = master()
    fp = vehicleView(m, CAR.lower())["fingerprint"]
    assert fp == pageFingerprint(m, "vehicle", CAR)
    assert pageFingerprint(m, "home") == fingerprint("home")


def test_vehicles_view_on_fresh_settings():  # Review Focus 5
    m = master(); m.settings = {"VehicleGroups": {}}
    v = vehiclesView(m)
    assert v["history"] == [] and v["spend"] == {"fleet": False, "telemetry": False, "month": None}
    assert vehicleView(m, CAR)["vehicle"]["chargeSessions"] == 0


def test_vehicle_view_detail_fields():
    m = master()
    d = vehicleView(m, CAR)["vehicle"]
    assert d["vin"] == CAR and d["chargeSessions"] == 3
    assert d["ble"] is None and d["pending"] == []
    ble = Mock(bleStatus=lambda vin: {"status": "paired", "vin": vin})
    m.getModuleByName = lambda n: ble if n == "TeslaBLE" else None
    assert vehicleView(m, CAR.lower())["vehicle"]["ble"] == {"status": "paired", "vin": CAR}


def keysIn(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from keysIn(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from keysIn(v)


def test_no_secrets_in_vehicle_apis():
    m = master(); spending(m, telemetry=True)  # fleetSlot.accessToken == "tok"
    m.fleetSpend.request(CAR, "wake_up", 0.02)
    for view in (vehiclesView(m), vehicleView(m, CAR)):
        body = json.dumps(view)
        assert '"tok"' not in body
        for key in keysIn(json.loads(body)):
            low = key.lower()
            assert "token" not in low and "secret" not in low and "webhook" not in low, key


def test_vehicle_views_never_read_providers(monkeypatch):
    monkeypatch.setattr(Vehicle, "read", Mock(side_effect=AssertionError))
    m = master(); spending(m)
    m.vehicleRegistry.get(CAR).claim("OwnerAPI", False, "listed")
    m.vehicleRegistry.get(CAR).claim("FleetAPI", False, "listed")
    api = Mock(spec=["ownerTokenHeld", "fleetTokenHeld", "ownerReadOnlyReason", "fleetReadOnlyReason", "fleetSlot"])
    api.ownerReadOnlyReason.return_value = api.fleetReadOnlyReason.return_value = None
    m.getModuleByName = lambda n: api if n == "TeslaAPI" else None
    vehiclesView(m); vehicleView(m, CAR)


def test_get_vehicles_route():
    m = master()
    status, headers, body = callAPI(m, "/api/getVehicles")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    data = json.loads(body)
    assert data["fingerprint"] == pageFingerprint(m, "vehicles")
    assert entry(data)["vin"] == CAR


def test_get_vehicle_route_variants():  # Review Focus 2
    m = master()
    for path in ("/api/getVehicle/" + CAR.lower(), "/api/getVehicle/" + CAR + "/"):
        status, headers, body = callAPI(m, path)
        assert status == 200 and json.loads(body)["vehicle"]["vin"] == CAR
        assert headers["Cache-Control"] == "no-store"
    for path in ("/api/getVehicle/", "/api/getVehicle/5YJ3E1EA1KF000009", "/api/getVehicle/%3Cx%3E"):
        status, headers, body = callAPI(m, path)
        assert status == 404 and json.loads(body) == {"error": "unknown vehicle"}
        assert headers["Cache-Control"] == "no-store"


def test_fingerprint_template_global(monkeypatch):
    from http.server import BaseHTTPRequestHandler
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

    monkeypatch.setattr(BaseHTTPRequestHandler, "__init__", lambda self, *a, **k: None)
    m = master()
    m.config = {"config": {}}
    m.checkForUpdates = lambda: False
    handler = CreateHTTPHandlerClass(m)()
    fp = handler.templateEnv.globals["fingerprint"]
    assert fp("vehicles") == pageFingerprint(m, "vehicles")
    assert fp("vehicle", CAR) == pageFingerprint(m, "vehicle", CAR)
    assert fp("home") == fingerprint("home")


# Vehicles and Vehicle details pages: every hook resolves against its API

import re

_MISSING = object()
GEN3_ANON = "anon:Gen3TWC-192.168.1.50"  # a Gen3 point's ID holds dots


def _lookup(node, path):
    # live.js's rule: a segment on an array selects the element whose key
    # (else id) equals it, else a numeric index; on an object, its member
    for seg in path.split("."):
        if isinstance(node, list):
            keyed = [
                e for e in node
                if isinstance(e, dict)
                and str(e["key"] if e.get("key") is not None else e.get("id")) == seg
            ]
            if keyed:
                node = keyed[0]
            elif seg.isdigit() and int(seg) < len(node):
                node = node[int(seg)]
            else:
                return _MISSING
        elif isinstance(node, dict) and seg in node:
            node = node[seg]
        else:
            return _MISSING
    return node


def _checkHooks(html, data):
    paths = re.findall(r"data-(?:field|show|list|keyed-list)=['\"]([^'\"]+)['\"]", html)
    assert paths
    for path in paths:
        assert not path.startswith("."), path  # no keyed-list templates here
        assert _lookup(data, path) is not _MISSING, path
    src = open("lib/TWCManager/Control/static/js/format.js").read()
    names = re.findall(r"data-(?:format|show-if|row)=['\"]([^'\"]+)['\"]", html)
    assert names
    for name in names:
        assert "F." + name + " = function" in src, name


def dottedMaster():
    from tests.unit.test_vehicles_page import master as vehiclesMaster

    m = vehiclesMaster()
    m.vehicleRegistry.beginPlugSession("Gen3TWC-192.168.1.50")
    m.settings["Vehicles"]["legacy.record"] = {"chargeSessions": 1, "totalkWh": 2}
    s = spending(m, telemetry=True)
    s.request(CAR, "vehicle_data", 0.30)
    s.signal(CAR, "BatteryLevel")
    return m


def test_vehicles_hooks_match_the_api_with_dotted_keys():
    from tests.unit.test_vehicles_page import render

    m = dottedMaster()
    html = render(m)
    data = vehiclesView(m)
    keys = [e["key"] for e in data["vehicles"]]
    assert GEN3_ANON in keys and "Fleet this month" in html
    _checkHooks(html, data)
    # each row's hooks address that row's own vehicle
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S)
    anonRow = next(r for r in rows if "Unknown car on TWC Gen3TWC-192.168.1.50" in r)
    carRow = next(r for r in rows if f"/vehicleDetail/{CAR}" in r and "data-field" in r)
    for row, key in ((anonRow, GEN3_ANON), (carRow, CAR)):
        idx = set(re.findall(r'data-field="vehicles\.(\d+)', row))
        assert len(idx) == 1 and keys[int(idx.pop())] == key
    histRow = next(r for r in rows if ">legacy.record<" in r)
    entry = _lookup(data, re.search(r'data-field="(history\.\d+)\.', histRow).group(1))
    assert entry["key"] == "legacy.record"


def test_vehicle_detail_hooks_match_the_api():
    from tests.unit.test_vehicle_detail_page import render

    m = dottedMaster()
    ble = Mock()
    ble.bleStatus.return_value = {
        "status": "paired", "since": 1.0, "label": "Paired", "sinceAge": 5.0,
        "inRange": True, "scannedAt": 1.0, "lastScanAge": 5.0,
        "scanPending": False, "canScan": True, "scanBlocked": None,
        "attempt": {"at": 1.0, "age": 5.0},
    }
    mods = {"TeslaAPI": m.getModuleByName("TeslaAPI"), "TeslaBLE": ble}
    m.getModuleByName = mods.get
    m.modules = {"TeslaBLE": ble}
    m.vehicleRegistry.get(CAR).observe("name", "x.y", "OwnerAPI")
    html = render(m)
    assert "BLE Control" in html
    _checkHooks(html, vehicleView(m, CAR))
