# tests/unit/test_charging_reconciler.py: charging on/off is stateless
# desired state, decided by the Gen2 heartbeat each cycle and delivered by
# the reconciler (slim Task 12a)
import re
import threading
import time
from unittest.mock import Mock

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT, CHARGING
from TWCManager.Vehicle.Reconcile.Reconciler import LIMIT_THEN_START_GAP, PAID_AFTER, PROBE_AFTER, RETRY
from tests.unit.gen2_helpers import drive
from tests.unit.reconcile_helpers import ScriptedProvider
from tests.unit.teslatokens import fleet_token, new_api
from tests.unit.test_limit_reconciler import VIN, ble, cmds, fleet, setup

def onTwc(m, v):
    v.observe("twcVin", "8244", "TWC"); s = slave(m); s.currentVIN = VIN; return s

def slave(m):
    # Only a protocol 2 TWC decides start and stop, as on main
    s = m.getSlaveTWCs()[0]; s.protocolVersion = 2; return s

def ready_to_stop(s):
    # The heartbeat's 60 s gates (time.time) have run: the car drew this much for a minute
    s.reportedAmpsActualSignificantChangeMonitor = 12.0
    s.timeLastAmpsOfferedChanged = s.timeReportedAmpsActualChangedSignificantly = time.time() - 61

# The heartbeat and the master

def test_heartbeat_stop_is_sent_once_and_a_stale_read_does_not_reassert():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Charging", "TeslaBLE"); b = m.providerOrder.provider("TeslaBLE")
    for _ in range(5): m.stopCarsCharging(VIN)
    assert r.pass_() == 1 and b.calls == [("stopCharging", VIN, None)] and v.believed("chargingState").value == "Stopped" and m.desired.state(VIN).lastStopProvider == "TeslaBLE"
    assert r.pass_() == 0
    c.t += 10; v.observe("chargingState", "Charging", "TeslaBLE", at=v.lastWrite("chargingState").at)      # same instant as our write
    for _ in range(5): c.t += 61; assert r.pass_() == 0
    assert m.desired.get(VIN, CHARGING).value is False and len(b.calls) == 1

def test_heartbeat_skips_the_registry_write_for_a_pending_vin():             # S1
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); s = slave(m); s.currentVIN = "5YJ3E1EA1KF000009"                # heard, not yet confirmed: not in the registry
    drive(m, s, amps_actual=12.0); assert m.vehicleRegistry.get("5YJ3E1EA1KF000009") is None                                 # no raise, nothing created
    s.currentVIN = VIN; drive(m, s, amps_actual=12.0); assert v.latest("actualCurrent").value == 12.0 and v.latest("actualCurrent").source == "Gen2TWC"

def test_heartbeat_readings_are_not_evidence_for_a_car_with_vehicle_reads():   # S4, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.SATISFIED, "not_charging"))); s = onTwc(m, v); f = m.providerOrder.provider("FleetAPI")
    v.observe("chargingState", "Charging", "TeslaAPI"); m.stopCarsCharging(VIN); r.pass_(); assert cmds(f) == ["stopCharging"]
    for _ in range(10): c.t += 61; drive(m, s, amps_actual=12.0); r.pass_()                                                # readings every heartbeat, past RETRY each time
    assert cmds(f) == ["stopCharging"]                                                                                    # no vehicle read arrived: not re-sent

def test_heartbeat_stop_targets_the_anonymous_car():                         # real TWCSlave heartbeat, non-Tesla car
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); s = slave(m); anon = m.vehicleRegistry.beginPlugSession("8244"); m.settings["chargeStopMode"] = "3"; m.sendStopCommand = Mock()
    m.setMaxAmpsToDivideAmongSlaves(0); drive(m, s, amps_actual=12.0); ready_to_stop(s); drive(m, s, amps_actual=12.0)   # offer 0, the car still draws past the gates
    assert m.desired.get(anon.key, CHARGING).value is False and r.pass_() == 1 and s.stopRequested
    drive(m, s, amps_actual=12.0); m.sendStopCommand.assert_called_once_with(s.TWCID)                                       # sent by the heartbeat, on its thread

def test_heartbeat_with_no_vehicle_key_decides_nothing():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); s = slave(m); v.observe("chargingState", "Stopped", "TeslaBLE")
    m.desired.set(VIN, CHARGING, False, "distributor"); m.setMaxAmpsToDivideAmongSlaves(32)
    for _ in range(3): drive(m, s, amps_actual=0.0)
    assert s.vehicleKey is None and m.desired.get(VIN, CHARGING).value is False                                            # never a broadcast start or cancel

def test_start_waits_five_seconds_after_a_limit_write_and_wakes():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE), O.Outcome(O.DONE))); onTwc(m, v); b = m.providerOrder.provider("TeslaBLE")
    v.observe("chargeLimit", 50, "TeslaBLE"); v.observe("chargingState", "Stopped", "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy"); m.startCarsCharging(VIN); r.pass_()
    assert [x[0] for x in b.calls] == ["wake", "setChargeLimit"] and r.pass_() == 0
    c.t += LIMIT_THEN_START_GAP + 1; r.pass_(); assert [x[0] for x in b.calls][2:] == ["wake", "startCharging"]

def test_complete_or_disconnected_car_is_left_alone():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Disconnected", "TeslaBLE"); m.startCarsCharging(VIN)
    assert r.pass_() == 0 and m.providerOrder.provider("TeslaBLE").calls == []
    v.observe("chargingState", "Complete", "TeslaBLE"); assert r.pass_() == 0 and m.providerOrder.provider("TeslaBLE").calls == []

def test_stop_falls_to_fleet_after_60s_once():                               # Review Focus 1
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Charging", "TeslaAPI"); f = m.providerOrder.provider("FleetAPI")
    m.stopCarsCharging(VIN); r.pass_(); assert f.calls == [] and len(m.providerOrder.provider("TeslaBLE").calls) == 1
    c.t += PAID_AFTER[CHARGING] - 1; r.pass_(); assert f.calls == []
    c.t += 1; r.pass_(); assert f.calls == [("stopCharging", VIN, None)]
    c.t += 3600; r.pass_(); assert len(f.calls) == 1

def test_disruptive_stop_waits_for_ble_only_when_ble_is_set_up_for_the_car():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Charging", "TeslaAPI")
    m.settings["chargeStopMode"] = "2"; m.stopCarsCharging(VIN); r.pass_()
    assert m.settings.get("respondToSlaves", 1) == 1 and len(m.providerOrder.provider("TeslaBLE").calls) == 1   # BLE ready for this car: tried first
    c.t += PAID_AFTER[CHARGING]; r.pass_(); assert m.settings["respondToSlaves"] == 0 and m.providerOrder.provider("FleetAPI").calls == []   # billed stops suppressed under mode 2
    m2, r2, v2, c2 = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); onTwc(m2, v2); v2.observe("chargingState", "Charging", "TeslaAPI")
    m2.providerOrder.provider("TeslaBLE").ready = lambda vehicle, cap: False                                   # no key, or a refused one
    m2.settings["chargeStopMode"] = "2"; m2.stopCarsCharging(VIN); r2.pass_()
    assert m2.settings["respondToSlaves"] == 0 and m2.providerOrder.provider("TeslaBLE").calls == []           # at once

def test_disruptive_stop_is_sent_once_until_new_evidence_then_reasserted():   # real heartbeat readings
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); s = slave(m); anon = m.vehicleRegistry.beginPlugSession("8244"); m.settings["chargeStopMode"] = "2"
    drive(m, s, amps_actual=12.0); assert anon.latest("actualCurrent").value == 12.0 and anon.latest("actualCurrent").source == "Gen2TWC"   # the heartbeat records current
    m.stopCarsCharging(anon.key); assert r.pass_() == 1 and m.settings["respondToSlaves"] == 0 and anon.lastWrite("chargingState") is None     # never stamped
    m.settings["respondToSlaves"] = 1
    for _ in range(5): assert r.pass_() == 0                                                                    # no new reading: not re-sent
    c.t += 30; drive(m, s, amps_actual=12.0); assert r.pass_() == 0 and m.settings["respondToSlaves"] == 1         # newer reading, inside RETRY: held
    c.t += 31; drive(m, s, amps_actual=12.0); m.stopCarsCharging(anon.key); assert r.pass_() == 1 and m.settings["respondToSlaves"] == 0   # resumed, past RETRY: re-stopped
    m.settings["respondToSlaves"] = 1; c.t += 61; drive(m, s, amps_actual=0.5); assert r.pass_() == 0 and m.settings["respondToSlaves"] == 1   # quiet bus: satisfied

def test_disruptive_stop_on_the_real_clock_is_sent_once():                    # production clock
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), clock=False); s = slave(m); anon = m.vehicleRegistry.beginPlugSession("8244"); m.settings["chargeStopMode"] = "3"; m.sendStopCommand = Mock()
    drive(m, s, amps_actual=12.0); m.stopCarsCharging(anon.key)
    sent = sum(r.pass_() for _ in range(20)); drive(m, s, amps_actual=12.0)
    assert sent == 1 and m.sendStopCommand.call_count == 1 and not s.stopRequested                                # one relay stop, not one per pass

def test_mode_2_start_after_a_ble_stop_goes_through_ble():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE), O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Charging", "TeslaBLE"); m.settings["chargeStopMode"] = "2"
    m.stopCarsCharging(VIN); r.pass_(); assert m.desired.state(VIN).lastStopProvider == "TeslaBLE"
    c.t += 61; m.startCarsCharging(VIN); r.pass_(); assert cmds(m.providerOrder.provider("TeslaBLE")) == ["stopCharging", "startCharging"] and m.settings.get("respondToSlaves", 1) == 1

def test_mode_2_start_after_our_stop_needs_no_slave():                      # ledger L50
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); s = onTwc(m, v); f = m.providerOrder.provider("FleetAPI"); v.observe("chargingState", "Charging", "TeslaAPI"); m.settings["chargeStopMode"] = "2"
    m.stopCarsCharging(VIN); r.pass_(); assert m.settings["respondToSlaves"] == 0 and m.desired.state(VIN).lastStopProvider == "Gen2TWC"
    m.slaveTWCs.pop(s.TWCID); m.slaveTWCRoundRobin.remove(s); assert m.getSlaveTWCs() == []     # aged out about 26 s after the stop
    c.t += 61; v.observe("chargingState", "Stopped", "TeslaMateVehicle"); m.startCarsCharging(VIN); r.pass_()
    assert m.settings["respondToSlaves"] == 1 and f.calls == []

def test_non_tesla_car_on_a_twc_gets_the_disruptive_stop_at_once():
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), fleet(O.Outcome(O.DONE))); s = slave(m); anon = m.vehicleRegistry.beginPlugSession("8244"); m.settings["chargeStopMode"] = "2"
    anon.observe("actualCurrent", 12.0, "Gen2TWC"); m.stopCarsCharging(anon.key); assert r.pass_() == 1 and m.settings["respondToSlaves"] == 0
    assert m.providerOrder.provider("TeslaBLE").calls == [] and m.providerOrder.provider("FleetAPI").calls == []
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); anon = m.vehicleRegistry.beginPlugSession("8244"); m.settings["chargeStopMode"] = "3"
    anon.observe("actualCurrent", 12.0, "Gen2TWC"); m.stopCarsCharging(anon.key); assert r.pass_() == 1 and m.getSlaveTWCs()[0].stopRequested

def test_gen3_multi_unit_stop_uses_the_car_not_the_bus():
    from tests.unit.test_gen3twc_provider import make as make_gen3, vitals
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); g, ctl, gp = make_gen3(("10.0.0.1", "10.0.0.2")); gp.master = m; m.providerOrder.register(gp)
    a, b = ctl.devices(); vitals(a, True); vitals(b, True); car = m.vehicleRegistry.getOrCreate(VIN, "t"); car.observe("pluggedIn", True, "TeslaMateVehicle")
    gp.unitFor = lambda vehicle: a if vehicle.vin == VIN else None; gp.limitations = lambda vehicle: frozenset({C.NO_INDIVIDUAL_STOP})
    v.observe("chargingState", "Charging", "TeslaBLE"); n = ctl.setHouseWatts.call_count
    m.stopCarsCharging(VIN); r.pass_(); assert m.providerOrder.provider("TeslaBLE").calls == [("stopCharging", VIN, None)] and ctl.setHouseWatts.call_count == n

# Fallback and paid counts

def test_shared_free_failure_lets_a_later_aspect_go_paid_at_once():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); onTwc(m, v); f = m.providerOrder.provider("FleetAPI")
    v.observe("chargeLimit", 90, "TeslaAPI"); v.observe("chargingState", "Stopped", "TeslaAPI"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    r.pass_(); c.t += PAID_AFTER[CHARGE_LIMIT]; r.pass_(); assert cmds(f) == ["setChargeLimit"]
    c.t += LIMIT_THEN_START_GAP + 1; m.startCarsCharging(VIN); r.pass_(); assert cmds(f) == ["setChargeLimit", "startCharging"]   # no fresh 60 s wait

def test_free_failure_windows_run_concurrently_not_summed():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); onTwc(m, v); f = m.providerOrder.provider("FleetAPI")
    v.observe("chargeLimit", 90, "TeslaAPI"); v.observe("chargingState", "Charging", "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); m.stopCarsCharging(VIN); r.pass_()
    c.t += PAID_AFTER[CHARGING]; r.pass_(); assert cmds(f) == ["stopCharging"]
    c.t += PAID_AFTER[CHARGE_LIMIT] - PAID_AFTER[CHARGING]; r.pass_(); assert cmds(f) == ["stopCharging", "setChargeLimit"]

def test_a_free_success_puts_ble_first_again():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE), O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)), fleet(O.Outcome(O.DONE), O.Outcome(O.DONE))); onTwc(m, v); b = m.providerOrder.provider("TeslaBLE"); f = m.providerOrder.provider("FleetAPI")
    v.observe("chargeLimit", 90, "TeslaAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    m.startCarsCharging(VIN); r.pass_(); c.t += PAID_AFTER[CHARGING]; r.pass_(); assert cmds(f) == ["startCharging"]   # BLE's wake and start consumed the two UNREACHABLEs
    c.t += PROBE_AFTER; m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()                                    # untried long enough: the real command over BLE first
    assert cmds(b) == ["startCharging", "setChargeLimit"] and len(cmds(f)) == 1 and m.desired.free(VIN).failingSince is None   # a BLE DONE
    m.desired.set(VIN, CHARGE_LIMIT, 60, "policy"); c.t += 10; r.pass_(); assert cmds(b)[-1] == "setChargeLimit" and len(cmds(f)) == 1

def test_paid_calls_never_exceed_desired_changes():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); onTwc(m, v); f = m.providerOrder.provider("FleetAPI")
    v.observe("chargeLimit", 90, "TeslaAPI"); v.observe("chargingState", "Stopped", "TeslaAPI"); changes = 0
    for limit, start in ((50, True), (60, False), (70, True)):
        m.desired.set(VIN, CHARGE_LIMIT, limit, "policy", decision=True); changes += 1
        (m.startCarsCharging if start else m.stopCarsCharging)(VIN); changes += 1
        for _ in range(12): c.t += 60; r.pass_()
    assert len(cmds(f)) <= changes

def test_a_start_that_keeps_failing_is_bounded():                           # paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "timeout"))); onTwc(m, v); f = m.providerOrder.provider("FleetAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    for _ in range(60): m.startCarsCharging(VIN); c.t += 60; r.pass_()                                          # the heartbeat decides it every cycle for an hour
    assert len(cmds(f)) <= 4 and len(f.calls) - len(cmds(f)) <= 4

def test_a_stop_that_keeps_failing_is_bounded():                            # paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "not accepted"))); onTwc(m, v); f = m.providerOrder.provider("FleetAPI"); v.observe("chargingState", "Charging", "TeslaAPI")
    for i in range(60):
        m.stopCarsCharging(VIN); c.t += 60; r.pass_()
        if i % 5 == 0: v.observe("chargingState", "Charging", "TeslaMateVehicle")                                 # a report now and then
    assert len(cmds(f)) <= 4 and not any(x[0] == "wake" for x in f.calls)                                          # a stop never wakes

def test_an_app_start_after_our_stop_is_stopped_again_once_per_newer_reading():   # user decision
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); onTwc(m, v); b = m.providerOrder.provider("TeslaBLE"); v.observe("chargingState", "Charging", "TeslaBLE")
    m.stopCarsCharging(VIN); r.pass_(); c.t += 30; v.observe("chargingState", "Charging", "TeslaMateVehicle")
    assert r.pass_() == 0                                                    # inside RETRY: a lagging report, not resent
    c.t += 31; assert r.pass_() == 1 and cmds(b) == ["stopCharging", "stopCharging"]
    for _ in range(5): c.t += 61; m.stopCarsCharging(VIN); r.pass_()
    assert len(cmds(b)) == 2                                                 # no newer reading: our stamp holds

def test_zero_offer_clears_a_pending_start_and_sends_no_wake():             # Review Focus 3, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); s = onTwc(m, v); f = m.providerOrder.provider("FleetAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    m.startCarsCharging(VIN); m.setMaxAmpsToDivideAmongSlaves(0); drive(m, s, amps_actual=0.0)
    assert m.desired.get(VIN, CHARGING) is None
    for _ in range(5): c.t += 61; drive(m, s, amps_actual=0.0); r.pass_()
    assert f.calls == []

def test_an_offer_starts_the_car_from_the_heartbeat():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); s = onTwc(m, v); v.observe("chargingState", "Stopped", "TeslaBLE")
    m.setMaxAmpsToDivideAmongSlaves(32); drive(m, s, amps_actual=0.0); drive(m, s, amps_actual=0.0)
    assert m.desired.get(VIN, CHARGING).value is True and r.pass_() == 1 and cmds(m.providerOrder.provider("TeslaBLE")) == ["startCharging"]

def test_twc_current_alone_never_restops_a_car_with_vehicle_reads():        # Review Focus 4
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); onTwc(m, v); b = m.providerOrder.provider("TeslaBLE"); v.observe("chargingState", "Charging", "TeslaBLE")
    m.stopCarsCharging(VIN); r.pass_()
    c.t += 30; v.observe("actualCurrent", 10.0, "Gen2TWC"); assert r.pass_() == 0 and len(b.calls) == 1
    c.t += 61; v.observe("actualCurrent", 10.0, "Gen2TWC"); assert r.pass_() == 0 and len(b.calls) == 1

def test_cancel_drops_a_pending_stop_only():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Charging", "TeslaBLE")
    m.stopCarsCharging(VIN); assert m.cancelStopCarsCharging(VIN) == 1 and m.desired.get(VIN, CHARGING) is None
    assert m.cancelStopCarsCharging(VIN) == 0
    m.startCarsCharging(VIN); assert m.cancelStopCarsCharging(VIN) == 0 and m.desired.get(VIN, CHARGING).value is True   # a start is never cancelled

def test_cancel_withdraws_a_queued_stop():
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); onTwc(m, v); v.observe("chargingState", "Charging", "TeslaAPI")
    r.cancel = Mock(return_value=1); m.stopCarsCharging(VIN); assert m.cancelStopCarsCharging(VIN) == 1
    r.cancel.assert_called_once_with(VIN)

def test_fleet_only_car_with_a_pending_schedule_gets_no_start_or_wake(requests_mock):   # pre-flight H3, paid counted
    m, r, v, c, api = fleetOnTwc(requests_mock)
    v.observe("scheduledChargingPending", True, "TeslaAPI"); m.startCarsCharging(VIN)
    for _ in range(5): m.startCarsCharging(VIN); r.pass_()
    assert calls(requests_mock, "wake_up") == [] and calls(requests_mock, "charge_start") == []

# Real Fleet requests (requests_mock)

FLEET_CAR = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/" + VIN

def calls(requests_mock, part):
    return [q for q in requests_mock.request_history if q.url.split("?")[0].endswith(part)]

def fleetOnTwc(requests_mock, inline_=True, state="online", listed=True):
    # A Fleet-only car on the TWC: a real TeslaAPI with a Fleet token, the
    # car on its list, every Tesla request mocked
    m, r, v, c = setup(clock=False)
    if not inline_:
        from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
        m.reconciler = r = VehicleReconciler(m)
    api = new_api(master=m); api.storeToken(fleet_token())
    for p in api.providers(): m.providerOrder.register(p)
    if listed:
        api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"}); api.getCarApiVehicles()[0].atHome = True
    onTwc(m, v); v.observe("chargingState", "Stopped", "TeslaAPI")
    requests_mock.get(FLEET_CAR, json={"response": {"state": state}})
    requests_mock.get(FLEET_CAR + "/vehicle_data", json={"response": {"charge_state": {"charging_state": "Stopped", "charge_limit_soc": 90},
                                                                    "drive_state": {"latitude": 1.0, "longitude": 2.0}, "vehicle_state": {}}})
    requests_mock.post(re.compile(r".*/command/.*"), json={"response": {"result": True, "reason": ""}})
    requests_mock.post(re.compile(r".*/wake_up"), json={"response": {"state": "online"}})
    return m, r, v, c, api

def test_fleet_start_sends_the_limit_first(requests_mock, monkeypatch):     # Task 11 M2 closed, paid counted
    import TWCManager.Vehicle.Reconcile.Reconciler as rec
    monkeypatch.setattr(rec, "LIMIT_THEN_START_GAP", 0.0)
    m, r, v, c, api = fleetOnTwc(requests_mock); m.settings["chargeStopMode"] = "1"
    m.setPolicyLimit(50, transition=True); m.startCarsCharging(VIN)
    for _ in range(3): r.pass_(); time.sleep(0.01)
    sent = [q.url.split("/")[-1] for q in requests_mock.request_history if q.method == "POST"]
    assert sent == ["set_charge_limit", "charge_start"]                     # the limit before the start, no wake (online)
    assert requests_mock.request_history[0].url == FLEET_CAR                # the free online check comes first

def test_fleet_wake_comes_after_a_free_online_check(requests_mock):         # paid counted
    m, r, v, c, api = fleetOnTwc(requests_mock, state="asleep"); car = api.getCarApiVehicles()[0]
    requests_mock.post(re.compile(r".*/wake_up"), json={"response": {"state": "asleep"}})
    car.firstChargeNeededTime = time.time() - 600; car.lastChargeNeededTime = time.time()                            # the wake hold is over
    m.startCarsCharging(VIN); r.pass_()
    urls = [(q.method, q.url.split("?")[0]) for q in requests_mock.request_history]
    assert ("POST", FLEET_CAR + "/wake_up") in urls and urls.index(("GET", FLEET_CAR)) < urls.index(("POST", FLEET_CAR + "/wake_up"))
    assert calls(requests_mock, "charge_start") == [] and len(calls(requests_mock, "wake_up")) == 1   # still asleep: no command after the wake
    for _ in range(5): m.startCarsCharging(VIN); r.pass_()
    assert len(calls(requests_mock, "wake_up")) == 1 and calls(requests_mock, "vehicle_data") == []     # held between wakes; no paid read of a sleeping car

def test_a_fleet_only_car_is_stopped_once_the_list_loads(requests_mock):    # C1: no legacy loader left
    from tests.unit.teslatokens import FLEET_NA_URL
    m, r, v, c, api = fleetOnTwc(requests_mock, listed=False)
    requests_mock.get(FLEET_NA_URL, json={"response": [{"id": 1, "vin": VIN, "display_name": "Car"}], "count": 1})
    v.observe("chargingState", "Charging", "TeslaAPI"); m.stopCarsCharging(VIN); r.pass_()
    assert calls(requests_mock, "charge_stop") == [] and m.desired.record(VIN, CHARGING).lastOutcome is None   # not listed: no candidate yet
    assert api.updateChargeAtHome() is True; r.pass_()
    assert len(calls(requests_mock, "charge_stop")) == 1 and calls(requests_mock, "wake_up") == []

def test_a_queued_start_is_withdrawn_when_the_offer_drops(requests_mock):   # pre-flight M7, threaded, paid counted
    m, r, v, c, api = fleetOnTwc(requests_mock, inline_=False, state="asleep"); gate = threading.Event()
    block = ScriptedProvider("Blocker", 9, False, [O.Outcome(O.DONE)]); block.wake = lambda vin: (gate.wait(5), O.Outcome(O.DONE))[1]
    m.providerOrder.register(block); r.cloud.start()
    try:
        from TWCManager.Vehicle.Reconcile.Desired import Attempt
        r.submit(Attempt(VIN, None, C.WAKE, None, "Blocker", False, None, False, time.time()))                    # holds the cloud worker
        from tests.unit.test_reconciler_queues import wait_for
        assert wait_for(lambda: r.cloud._busy and not r.cloud._low)
        m.startCarsCharging(VIN); r.pass_(); assert len(r.cloud._low) == 1                                       # the Fleet start waits behind it
        m.desired.clearIf(VIN, CHARGING, True); gate.set()
        r.drain(5)
    finally:
        gate.set()
    assert calls(requests_mock, "wake_up") == [] and calls(requests_mock, "charge_start") == [] and calls(requests_mock, "vehicle_data") == []
