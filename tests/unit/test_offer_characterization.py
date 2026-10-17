# tests/unit/test_offer_characterization.py: what a Gen2 TWC is offered on
# the wire, and which car amps its charging point writes, on the default
# (distributor) path. These pass at 1029809b (offers in the heartbeat, spike
# settings 0) and after Phase 4 Task 4 (offers on the control tick): the
# rules moved, the values did not. tickThenBeat runs each version's order.
import time

import pytest

from TWCManager.Vehicle.Reconcile.Desired import AMPS
from tests.unit.gen2_helpers import tickThenBeat
from tests.unit.test_charging_points import VIN, VIN2, budget, car, real


def master(mode=1, feed=40, **config):
    m = real(mode)
    m.config["config"].update(config)
    m.settings["spikeAmpsProactively"] = m.settings["spikeAmpsReactively"] = 0
    budget(m, feed)
    return m


def slave(m, twcid=b"\x82\x44", vin=None, state=0x01, maxA=40, actual=10.0):
    # Protocol 1: no start/stop gates in the heartbeat. It reports 40 A max,
    # which no offer here equals, so every offer goes out on the wire
    s = m.newSlave(twcid, 80)
    s.protocolVersion, s.heardFrom = 1, True
    s.reportedState, s.reportedAmpsMax, s.reportedAmpsActual = state, maxA, actual
    if vin:
        # A registry car: a VIN the registry lacks starts an anonymous session
        m.vehicleRegistry.getOrCreate(vin, "OwnerAPI")
        s.currentVIN = vin
    return s


def beat(m, s, state=None, maxA=None, actual=None):
    # One round for s; returns the (command, amps) it was sent
    iface = tickThenBeat(
        m,
        s,
        s.reportedState if state is None else state,
        s.reportedAmpsMax if maxA is None else maxA,
        s.reportedAmpsActual if actual is None else actual,
    )
    hb = iface.send.call_args.args[0][6:15]
    return hb[0], ((hb[1] << 8) + hb[2]) / 100


def rate(m, key=VIN):
    d = m.desired.get(key, AMPS)
    return None if d is None else (d.value, d.by, d.lift)


def test_idle_twc_nobody_charging_sends_zero():
    m = master()
    s = slave(m, state=0x00, maxA=0, actual=0.0)
    assert [beat(m, s) for _ in range(3)] == [(0x05, 0)] * 3


def test_idle_twc_probe_when_budget_per_car_exceeds_min():
    m = master(feed=40, minAmpsPerTWC=12)
    slave(m, b"\x82\x45", vin=VIN, actual=20.0)  # charging
    idle = slave(m, state=0x00, maxA=0, actual=0.0)
    assert beat(m, idle) == (0x05, 6)  # minAmpsTWCSupports, so it sees a car


def test_idle_twc_probe_zero_when_it_does_not():
    m = master(feed=10, minAmpsPerTWC=12)
    slave(m, b"\x82\x45", vin=VIN, actual=20.0)
    idle = slave(m, state=0x00, maxA=0, actual=0.0)
    assert beat(m, idle) == (0x05, 0)


def test_charging_twc_gets_whole_amps_of_its_share():
    m = master(feed=25)
    a = slave(m, vin=VIN)
    slave(m, b"\x82\x45", vin=VIN2)
    assert beat(m, a) == (0x05, 12)  # 12.5 A share
    assert a.lastAmpsOffered == 12


def test_a_decrease_within_5s_is_held_then_applied():
    m = master(feed=20)
    s = slave(m, vin=VIN)
    assert beat(m, s) == (0x05, 20)
    budget(m, 12)
    assert beat(m, s) == (0x05, 20)  # changed under 5 s ago
    s.timeLastAmpsOfferedChanged -= 10
    assert beat(m, s) == (0x05, 12)


def test_all_twc_wiring_cap_uses_others_actual_draw_and_floors_at_twc_minimum():
    m = master(feed=40, wiringMaxAmpsAllTWCs=40)
    s = slave(m, vin=VIN, actual=5.0)
    other = slave(m, b"\x82\x45", vin=VIN2, actual=30.0)
    assert beat(m, s) == (0x05, 10)  # a 20 A share; 40 - 30 drawn elsewhere
    other.reportedAmpsActual = 38.0
    assert beat(m, s) == (0x05, 6)  # 2 A left: never below the TWC's minimum


def test_per_twc_wiring_cap():
    m = master(feed=40, wiringMaxAmpsPerTWC=16)
    m.config["controller.Gen2TWCs"] = {"ranges": {"8244": {"maxAmps": 40}}}
    s = slave(m, vin=VIN, maxA=20)  # at 1029809b an unchanged offer was never capped
    assert s.wiringMaxAmps == 16
    assert beat(m, s) == (0x05, 16)


def test_mode_2_offers_the_twc_max_once_the_car_rate_is_confirmed():
    m = master(mode=2, feed=20)
    v = car(m, VIN, "Charging")
    s = slave(m, vin=VIN)
    assert beat(m, s) == (0x05, 20)  # the car's rate is not confirmed yet
    assert rate(m) == (20, "distributor", False)
    v.observe("chargeCurrentRequest", 20, "TeslaMateVehicle")
    assert beat(m, s) == (0x05, 32)  # confirmed: the TWC offers its maximum


def test_mode_3_below_six_layers_the_car_and_holds_the_twc_floor():
    m = master(mode=3, feed=5, minAmpsPerTWC=5)
    car(m, VIN, "Charging")
    s = slave(m, vin=VIN, actual=5.0)
    s.minAmpsTWCSupports = 5
    assert beat(m, s) == (0x05, 6)
    assert rate(m) == (5, "distributor", False)


def endSession(s):
    # The car drew nothing for over 5 s: the next reply ends the session
    s.isCharging, s.chargingDroppedBelowThresholdTime = 1, time.time() - 10


def test_mode_1_lift_goes_once_per_session_for_a_twc_only_point():
    m = master(mode=1, feed=20)
    s = slave(m, vin=VIN)  # no VehicleEndpoint for VIN: a TWC-only point
    beat(m, s)
    assert rate(m) == (32, "lift", True)
    m.desired.clearIf(VIN, AMPS, 32)  # settled
    beat(m, s)
    assert rate(m) is None  # once per session
    endSession(s)
    beat(m, s, state=0x04, actual=0.0)
    assert s.isCharging == 0 and s.vehicleKey == VIN
    assert rate(m) == (32, "lift", True)  # the next session lifts again


@pytest.mark.parametrize("member", [False, True])
def test_a_mode_2_to_1_switch_mid_session_lifts_again(member):  # F6
    m = master(mode=1, feed=20)
    if member:
        car(m, VIN, "Charging")
    s = slave(m, vin=VIN)
    beat(m, s)
    assert rate(m) == (32, "lift", True)
    m.desired.clearIf(VIN, AMPS, 32)
    beat(m, s)
    assert rate(m) is None
    m.settings["chargeRateControl"] = 2
    beat(m, s)
    assert rate(m) == (20, "distributor" if member else "heartbeat", False)
    m.settings["chargeRateControl"] = 1
    beat(m, s)
    assert rate(m) == (32, "lift", True)


def test_a_twc_point_never_clears_a_vehicle_endpoints_rate():  # F13
    # An anonymous TWC at 00 (not blind) rates its fan-out in mode 2. Once
    # VIN is named by the other TWC it leaves that fan-out, and its own
    # VehicleEndpoint (on the other TWC's point) holds a "distributor" rate,
    # which the anonymous point must leave in place
    m = master(mode=2, feed=40)
    m.config["controller.Gen2TWCs"] = {"ranges": {"8245": {"maxAmps": 16}}}
    car(m, VIN, "Charging")
    s = slave(m, state=0x00, maxA=32, actual=0.0)
    other = slave(m, b"\x82\x45", state=0x00, maxA=0, actual=0.0)
    m.vehicleRegistry.beginPlugSession("8244")
    assert s.vehicleKey == "anon:8244" and other.vehicleKey is None
    beat(m, s)
    assert rate(m)[0] == 20  # the fan-out reaches VIN
    other.currentVIN, other.reportedState, other.reportedAmpsMax = VIN, 0x01, 16
    other.reportedAmpsActual = 10.0
    for _ in range(2):
        beat(m, s)
        assert rate(m) == (16, "distributor", False)
