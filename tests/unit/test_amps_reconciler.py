# tests/unit/test_amps_reconciler.py: amps are stateless desired state with
# layered actuators inside a charging point; the lift is a one-shot desire
# (slim Task 12b)
import time

import pytest

from TWCManager.EVSEInstance.ChargingPoint import groupEndpoints
from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from TWCManager.Vehicle.Reconcile.Reconciler import MIN_SEND_GAP, RETRY
from tests.unit.gen2_helpers import drive as heartbeat
from tests.unit.test_limit_reconciler import VIN, ble, cmds, fleet, setup

def point(m, v, mode, car=True):
    m.settings["chargeRateControl"] = str(mode); s = m.getSlaveTWCs()[0]; s.currentVIN = VIN; v.observe("twcVin", "8244", "TWC")
    (pt,) = groupEndpoints(m, [s] + ([VehicleEndpoint(v, m.providerOrder, m)] if car else [])); return pt, s

def prov(m, name): return m.providerOrder.provider(name)

def test_carriers_follow_charge_rate_control():
    m, r, v, c = setup(); pt, s = point(m, v, 1)
    for mode, carriers in ((1, (True, False)), (2, (False, True)), (3, (True, True))):
        m.settings["chargeRateControl"] = str(mode); assert pt.carriers() == carriers

def test_mode_1_twc_carries_and_the_car_is_lifted_once_per_session():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); b = prov(m, "TeslaBLE"); v.observe("chargeCurrentRequest", 16, "TeslaBLE"); pt, s = point(m, v, 1)
    pt.setTargetPower(2400); d = m.desired.get(VIN, AMPS)
    assert s._evseTargetAmps == 10 and s.pointOwnsAmps and s.vehicleRateRaised and d.lift and d.value == 32
    r.pass_(); assert cmds(b) == ["setAmps"] and b.calls[-1] == ("setAmps", VIN, 32)
    r.pass_(); assert m.desired.get(VIN, AMPS) is None                                    # one-shot: cleared once it settled
    c.t += 61; pt.setTargetPower(4800); r.pass_(); assert len(cmds(b)) == 1 and m.desired.get(VIN, AMPS) is None   # lifted already this session
    s._resetVehicleRateRaised(); v.observe("chargeCurrentRequest", 16, "TeslaBLE")      # the next session; the car is capped again
    pt.setTargetPower(4800); r.pass_(); assert cmds(b) == ["setAmps", "setAmps"]

def test_amps_use_the_cars_request_not_the_pilot():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); b = prov(m, "TeslaBLE"); pt, s = point(m, v, 2); pt.setTargetPower(10 * 240); r.pass_()
    assert b.calls[-1] == ("setAmps", VIN, 10)
    c.t += 61; v.observe("availableCurrent", 32, "TeslaBLE")                               # the TWC's own offer, read back from the car
    assert r.pass_() == 0 and len(cmds(b)) == 1
    v.observe("chargeCurrentRequest", 8, "TeslaBLE")                                       # the car's own request disagrees: only a resend, never a bound
    assert r.pass_() == 1 and b.calls[-1] == ("setAmps", VIN, 10) and m.desired.get(VIN, AMPS).value == 10

def test_mode_3_layers_below_the_floor_and_holds_the_twc_at_its_floor():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); b = prov(m, "TeslaBLE"); v.observe("chargeCurrentRequest", 32, "TeslaBLE"); pt, s = point(m, v, 3)
    pt.setTargetPower(4 * 240); d = m.desired.get(VIN, AMPS)
    assert s._evseTargetAmps == s.minAmps == 6 and d.value == 4 and not d.lift and s.pointOwnsAmps
    r.pass_(); assert cmds(b) == ["setAmps"] and b.calls[-1] == ("setAmps", VIN, 4)
    pt.setTargetPower(10 * 240); assert s._evseTargetAmps == 10 and m.desired.get(VIN, AMPS).lift   # back in range: the car (at 4) would cap, so lift
    c.t += MIN_SEND_GAP[AMPS]; r.pass_(); assert b.calls[-1] == ("setAmps", VIN, 32)
    r.pass_(); assert m.desired.get(VIN, AMPS) is None
    pt.setTargetPower(12 * 240); assert m.desired.get(VIN, AMPS) is None                  # the car no longer caps: nothing to send

def test_mode_3_floor_is_the_heartbeats_handoff_not_a_lower_twc_minimum():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); pt, s = point(m, v, 3); s.minAmpsTWCSupports = 5
    m.config["config"]["minAmpsPerTWC"] = 5; s.configConfig = m.config["config"]; assert s.minAmps == 5
    pt.setTargetPower(5.5 * 240)                                                           # below 6 A: the car carries, the TWC holds 6, never 5
    assert s._evseTargetAmps == 6 and m.desired.get(VIN, AMPS).value == 5

def test_mode_2_car_carries_and_the_twc_offers_its_max():
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); pt, s = point(m, v, 2); pt.setTargetPower(10 * 240)
    assert s._evseTargetAmps == s.maxOfferAmps and m.desired.get(VIN, AMPS).value == 10
    r.pass_(); assert f.calls == [("setAmps", VIN, 10)]                                    # TWC not an amps path under mode 2: Fleet, no wake, no read first

def test_zero_offer_withdraws_the_cars_rate_but_not_a_lift():
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE))); pt, s = point(m, v, 2); pt.setTargetPower(10 * 240)
    pt.setTargetPower(0); assert m.desired.get(VIN, AMPS) is None and s._evseTargetAmps == 0
    m.settings["chargeRateControl"] = "1"; pt.setTargetPower(2400); pt.setTargetPower(0); assert m.desired.get(VIN, AMPS).lift

def test_twc_only_point_keeps_the_heartbeat_path_under_mode_2():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); pt, s = point(m, v, 2, car=False); pt.setTargetPower(10 * 240)
    assert s._evseTargetAmps == 10 and not s.pointOwnsAmps and m.desired.get(VIN, AMPS) is None   # the heartbeat's mode-2 branch sets the car's amps
    s._queueChargeRate(10); assert m.desired.get(VIN, AMPS).value == 10 and m.desired.get(VIN, AMPS).by == "heartbeat"

def test_heartbeat_does_not_write_amps_for_a_point_with_a_vehicle_member():
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); pt, s = point(m, v, 3); pt.setTargetPower(4 * 240)
    heartbeat(m, s, amps_actual=4.0); assert m.desired.get(VIN, AMPS).value == 4 and m.desired.get(VIN, AMPS).by == "distributor"   # no "heartbeat" write, no lift
    s.pointOwnsAmps = False; heartbeat(m, s, amps_actual=4.0); assert m.desired.get(VIN, AMPS).by == "lift"             # the TWC branch's raise, now unguarded

def test_fleet_never_sets_a_rate_the_twc_covers_but_carries_a_lift():          # paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); pt, s = point(m, v, 3)
    pt.setTargetPower(10 * 240); r.pass_(); assert f.calls == []                          # 10 A: the TWC covers it; the request is unknown, so no lift under mode 3
    v.observe("chargeCurrentRequest", 6, "TeslaAPI"); pt.setTargetPower(10 * 240); r.pass_(); assert f.calls == [("setAmps", VIN, 32)]
    c.t += MIN_SEND_GAP[AMPS]; pt.setTargetPower(4 * 240); r.pass_(); assert f.calls[-1] == ("setAmps", VIN, 4) and len(f.calls) == 2

def test_legacy_heartbeat_path_sets_desired_and_the_lift_once():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); s = m.getSlaveTWCs()[0]; s.currentVIN = VIN
    s._queueChargeRate(7.9); d = m.desired.get(VIN, AMPS); assert (d.value, d.by, d.lift) == (7, "heartbeat", False)
    s._requestVehicleRateRaise(); d = m.desired.get(VIN, AMPS); assert d.lift and d.value == 32 and s.vehicleRateRaised
    r.pass_(); r.pass_(); assert m.desired.get(VIN, AMPS) is None
    s._resetVehicleRateRaised(); assert not s.vehicleRateRaised

def test_a_session_end_resets_the_once_per_session_lift():
    m, r, v, c = setup(); s = m.getSlaveTWCs()[0]; s.vehicleRateRaised = True
    s.isCharging = 1; s.reportedAmpsActual = 0; s.chargingDroppedBelowThresholdTime = time.time() - 10
    m.num_cars_charging_now(); assert s.isCharging == 0 and not s.vehicleRateRaised

def test_user_changed_amps_are_resent_on_the_next_reading_under_mode_2():   # as main: overridden
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); b = prov(m, "TeslaBLE"); pt, s = point(m, v, 2); pt.setTargetPower(20 * 240); r.pass_()
    c.t += 61; v.observe("chargeCurrentRequest", 16, "TeslaAPI"); assert r.pass_() == 1 and b.calls[-1] == ("setAmps", VIN, 20)

@pytest.mark.parametrize("mode", [1, 3])
def test_user_changed_amps_after_a_lift_are_left_alone(mode):              # main's once-per-session raise
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); b = prov(m, "TeslaBLE"); v.observe("chargeCurrentRequest", 6, "TeslaBLE")
    pt, s = point(m, v, mode); pt.setTargetPower(10 * 240); r.pass_(); r.pass_()
    assert cmds(b) == ["setAmps"] and b.calls[-1] == ("setAmps", VIN, 32)
    c.t += 61; v.observe("chargeCurrentRequest", 16, "TeslaAPI"); pt.setTargetPower(20 * 240)   # above the user's 16; TeslaMate never publishes it (pre-flight L11)
    assert r.pass_() == 0 and cmds(b) == ["setAmps"]

def test_failed_paid_rate_retries_once_then_waits_for_a_new_value():        # paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); e = VehicleEndpoint(v, m.providerOrder, m)
    e.setTargetPower(10 * 240); r.pass_(); c.t += 30; r.pass_(); assert len(f.calls) == 1
    c.t += RETRY[AMPS]; r.pass_(); assert len(f.calls) == 2
    c.t += 3600; r.pass_(); e.setTargetPower(10 * 240); r.pass_(); assert len(f.calls) == 2
    e.setTargetPower(11 * 240); r.pass_(); assert len(f.calls) == 3 and f.calls[-1] == ("setAmps", VIN, 11)
    assert not any(x[0] == "wake" for x in f.calls)

def test_unchanged_rate_is_never_resent():                                  # replaces the 600 s refresh; paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); e = VehicleEndpoint(v, m.providerOrder, m)
    for _ in range(120): e.setTargetPower(10 * 240); r.pass_(); c.t += 61
    assert f.calls == [("setAmps", VIN, 10)]

def test_fluctuating_offer_sends_at_most_one_rate_per_15s():               # pre-flight I6, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); e = VehicleEndpoint(v, m.providerOrder, m)
    for i in range(3600): e.setTargetPower((10 + i % 2) * 240); r.pass_(); c.t += 1
    assert 200 <= len(f.calls) <= 240 and all(x[0] == "setAmps" for x in f.calls)       # one hour: at most main's 240 set_charging_amps, no wake

VIN2, VIN3, VIN4 = "5YJ3E1EA1KF000002", "5YJ3E1EA1KF000003", "5YJ3E1EA1KF000004"

def anonTwc(*outcomes):
    # A VIN-less Gen2 in mode 2 and four at-home Fleet cars, listed VIN
    # first; VIN3 is on a TWC that names it
    from types import SimpleNamespace
    m, r, v, c = setup(fleet(*(outcomes or (O.Outcome(O.DONE),)))); m.settings["chargeRateControl"] = "2"
    for vin in (VIN, VIN2, VIN3, VIN4):
        m.vehicleRegistry.getOrCreate(vin, "t").attach("TeslaAPI", SimpleNamespace(VIN=vin, atHome=True, lat=1.0))
    s, other = m.getSlaveTWCs()[0], m.newSlave(b"\x82\x45", 80); other.currentVIN = VIN3
    anon = m.vehicleRegistry.beginPlugSession("8244"); assert s.vehicleKey == anon.key
    return m, r, v, c, s

def test_an_anonymous_twc_rates_only_plugged_in_cars_else_vehicles_0():    # I2, paid counted
    m, r, v, c, s = anonTwc(); f = prov(m, "FleetAPI")
    m.vehicleRegistry.get(VIN2).observe("chargingState", "Charging", "TeslaAPI"); m.vehicleRegistry.get(VIN3).observe("chargingState", "Charging", "TeslaAPI")
    m.vehicleRegistry.get(VIN4).observe("chargingState", "Disconnected", "TeslaAPI")
    for i in range(3600): s._queueChargeRate(10 + i % 2); r.pass_(); c.t += 1
    assert {x[1] for x in f.calls} == {VIN2} and len(f.calls) <= 240 and all(x[0] == "setAmps" for x in f.calls)   # never an unplugged car, nor a named one; no wake
    m, r, v, c, s = anonTwc(); f = prov(m, "FleetAPI")                     # none known plugged in: main's vehicles[0]
    s._queueChargeRate(10); r.pass_(); assert f.calls == [("setAmps", VIN, 10)]
    m, r, v, c, s = anonTwc(); f = prov(m, "FleetAPI")                     # HA says "Stopped" for an unplugged car (rereview N1)
    for vin in (VIN, VIN2, VIN4):
        m.vehicleRegistry.get(vin).observe("chargingState", "Disconnected", "TeslaAPI")
        m.vehicleRegistry.get(vin).observe("chargingState", "Stopped", "HomeAssistant", at=c() + 1)
    for i in range(3600): s._queueChargeRate(10 + i % 2); r.pass_(); c.t += 1
    assert {x[1] for x in f.calls} == {VIN} and len(f.calls) <= 240                 # the vehicles[0] fallback only, no fan-out
    m, r, v, c, s = anonTwc()
    m.vehicleRegistry.get(VIN2).observe("chargingState", "Unknown", "TeslaBLE"); m.vehicleRegistry.get(VIN4).observe("chargingState", "None", "TeslaMateVehicle")
    m.vehicleRegistry.get(VIN).observe("chargingState", "Charging", "HomeAssistant")
    assert [x.key for x in s._anonCars()] == [VIN]                            # BLE "Unknown" and TeslaMate "None" are not plugged in; HA "Charging" is

def test_an_anonymous_mode_1_lift_goes_to_the_plugged_in_car_only():    # I2, paid counted
    m, r, v, c, s = anonTwc(); f = prov(m, "FleetAPI"); m.settings["chargeRateControl"] = "1"
    m.vehicleRegistry.get(VIN2).observe("chargingState", "Stopped", "TeslaAPI")              # plugged in, paused
    s._requestVehicleRateRaise()
    for _ in range(5): r.pass_(); c.t += 61
    assert f.calls == [("setAmps", VIN2, 32)] and s.vehicleRateRaised

def test_a_car_that_leaves_the_anonymous_set_loses_the_heartbeat_rate():  # I2
    m, r, v, c, s = anonTwc(O.Outcome(O.UNREACHABLE)); v.observe("chargingState", "Charging", "TeslaAPI")
    s._queueChargeRate(10); assert m.desired.get(VIN, AMPS).value == 10
    c.t += 1; v.observe("chargingState", "Disconnected", "TeslaAPI"); m.vehicleRegistry.get(VIN2).observe("chargingState", "Charging", "TeslaAPI")
    s._queueChargeRate(10); assert m.desired.get(VIN, AMPS) is None and m.desired.get(VIN2, AMPS).value == 10

def test_heartbeat_withdraws_its_rate_when_the_offer_drops_below_the_minimum():   # M2
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE))); s = m.getSlaveTWCs()[0]; s.currentVIN = VIN; m.settings["chargeRateControl"] = "2"
    s._queueChargeRate(10); assert m.desired.get(VIN, AMPS).value == 10
    m.setMaxAmpsToDivideAmongSlaves(0); heartbeat(m, s, amps_actual=0.0); assert m.desired.get(VIN, AMPS) is None
    m.desired.set(VIN, AMPS, 32, "lift", lift=True); heartbeat(m, s, amps_actual=0.0); assert m.desired.get(VIN, AMPS).lift   # a lift survives

# Fix round 1 (12b review I1, M4)

def test_mode_3_lift_goes_once_per_range_entry_even_when_the_car_clamps_it():   # I1, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); pt, s = point(m, v, 3)
    for _ in range(240):                                                     # an hour of readings every 15 s: the car clamps at 24
        v.observe("chargeCurrentRequest", 24, "TeslaAPI"); pt.setTargetPower(30 * 240); r.pass_(); c.t += 15
    assert f.calls == [("setAmps", VIN, 32)]
    pt.setTargetPower(5 * 240); r.pass_(); assert f.calls[-1] == ("setAmps", VIN, 5) and not s.vehicleRateRaised   # below the floor: the car carries
    c.t += 15; pt.setTargetPower(30 * 240); r.pass_()
    assert f.calls[-1] == ("setAmps", VIN, 32) and len(f.calls) == 3          # back in range: exactly one more lift

def test_a_floor_rate_is_withdrawn_when_the_twc_carries_again():         # M4
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE))); v.observe("chargeCurrentRequest", 32, "TeslaBLE"); pt, s = point(m, v, 3)
    pt.setTargetPower(4 * 240); r.pass_(); assert m.desired.get(VIN, AMPS).value == 4    # undelivered
    pt.setTargetPower(10 * 240); assert m.desired.get(VIN, AMPS) is None                 # the TWC carries; the car is not left chasing 4 A

# 12b deleted-test PARTIALs: HA after a failed Fleet rate, per car

@pytest.mark.parametrize("fails, fleetSends", [(O.Outcome(O.ERROR, "500"), 2), (O.Outcome(O.REFUSED, "403"), 1)])
def test_home_assistant_after_a_failed_fleet_rate_spends_from_the_same_paid_budget(fails, fleetSends):   # paid counted
    from tests.unit.test_limit_reconciler import ha
    m, r, v, c = setup(fleet(fails), ha(O.Outcome(O.DONE))); f, h = prov(m, "FleetAPI"), prov(m, "HomeAssistant")
    e = VehicleEndpoint(v, m.providerOrder, m)
    for _ in range(3600): e.setTargetPower(10 * 240); r.pass_(); c.t += 1
    assert len(f.calls) == fleetSends and h.calls == []                      # one budget for the desire: never Fleet and HA both
    e.setTargetPower(11 * 240); r.pass_()
    if fails.kind == O.REFUSED: assert h.calls == [("setAmps", VIN, 11)] and len(f.calls) == 1   # Fleet excluded: HA carries the next value
    else: assert f.calls[-1] == ("setAmps", VIN, 11) and h.calls == []        # a plain failure: Fleet again, never HA

def test_the_paid_budget_is_per_car():                                      # paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v2 = m.vehicleRegistry.getOrCreate(VIN2, "t")
    e, e2 = VehicleEndpoint(v, m.providerOrder, m), VehicleEndpoint(v2, m.providerOrder, m)
    e.setTargetPower(10 * 240); r.pass_(); c.t += RETRY[AMPS]; r.pass_(); c.t += RETRY[AMPS]; r.pass_()
    assert [x[1] for x in f.calls] == [VIN, VIN]                              # VIN's budget is spent
    e2.setTargetPower(10 * 240); r.pass_(); assert f.calls[-1] == ("setAmps", VIN2, 10)   # VIN2's own send, in the same pass

@pytest.mark.parametrize("fails, after", [(O.Outcome(O.UNREACHABLE, "408"), 4), (O.Outcome(O.ERROR, "500"), 2)])
def test_failed_paid_rate_waits_for_a_newer_reading_once_per_desire(fails, after):   # review M6, paid counted
    # A send that never reached the car is re-armed once by a newer live
    # reading (one send and one blind retry more: 4 per desire at most); a
    # send the car answered is not
    m, r, v, c = setup(fleet(fails)); f = prov(m, "FleetAPI"); e = VehicleEndpoint(v, m.providerOrder, m)
    e.setTargetPower(10 * 240); r.pass_(); c.t += RETRY[AMPS]; r.pass_(); c.t += 3600; r.pass_(); assert len(f.calls) == 2
    for _ in range(3):
        c.t += RETRY[AMPS]; v.observe("chargeCurrentRequest", 9, "TeslaAPI"); r.pass_(); c.t += RETRY[AMPS]; r.pass_()
    assert len(f.calls) == after and not any(x[0] == "wake" for x in f.calls)
