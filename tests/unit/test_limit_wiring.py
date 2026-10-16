# tests/unit/test_limit_wiring.py
import json
import logging
import re
import subprocess
import time
from pathlib import Path

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit, HeldLimits
from TWCManager.Vehicle.Reconcile.Targets import applyPolicyLimit, broadcastVehicles, onArrival, onDeparture, presentVehicles, targetsFor
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from tests.unit.gen2_helpers import drive as heartbeat
from tests.unit.reconcile_helpers import AWAY, HOME, inline, placeAt
from tests.unit.teslatokens import FLEET_NA_URL, OWNER_API_URL, FakeMaster, fleet_token, new_api, owner_token
from tests.unit.test_gen2twc_provider import make
from tests.unit.test_limit_reconciler import VIN, ble, prov, setup
VIN2 = "5YJ3E1EA1KF000002"; VIN3 = "5YJ3E1EA1KF000003"; VIN4 = "5YJ3E1EA1KF000004"
REPO = Path(__file__).resolve().parents[2]

def cars(m):
    reg = m.vehicleRegistry; a = reg.get(VIN); a.observe("twcVin", "8244", "TWC")
    new_api(master=m).addVehicle({"id": 2, "vin": VIN2, "display_name": None}); b = reg.get(VIN2); placeAt(m, b, AWAY)   # the API car's position says away
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
    assert "Car API get vehicles: HTTP 200" in caplog.text                       # the INFO8 lines were captured
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
    api.updateChargeAtHome(); assert m.heldLimits.get(VIN3) == HeldLimit(90, 50) and "chargeLimits" not in m.settings
    assert [q.method for q in requests_mock.request_history] == ["GET"] * 4

def test_an_unresolved_id_survives_a_restart():
    settings = {"chargeLimits": {VIN: [80, -1], "11": [90, 50]}}
    api, m = legacyApi(settings); saved = json.loads(json.dumps(m.settings))
    api2, m2 = legacyApi(saved); assert m2.heldLimits.waiting() and m2.heldLimits.get(VIN) == HeldLimit(80, None)

def test_vehicle_list_loads_without_any_legacy_command(requests_mock):       # C1, paid counted
    master = FakeMaster(); master.vehicleRegistry = VehicleRegistry({}); api = new_api(master=master); api.storeToken(fleet_token())
    requests_mock.get(FLEET_NA_URL, text=listing((1, VIN)))
    api.updateChargeAtHome(); api.updateChargeAtHome()
    calls = [(q.method, q.url) for q in requests_mock.request_history]
    assert calls == [("GET", FLEET_NA_URL)]                                     # one free GET /vehicles; no vehicle_data, no wake_up
    fleetProvider = api.providers()[1]
    assert fleetProvider.ready(master.vehicleRegistry.get(VIN), C.STOP_CHARGING)

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
    # unplugged; the paid read places it at home or away
    m, r, v, c = setup(clock=False); api = new_api(master=m); api.storeToken(fleet_token())
    for p in api.providers(): m.providerOrder.register(p)
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"}); car = api.getCarApiVehicles()[0]
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1]); at = HOME if home else AWAY
    v.observe("twcVin", "8244", "TWC"); v.observe("twcVin", None, "TWC", seen=False)
    m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 50, "TeslaAPI")
    requests_mock.get(FLEET_CAR, json={"response": {"state": "online"}})
    requests_mock.get(FLEET_CAR + "/vehicle_data", json={"response": {"drive_state": {"latitude": at[0], "longitude": at[1], "shift_state": "D"},
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

def test_live_telemetry_places_a_departing_car_for_free(requests_mock):   # main's telemetry update_location, paid counted
    m, r, car = fleetCarOffItsPoint(requests_mock, home=True)                                     # awake: a paid read would go out, and say home
    v = m.vehicleRegistry.get(VIN); v.observe("state", "online", "TeslaMateVehicle"); placeAt(m, v, AWAY)
    onDeparture(m); d = m.desired.get(VIN, CHARGE_LIMIT)
    assert car.syncSource == "TeslaMateVehicle" and d.restore and d.value == 90 and requests_mock.call_count == 0

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

# Restart presence (slim Task 12c): the 2026-10-16 live failure

def restarted(requests_mock, position=HOME, held=True, claim=True):
    # The live box after a restart: commandPolicy ble_only; a car the Owner and Fleet tokens
    # list and BLE has paired, held at 50 (normal 100) by the migrated record; asleep at SOC 51
    # with TeslaMate's retained position. Nothing sets atHome: presence comes from position
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); m.settings.update(commandPolicy="ble_only", homeLat=HOME[0], homeLon=HOME[1])
    api = new_api(master=m); api.storeToken(owner_token()); api.storeToken(fleet_token()); del m.modules["TeslaAPI"]   # not a module record
    for p in api.providers(): m.providerOrder.register(p)
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "TestCar"})
    if claim: v.claim("TeslaBLE", True, "paired")
    if held: m.heldLimits.set(VIN, 100, 50)
    if position: placeAt(m, v, position)
    for field, value in (("chargeLimit", 50), ("chargingState", "Complete"), ("batteryLevel", 51)): v.observe(field, value, "TeslaMateVehicle")
    from requests_mock import ANY
    requests_mock.register_uri(ANY, ANY, json={"response": {"state": "asleep"}})   # any request is counted, none fails
    return m, r, v, api.getCarApiVehicles()[0]

def paidCalls(requests_mock):
    return [q.url for q in requests_mock.request_history if re.search(r"vehicle_data|wake_up|/command/", q.url)]

def test_a_restart_with_the_car_plugged_in_and_asleep_still_gets_the_policy_limit(requests_mock):   # live 2026-10-16, paid counted
    m, r, v, car = restarted(requests_mock)
    m.setPolicyLimit(70, transition=True)                                                        # Scheduled, on the first pass
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 70 and car.atHome is True                  # located for free: TeslaMate's position
    r.pass_(); b = prov(m, "TeslaBLE")
    assert b.calls == [("wake", VIN, None), ("setChargeLimit", VIN, 70)]                         # a raise wakes over BLE, then sends
    assert m.heldLimits.get(VIN) == HeldLimit(100, 70)
    assert paidCalls(requests_mock) == [] and requests_mock.call_count == 0                     # presence cost nothing; Fleet/Owner never commanded
    startAfterRaise(m, r)                                                                        # TeslaMate's "Complete" predates the raise (parity row 26)
    i = b.calls.index(("startCharging", VIN, None)); assert [x[0] for x in b.calls].count("startCharging") == 1   # once, over BLE
    assert b.calls[i - 1] == ("wake", VIN, None) and b.calls.index(("setChargeLimit", VIN, 70)) < i  # after the limit, with its wake
    assert requests_mock.call_count == 0

def startAfterRaise(m, r):
    # The VIN-less Gen2 at 03 asks for a start; passes past the limit-then-start gap
    s = m.getSlaveTWCs()[0]; s.protocolVersion = 2; m.setMaxAmpsToDivideAmongSlaves(32); c = m.vehicleRegistry._clock
    for _ in range(6): heartbeat(m, s, 0.0, state=0x03); c.t += 30; r.pass_()

def test_a_complete_read_after_the_raise_still_satisfies_the_start(requests_mock):   # parity row 26
    m, r, v, car = restarted(requests_mock); m.setPolicyLimit(70, transition=True); r.pass_()
    m.vehicleRegistry._clock.t += 1; v.observe("chargingState", "Complete", "TeslaMateVehicle")   # read after our write: really complete
    startAfterRaise(m, r)
    assert "startCharging" not in [x[0] for x in prov(m, "TeslaBLE").calls] and requests_mock.call_count == 0

def test_a_held_car_with_no_known_position_is_present(requests_mock):   # main's wasAtHome
    m, r, v, car = restarted(requests_mock, position=None)
    m.setPolicyLimit(70, transition=True); r.pass_()
    assert ("setChargeLimit", VIN, 70) in prov(m, "TeslaBLE").calls and requests_mock.call_count == 0
    m, r, v, car = restarted(requests_mock, position=None, held=False, claim=False)              # nothing says home: absent, nothing woken
    assert presentVehicles(m, C.SET_CHARGE_LIMIT) == [] and m.setPolicyLimit(70, transition=True) is None
    assert m.desired.get(VIN, CHARGE_LIMIT) is None
    v.claim("TeslaBLE", True, "paired"); assert presentVehicles(m, C.SET_CHARGE_LIMIT) == [v]  # the BLE claim still applies

def test_a_held_car_a_free_position_places_away_gets_its_normal_limit_back(requests_mock):   # paid counted
    m, r, v, car = restarted(requests_mock, position=AWAY)
    m.setPolicyLimit(70, transition=False)                                                       # any policy pass, no edge
    d = m.desired.get(VIN, CHARGE_LIMIT); assert d.restore and d.value == 100 and car.atHome is False
    m.setPolicyLimit(70, transition=False); assert m.desired.get(VIN, CHARGE_LIMIT) == d         # once: the same desire
    r.pass_(); assert prov(m, "TeslaBLE").calls == [("setChargeLimit", VIN, 100)]               # a restore never wakes
    assert requests_mock.call_count == 0

def test_a_vinless_gen2_after_a_restart_is_an_anonymous_plug_session(requests_mock):   # main's unaddressed lift and start, paid counted
    m, r, v, car = restarted(requests_mock); s = m.getSlaveTWCs()[0]; s.protocolVersion = 2; m.setMaxAmpsToDivideAmongSlaves(32)
    m.setPolicyLimit(70, transition=True)
    for _ in range(2): heartbeat(m, s, 0.0, state=0x03)                                         # plugged in, asleep, drawing nothing; no VIN
    assert s.vehicleKey == "anon:8244" and s.vehicleRateRaised
    lift, start = m.desired.get(VIN, AMPS), m.desired.get(VIN, CHARGING)
    assert lift.lift and lift.value == s.wiringMaxAmps and start.value is True                   # the fan-out reaches the BLE car
    for _ in range(3): r.pass_()
    b = prov(m, "TeslaBLE")
    assert b.calls == [("wake", VIN, None), ("setChargeLimit", VIN, 70),                        # the limit first
                       ("wake", VIN, None), ("setAmps", VIN, s.wiringMaxAmps)]                   # the BLE lift may wake the car, as main's; the start waits out the limit gap
    assert requests_mock.call_count == 0
    v.observe("chargingState", "Disconnected", "TeslaMateVehicle")
    heartbeat(m, s, 0.0, state=0x00)                                                             # 00 and no car known plugged in: the session ends
    assert s.vehicleKey is None and m.vehicleRegistry.get("anon:8244") is None
    heartbeat(m, s, 0.0, state=0x00); assert m.vehicleRegistry.get("anon:8244") is None          # 00 never begins one

def test_a_car_its_twc_names_stays_at_home_whatever_its_stale_fix_says(requests_mock):   # vehicleIsDefinitelyHome; Gen3 endpoints read atHome
    m, r, v, car = restarted(requests_mock, position=AWAY); v.observe("twcVin", "8244", "TWC"); car.atHome = True   # as vehicleIsDefinitelyHome sets it
    assert broadcastVehicles(m, C.START_CHARGING) == [v] and presentVehicles(m, C.SET_CHARGE_LIMIT) == [v]
    m.setPolicyLimit(70, transition=False); assert car.atHome is True and not m.desired.get(VIN, CHARGE_LIMIT).restore   # no restore, no flip

def test_a_held_car_with_no_position_or_ble_is_present_for_a_cloud_provider(requests_mock):   # main's wasAtHome without BLE (review I1)
    m, r, v, car = restarted(requests_mock, position=None, claim=False); m.settings["commandPolicy"] = "prefer_ble"
    assert presentVehicles(m, C.SET_CHARGE_LIMIT) == [v]                                          # Fleet is the only candidate
    m.setPolicyLimit(70, transition=True); assert m.desired.get(VIN, CHARGE_LIMIT).value == 70 and requests_mock.call_count == 0
    m, r, v, car = restarted(requests_mock, position=None, held=False, claim=False); m.settings["commandPolicy"] = "prefer_ble"
    assert presentVehicles(m, C.SET_CHARGE_LIMIT) == []                                           # holding nothing: absent

def test_an_away_fix_older_than_our_last_limit_write_is_no_departure(requests_mock):   # review M1
    m, r, v, car = restarted(requests_mock, position=AWAY); c = m.vehicleRegistry._clock
    c.t += 1; v.noteWrite("chargeLimit", 50, "TeslaBLE")                                         # we wrote the hold after that fix
    m.setPolicyLimit(70, transition=False); assert m.desired.get(VIN, CHARGE_LIMIT) is None      # away, but the fix predates the hold
    c.t += 1; placeAt(m, v, AWAY); m.setPolicyLimit(70, transition=False)                        # a newer fix shows the departure
    d = m.desired.get(VIN, CHARGE_LIMIT); assert d.restore and d.value == 100 and requests_mock.call_count == 0

def test_an_away_fix_stamped_at_our_write_instant_is_no_departure(requests_mock):   # slim Task 13: the <= boundary
    m, r, v, car = restarted(requests_mock, position=AWAY); c = m.vehicleRegistry._clock
    v.noteWrite("chargeLimit", 50, "TeslaBLE")                                                   # a retained position redelivered with our write's stamp
    assert v.lastWrite("chargeLimit").at == v.latest("lat").at
    m.setPolicyLimit(70, transition=False); assert m.desired.get(VIN, CHARGE_LIMIT) is None      # equal stamps: no departure
    c.t += 0.001; placeAt(m, v, AWAY); m.setPolicyLimit(70, transition=False)                    # any later fix shows it
    assert m.desired.get(VIN, CHARGE_LIMIT).restore

def test_at_00_a_plugged_car_a_free_source_shows_at_home_begins_the_session(requests_mock):   # review I2, paid counted
    m, r, v, car = restarted(requests_mock); s = m.getSlaveTWCs()[0]; s.protocolVersion = 2; m.setMaxAmpsToDivideAmongSlaves(32)
    heartbeat(m, s, 0.0, state=0x00)                                                             # asleep, plugged in: TeslaMate says "Complete" at home
    assert s.vehicleKey == "anon:8244" and m.desired.get(VIN, AMPS).lift and requests_mock.call_count == 0
    v.observe("chargingState", "Disconnected", "TeslaMateVehicle"); heartbeat(m, s, 0.0, state=0x00)
    assert s.vehicleKey is None and m.vehicleRegistry.get("anon:8244") is None                   # unplugged: the session ends

def test_an_empty_twc_at_00_begins_no_session(requests_mock):   # review I2
    for position, state in ((HOME, "Disconnected"), (AWAY, "Complete")):                         # at home unplugged; plugged in elsewhere
        m, r, v, car = restarted(requests_mock, position=position); s = m.getSlaveTWCs()[0]; s.protocolVersion = 2
        v.observe("chargingState", state, "TeslaMateVehicle"); m.setMaxAmpsToDivideAmongSlaves(32)
        for _ in range(2): heartbeat(m, s, 0.0, state=0x00)
        assert s.vehicleKey is None and m.desired.get(VIN, AMPS) is None and m.desired.get(VIN, CHARGING) is None
