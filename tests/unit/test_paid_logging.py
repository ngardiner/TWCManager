"""The evidence the logs carry: reconciler outcomes, paid Tesla requests and
telemetry plug, state and geofence changes."""

import logging
import re
import threading
from types import SimpleNamespace
from unittest.mock import ANY

import pytest

from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaAPI import _PAID_PATH
from tests.unit.teslatokens import fleet_token, new_api, owner_token
from tests.unit.test_limit_reconciler import ble, setup
from tests.unit.test_gen2twc_provider import VIN

FLEET = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/"


def infoLines(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


def test_every_paid_request_logs_one_info_line_without_url_or_token(
    requests_mock, caplog
):
    caplog.set_level(logging.INFO)
    api = new_api()
    token = fleet_token()
    api.storeToken(token)
    requests_mock.get(re.compile(".*"), json={"response": {}})
    requests_mock.post(re.compile(".*"), json={"response": {}})
    api._request(
        api.fleetSlot,
        "get",
        f"vehicles/{VIN}/vehicle_data?endpoints=charge_state",
        "read",
    )
    api._request(api.fleetSlot, "post", f"vehicles/{VIN}/wake_up", "wake")
    api._request(
        api.fleetSlot, "post", f"vehicles/{VIN}/command/set_charge_limit", "command"
    )
    paid = [m for m in infoLines(caplog) if "paid request" in m]
    assert paid == [
        "Tesla fleet paid request: vehicle_data ($0.002)",
        "Tesla fleet paid request: wake_up ($0.020)",
        "Tesla fleet paid request: command:set_charge_limit ($0.001)",
    ]
    for line in infoLines(caplog):
        assert VIN not in line and "https://" not in line and token not in line


def test_an_answered_408_is_still_a_paid_request(requests_mock, caplog):
    caplog.set_level(logging.INFO)
    api = new_api()
    api.storeToken(fleet_token())
    requests_mock.post(FLEET + f"vehicles/{VIN}/wake_up", status_code=408)
    api._request(api.fleetSlot, "post", f"vehicles/{VIN}/wake_up", "wake")
    assert "Tesla fleet paid request: wake_up ($0.020)" in infoLines(caplog)


def test_free_requests_log_nothing(requests_mock, caplog):
    caplog.set_level(logging.INFO)
    api = new_api()
    api.storeToken(fleet_token())
    api.storeToken(owner_token())
    requests_mock.get(re.compile(".*"), json={"response": {}})
    requests_mock.post(re.compile(".*"), json={"response": {}})
    fleet, owner = api.fleetSlot, api.ownerSlot
    assert fleet.tokenType == "fleet" and owner.tokenType == "owner"
    for path, slot, method in (
        ("vehicles", fleet, "get"),
        (f"vehicles/{VIN}", fleet, "get"),
        (f"vehicles/{VIN}/vehicle_data", owner, "get"),
        (f"vehicles/{VIN}/wake_up", owner, "post"),
        (f"vehicles/{VIN}/command/auto_conditioning_start", owner, "post"),
    ):
        api._request(slot, method, path, "read")
    assert requests_mock.call_count == 5
    assert [m for m in infoLines(caplog) if "paid request" in m] == []


def test_every_attempt_outcome_is_logged_at_info(caplog):
    caplog.set_level(logging.INFO)
    m, r, v, c = setup(ble(O.Outcome(O.DONE)))
    v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    assert r.pass_() == 1
    assert [x for x in infoLines(caplog) if x.startswith("chargeLimit ")] == [
        f"chargeLimit setChargeLimit for {VIN} via TeslaBLE: done "
    ]


def test_telemetry_plug_state_and_geofence_changes_log_at_info(caplog):
    from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle

    caplog.set_level(logging.INFO)
    registry = VehicleRegistry({})
    car = registry.getOrCreate(VIN, "t")
    car.observe("name", "TestCar", "t")
    t = TeslaMateVehicle.__new__(TeslaMateVehicle)
    t.loggedValues = {}
    t.vehicleNames = {"1": "TestCar"}
    for plugged in (True, True, False):
        t._observe(car, "pluggedIn", plugged)
    for state in ("online", "online"):
        t._observe(car, "state", state)
    t._observe(car, "batteryLevel", 50)
    t._observe(car, "pluggedIn", True, retained=True)
    for zone in ("Home", "Home", "Work"):
        t.mqttMessage(
            None,
            None,
            SimpleNamespace(topic="teslamate/cars/1/geofence", payload=zone.encode()),
        )
    assert [m for m in infoLines(caplog) if "->" in m] == [
        "TestCar: pluggedIn None -> True",
        "TestCar: pluggedIn True -> False",
        "TestCar: state None -> online",
        "TestCar: geofence None -> Home",
        "TestCar: geofence Home -> Work",
    ]


def test_a_paid_request_is_counted_under_the_vehicle_lock(requests_mock):
    # The cloud worker and the background thread both count
    from TWCManager.Vehicle.Registry.Vehicle import Vehicle

    class Spy:
        held = False

        def __enter__(self):
            Spy.held = True

        def __exit__(self, *exc):
            Spy.held = False

    class Counted(Vehicle):
        counts = []
        paidRequests = property(
            lambda self: len(self.counts),
            lambda self, n: self.counts.append(Spy.held),
        )

    api = new_api()
    api.storeToken(fleet_token())
    v = VehicleRegistry({}).getOrCreate(VIN, "t")
    v.__class__, v._lock = Counted, Spy()
    requests_mock.post(FLEET + f"vehicles/{VIN}/wake_up", json={"response": {}})
    api._request(api.fleetSlot, "post", f"vehicles/{VIN}/wake_up", "wake", vehicle=v)
    assert Counted.counts == [True]


def test_a_paid_request_is_counted_for_its_car(requests_mock):
    api = new_api()
    api.storeToken(fleet_token())
    requests_mock.post(re.compile(".*"), json={"response": {}})
    car = SimpleNamespace(vin=VIN, _lock=threading.Lock(), paidRequests=0)
    api._request(
        api.fleetSlot,
        "post",
        f"vehicles/{VIN}/command/set_charge_limit",
        "command",
        vehicle=car,
    )
    assert api.master.fleetSpend.car(VIN)["types"] == [
        {
            "kind": "command:set_charge_limit",
            "count": 1,
            "cost": pytest.approx(0.001),
            "last": ANY,
        }
    ]


def test_an_owner_api_request_is_not_counted(requests_mock):
    api = new_api()
    api.storeToken(owner_token())
    requests_mock.post(re.compile(".*"), json={"response": {}})
    api._request(
        api.ownerSlot, "post", f"vehicles/{VIN}/command/set_charge_limit", "command"
    )
    assert api.master.fleetSpend.month()["api"] == 0


def test_the_cached_read_stays_free():
    assert _PAID_PATH.search(f"/api/1/vehicles/{VIN}/vehicle_data_cached") is None
