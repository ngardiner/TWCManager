# tests/unit/test_limit_wiring.py
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit, HeldLimits
from TWCManager.Vehicle.Reconcile.Targets import applyPolicyLimit, onArrival, onDeparture, presentVehicles, targetsFor
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.reconcile_helpers import inline
from tests.unit.teslatokens import FLEET_NA_URL, OWNER_API_URL, FakeMaster, fleet_token, new_api, owner_token
from tests.unit.test_gen2twc_provider import make
from tests.unit.test_limit_reconciler import VIN, ble, prov, setup
VIN2 = "5YJ3E1EA1KF000002"; VIN3 = "5YJ3E1EA1KF000003"; VIN4 = "5YJ3E1EA1KF000004"
REPO = Path(__file__).resolve().parents[2]

def cars(m):
    reg = m.vehicleRegistry; a = reg.get(VIN); a.observe("twcVin", "8244", "TWC")
    b = reg.getOrCreate(VIN2, "t"); b.attach("TeslaAPI", SimpleNamespace(atHome=False, ID=2, VIN=VIN2, update_location=lambda minInterval=0: True))
    d = reg.getOrCreate(VIN3, "t"); d.claim("TeslaBLE", True, "paired"); return a, b, d

def test_present_rule_until_phase_5():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); a, b, d = cars(m); anon = m.vehicleRegistry.beginPlugSession("8245")
    assert [x.vin for x in presentVehicles(m, C.SET_CHARGE_LIMIT)] == [VIN, VIN3]            # on a TWC; BLE-only with no location evidence; not the away API car, not the anonymous one
    assert [x.key for x in targetsFor(m, None, C.STOP_CHARGING)] == [VIN, VIN3] and targetsFor(m, anon.key) == [anon]   # mode 1: the anonymous car has no stop candidate
    m.settings["chargeStopMode"] = "2"; m.newSlave(b"\x82\x45", 80)
    assert [x.key for x in targetsFor(m, None, C.STOP_CHARGING)] == [VIN, VIN3, anon.key]                            # mode 2: the Gen2 stop can reach it
    m.providerOrder.provider("TeslaBLE").ready = lambda vehicle, cap: False
    assert m.setPolicyLimit(50, transition=True) is None and m.desired.get(VIN, CHARGE_LIMIT) is None   # no candidate: no desired state

def test_policy_limit_sets_desired_for_present_cars_and_none_restores():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); a, b, d = cars(m); m.heldLimits.set(VIN, 90, 50)
    m.setPolicyLimit(50, transition=True); assert m.policyLimit == 50 and m.desired.get(VIN, CHARGE_LIMIT).value == 50 and m.desired.get(VIN2, CHARGE_LIMIT) is None
    m.setPolicyLimit(None, transition=True); assert m.desired.get(VIN, CHARGE_LIMIT).restore and m.desired.get(VIN, CHARGE_LIMIT).value == 90 and m.desired.get(VIN3, CHARGE_LIMIT) is None
    assert applyPolicyLimit(m, None, decision=False) == 2                      # both present cars touched; VIN3 holds nothing, so it wants nothing

def test_periodic_reenforce_does_not_undo_an_adopted_limit():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); cars(m); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); m.desired.adopt(VIN, CHARGE_LIMIT, 80)
    m.setPolicyLimit(50, transition=False); assert m.desired.get(VIN, CHARGE_LIMIT).value == 80
    onArrival(m); assert m.desired.get(VIN, CHARGE_LIMIT).value == 50

def test_departure_restores_a_held_limit_for_an_absent_car_once():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE))); a, b, d = cars(m); m.heldLimits.set(VIN2, 90, 50)
    m.providerOrder.provider("TeslaBLE").ready = lambda vehicle, cap: vehicle.vin != VIN2            # no BLE claim for the away car
    onDeparture(m); d = m.desired.get(VIN2, CHARGE_LIMIT); assert d.value == 90 and d.restore and m.desired.get(VIN, CHARGE_LIMIT) is None
    onDeparture(m); assert m.desired.get(VIN2, CHARGE_LIMIT) == d                # once: the same desire, not a new one
    r.pass_(); assert m.providerOrder.provider("TeslaBLE").calls == []           # BLE not ready for a car it holds no claim on; nothing sent, nothing paid

def test_policy_thread_calls_set_policy_limit():
    from TWCManager.Policy.Policy import Policy
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); seen = []; m.setPolicyLimit = lambda limit, transition: seen.append((limit, transition))
    p = Policy(m); p.enforcePolicy({"name": "Charge Now", "charge_amps": "value", "value": 10, "charge_limit": 80})
    p.enforcePolicy({"name": "Charge Now", "charge_amps": "value", "value": 10})
    assert seen == [(80, True), (None, False)] and not hasattr(m, "lastChargeLimitApplied")

def test_no_module_holds_a_copy_of_the_policy_limit():
    out = subprocess.run(["git", "grep", "-l", "lastChargeLimitApplied\\|stopTryingToApplyLimit\\|_lastAppliedChargeLimit\\|applyChargeLimit\\|NormalChargeLimit\\|adoptLegacyId", "--", "lib/TWCManager"], capture_output=True, text=True, cwd=str(REPO)).stdout.split()
    assert out == []

# Wiring

def test_master_wires_store_and_reconciler_on_one_clock():
    m, s = make(); assert m.reconciler.master is m and m.desired._clock is time.time and m.desired.changed == m.reconciler.wake
    m.vehicleRegistry.getOrCreate(VIN, "t"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); m.vehicleRegistry.forget(VIN)
    assert m.desired.peek(VIN) is None                                     # registry.removed wired
    assert m.policyLimit is None and m.reconciler.desired is m.desired

def test_debug_route_submits_through_the_master_reconciler():               # ledger L35
    from tests.unit.test_registry_callers import _handler, _post
    m, s = make(); m.providerOrder.register(ble(O.Outcome(O.DONE))); inline(m.reconciler)    # the master's own reconciler
    m.vehicleRegistry.getOrCreate(VIN, "t")
    request = _post(_handler(m), {"commandName": "wakeVehicle", "parameters": "{}", "vehicleVIN": VIN})
    request.send_response.assert_called_with(200)
    assert prov(m, "TeslaBLE").calls == [("wake", VIN, None)]

# Held limits: migrated once, natively on HeldLimit

def legacyApi(settings, *slots):
    master = FakeMaster(); master.vehicleRegistry = VehicleRegistry({}); master.settings = settings
    master.heldLimits = HeldLimits(master); master.heldLimits.load()
    api = new_api(master=master)
    for token in slots: api.storeToken(token)
    return api, master

def listing(*cars):
    return json.dumps({"count": len(cars), "response": [{"id": i, "vin": v, "display_name": None} for i, v in cars]})

def test_legacy_ids_resolve_once_from_the_first_vehicle_list(requests_mock, caplog):
    api, m = legacyApi({"chargeLimits": {VIN: [80, -1], "11": [90, 50], "22": 85, "33": [70, 60]},
                        "heldLimits": {"id:44": {"outside": 75, "held": None}, VIN4: {"outside": 66, "held": None}}},
                       owner_token(), fleet_token())
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)                       # VIN keys at load
    assert m.settings["chargeLimits"] == {"11": [90, 50], "22": 85, "33": [70, 60]} and m.heldLimits.waiting()
    owner = requests_mock.get(OWNER_API_URL[:-8] + "products", text=listing((11, VIN2), (33, VIN4)))
    fleet = requests_mock.get(FLEET_NA_URL, text=listing((22, VIN3)))           # Owner and Fleet: different accounts
    m.tasks.clear()
    import TWCManager.Vehicle.TeslaAPI as teslaapi
    with caplog.at_level(1), caplog.at_level(1, logger=teslaapi.logger.name):    # every level: INFO8/DEBUG lines too
        assert api.loadVehicleList() is True
    assert (owner.call_count, fleet.call_count) == (1, 1)
    assert m.settings["heldLimits"] == {VIN: {"outside": 80, "held": None}, VIN2: {"outside": 90, "held": 50},
                                        VIN3: {"outside": 85, "held": None}, VIN4: {"outside": 66, "held": None}}   # the store's own VIN4 wins
    assert "chargeLimits" not in m.settings and not m.heldLimits.waiting() and m.tasks == [{"cmd": "saveSettings"}]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "44" in warned[0] and "75%" in warned[0]       # unmatched: dropped, one warning naming the ID and its limit
    assert any("33" in r.getMessage() and "70%" in r.getMessage() and VIN4 in r.getMessage() for r in caplog.records if r.levelno == logging.INFO)   # the discard is logged (M5)
    assert "Car API cmd vehicles" in caplog.text and "Car API vehicle list" in caplog.text    # the INFO8 and DEBUG lines were captured
    assert not any(t in caplog.text for t in (api.ownerSlot.accessToken, api.fleetSlot.accessToken))
    api.carApiVehicles = []; api.loadVehicleList(); assert (owner.call_count, fleet.call_count) == (2, 1)   # once: nothing waits, no second list

def test_an_empty_vehicle_list_drops_nothing(requests_mock):
    api, m = legacyApi({"chargeLimits": {"11": [90, 50]}}, fleet_token())
    requests_mock.get(FLEET_NA_URL, text=listing())
    assert api.loadVehicleList() is True and m.heldLimits.waiting() and m.settings["chargeLimits"] == {"11": [90, 50]}

def test_a_failed_second_list_defers_the_drop(requests_mock):
    api, m = legacyApi({"chargeLimits": {"11": [90, 50]}}, owner_token(), fleet_token())
    requests_mock.get(OWNER_API_URL[:-8] + "products", text=listing((12, VIN2)))
    requests_mock.get(FLEET_NA_URL, status_code=500, text="{}")
    assert api.loadVehicleList() is True and m.heldLimits.waiting() and "chargeLimits" in m.settings
    requests_mock.get(FLEET_NA_URL, text=listing((11, VIN3)))                  # the next checkCharge retries: free calls only
    assert api.updateChargeAtHome() is True and m.heldLimits.get(VIN3) == HeldLimit(90, 50) and "chargeLimits" not in m.settings
    assert [q.method for q in requests_mock.request_history] == ["GET"] * 4

def test_an_unresolved_id_survives_a_restart():
    settings = {"chargeLimits": {VIN: [80, -1], "11": [90, 50]}}
    api, m = legacyApi(settings); saved = json.loads(json.dumps(m.settings))
    api2, m2 = legacyApi(saved); assert m2.heldLimits.waiting() and m2.heldLimits.get(VIN) == HeldLimit(80, None)

def test_vehicle_list_loads_without_any_legacy_command(requests_mock):       # C1, paid counted
    master = FakeMaster(); master.vehicleRegistry = VehicleRegistry({}); api = new_api(master=master); api.storeToken(fleet_token())
    requests_mock.get(FLEET_NA_URL, text=listing((1, VIN)))
    assert api.updateChargeAtHome() is True and api.updateChargeAtHome() is True
    calls = [(q.method, q.url) for q in requests_mock.request_history]
    assert calls == [("GET", FLEET_NA_URL)]                                     # one free GET /vehicles; no vehicle_data, no wake_up
    fleetProvider = api.providers()[1]
    assert fleetProvider.ready(master.vehicleRegistry.get(VIN), C.STOP_CHARGING)

def test_legacy_fleet_start_sends_no_limit_first(monkeypatch, requests_mock):   # pre-flight M10, paid counted
    import TWCManager.Vehicle.TeslaAPI as teslaapi
    from unittest.mock import Mock
    monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
    api = new_api(); api.storeToken(fleet_token()); api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    car = api.getCarApiVehicles()[0]; car.atHome = True; car.lastAPIAccessTime = time.time(); car.chargingState = "Stopped"
    car.batteryLevel = 50; car.chargeLimit = 90; car.update_charge = Mock(return_value=True); car.update_location = Mock(return_value=True)
    setattr(api, "lastChargeLimitApplied", 60)                                  # the copy BASE kept and sent before every start
    requests_mock.register_uri("POST", re.compile(r".*/command/.*"), json={"response": {"result": True, "reason": ""}})
    api.car_api_charge({"cmd": "charge", "charge": True, "vin": VIN})
    assert [r.url.split("/")[-1] for r in requests_mock.request_history] == ["charge_start"]

# Rules and the held record

def test_rules_need_no_sentinel():
    source = (REPO / "lib/TWCManager/Vehicle/Reconcile/Rules.py").read_text()
    assert re.findall(r"-1(?!\d)|lastApplied", source) == []
    from tests.unit.test_charge_limit_rules import CASES, call
    for case in CASES:                                                         # the ported table is the oracle
        _, limit, current, saved, unknown, send, saveNow, saveOnSuccess, settled = case
        d = call(limit, current, saved, unknown)
        assert (d.send, d.saveNow, d.saveOnSuccess, d.settled) == (send, saveNow, saveOnSuccess, settled), case[0]

def test_a_satisfied_restore_removes_the_held_record():                    # ledger c6
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True)
    assert r.pass_() == 0 and m.heldLimits.get(VIN) is None and m.heldLimits.holding() == []
    assert m.desired.get(VIN, CHARGE_LIMIT) is None and prov(m, "TeslaBLE").calls == []

# Fix round 1

FLEET_CAR = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/" + VIN

def fleetCarOffItsPoint(requests_mock, home):
    # A Fleet-only car we hold at 50 (normal 90), reported by its TWC, then
    # unplugged; it reads as at home or away
    from unittest.mock import Mock
    m, r, v, c = setup(clock=False); api = new_api(master=m); api.storeToken(fleet_token())
    for p in api.providers(): m.providerOrder.register(p)
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"}); car = api.getCarApiVehicles()[0]
    api.is_location_home = Mock(return_value=home); api.is_far_from_home = Mock(return_value=False)
    car.atHome = True; v.observe("twcVin", "8244", "TWC"); v.observe("twcVin", None, "TWC", seen=False)
    m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 50, "TeslaAPI")
    requests_mock.get(FLEET_CAR, json={"response": {"state": "online"}})
    requests_mock.get(FLEET_CAR + "/vehicle_data", json={"response": {"drive_state": {"latitude": 1.0, "longitude": 2.0, "shift_state": "D"},
                                                                    "charge_state": {"charge_limit_soc": 50}, "vehicle_state": {}}})
    requests_mock.post(re.compile(r".*/command/.*"), json={"response": {"result": True, "reason": ""}})
    requests_mock.post(re.compile(r".*/wake_up"), json={"response": {"state": "online"}})
    return m, r, car

def paid(requests_mock):
    return [q.url.split("?")[0].split("/")[-1] for q in requests_mock.request_history if q.url.split("?")[0] != FLEET_CAR]

def test_a_fleet_car_that_unplugs_and_leaves_gets_its_normal_limit_back(requests_mock):   # C1, paid counted
    m, r, car = fleetCarOffItsPoint(requests_mock, home=False)
    onDeparture(m); d = m.desired.get(VIN, CHARGE_LIMIT)
    assert car.atHome is False and d is not None and d.restore and d.value == 90
    r.pass_()
    assert paid(requests_mock) == ["vehicle_data", "set_charge_limit"]                  # one read, one command, never a wake
    assert requests_mock.request_history[-1].json() == {"percent": 90}

def test_an_unplug_at_home_is_not_a_departure(requests_mock):
    m, r, car = fleetCarOffItsPoint(requests_mock, home=True)
    onDeparture(m); r.pass_()
    assert m.desired.get(VIN, CHARGE_LIMIT) is None and paid(requests_mock) == ["vehicle_data"]

def test_a_policy_change_while_the_car_is_away_sends_nothing(requests_mock):  # C1, paid counted
    m, r, car = fleetCarOffItsPoint(requests_mock, home=False)
    onDeparture(m); r.pass_(); before = len(requests_mock.request_history)
    m.setPolicyLimit(60, transition=True); r.pass_()
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 90 and len(requests_mock.request_history) == before

def test_a_limit_recorded_while_an_id_waits_keeps_the_legacy_normal(caplog):  # I1
    m, s = make(); m.settings["chargeLimits"] = {"11": [90, 50]}; m.heldLimits.load()
    m.heldLimits.set(VIN, 50, 60)                                               # the reconciler, from a car still at our old 50
    with caplog.at_level(logging.INFO):
        m.heldLimits.resolveLegacyIds({"11": VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(90, 60) and "50%" in caplog.text and "90%" in caplog.text

def test_a_failed_boot_list_load_recovers_without_an_arrival(requests_mock):  # I2
    master = FakeMaster(); master.vehicleRegistry = VehicleRegistry({}); api = new_api(master=master); api.storeToken(fleet_token())
    requests_mock.get(FLEET_NA_URL, [{"status_code": 500, "text": "{}"}, {"text": listing((1, VIN))}])
    api.updateChargeAtHome(); assert api.getVehicleCount() == 0 and api.listPending()   # the boot checkCharge failed
    api.loadList(); assert api.getVehicleCount() == 1 and not api.listPending()      # the periodic retry
    assert [q.method for q in requests_mock.request_history] == ["GET", "GET"]
    source = (REPO / "lib/TWCManager/TWCManager.py").read_text()
    tick = source[source.index("master.lastTeslaTokenRefreshCheck = time.time()"):]
    assert tick.index("carapi.listPending()") < tick.index('{"cmd": "loadVehicleList"}') < 600
    assert 'task["cmd"] == "loadVehicleList"' in source and "carapi.loadList()" in source
