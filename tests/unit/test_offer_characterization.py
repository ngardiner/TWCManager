# What a Gen2 TWC is offered on the wire, and which car amps its charging
# point writes, on the default (distributor) path. tickThenBeat runs a
# slave's reply, the control pass and the heartbeat in that order.
import time
from unittest.mock import Mock

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
    m.vehicleRegistry.observeTwcVin("8244", VIN)  # the VIN read confirms it
    assert rate(m) == (32, "lift", True)
    m.desired.clearIf(VIN, AMPS, 32)  # settled
    beat(m, s)
    assert rate(m) is None  # once per session
    endSession(s)
    beat(m, s, state=0x04, actual=0.0)
    assert s.isCharging == 0 and s.vehicleKey == VIN
    assert rate(m) == (32, "lift", True)  # the next session lifts again


@pytest.mark.parametrize("member", [False, True])
def test_a_mode_2_to_1_switch_mid_session_lifts_again(member):
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


def test_a_twc_point_never_clears_a_vehicle_endpoints_rate():
    # An unnamed session at 00 (not blind) reports no car, as a named one
    # does, so VIN is rated by its own VehicleEndpoint. Once VIN is named by
    # the other TWC that endpoint (on the other TWC's point) holds a
    # "distributor" rate, which the unnamed point must leave in place
    m = master(mode=2, feed=40)
    m.config["controller.Gen2TWCs"] = {"ranges": {"8245": {"maxAmps": 16}}}
    car(m, VIN, "Charging")
    s = slave(m, state=0x00, maxA=32, actual=0.0)
    other = slave(m, b"\x82\x45", state=0x00, maxA=0, actual=0.0)
    m.vehicleRegistry.beginPlugSession("8244")
    assert s.vehicleKey is None and other.vehicleKey is None
    beat(m, s)
    assert rate(m) == (20, "distributor", False)  # VIN's own endpoint
    other.currentVIN, other.reportedState, other.reportedAmpsMax = VIN, 0x01, 16
    other.reportedAmpsActual = 10.0
    for _ in range(2):
        beat(m, s)
        assert rate(m) == (16, "distributor", False)


# The start/stop gates of a protocol-2 TWC with a car key. Gate calls are
# recorded, not made


GATES = (
    "stopCarsCharging",
    "cancelStopCarsCharging",
    "startCarsCharging",
)


def gated(m, twcid=b"\x82\x44", vin=VIN, protocol=2, **kw):
    s = slave(m, twcid, vin=vin, **kw)
    s.protocolVersion = protocol
    for name in GATES:
        setattr(m, name, Mock())
    return s


def calls(m):
    # The gate calls since the last look, as (name, key)
    out = []
    for name in GATES:
        out += [(name, c.args[0]) for c in getattr(m, name).call_args_list]
        getattr(m, name).reset_mock()
    return out


def age(m, seconds):
    # The car has been without room, its draw steady, seconds longer
    m._held = {key: (*h[:-1], h[-1] - seconds) for key, h in m._held.items()}


def test_a_car_drawing_over_4a_offered_zero_is_stopped_after_60s():
    m = master(feed=3)
    s = gated(m, actual=10.0)
    beat(m, s)
    assert s.lastAmpsOffered == 0
    assert calls(m) == [("cancelStopCarsCharging", VIN)]  # the offer just changed
    age(m, 61)
    beat(m, s)
    assert calls(m) == [("stopCarsCharging", VIN)]


def test_the_stop_waits_start_stop_delay_after_a_significant_amps_change():
    m = master(feed=3)
    s = gated(m, actual=10.0)
    m.config["config"]["startStopDelay"] = 120
    beat(m, s)
    age(m, 90)
    calls(m)
    beat(m, s, actual=12.0)  # a significant change: the hold restarts
    age(m, 90)
    beat(m, s)
    assert calls(m) == [("cancelStopCarsCharging", VIN)] * 2
    age(m, 40)
    beat(m, s)
    assert calls(m) == [("stopCarsCharging", VIN)]


def test_offered_at_least_the_minimum_and_not_drawing_starts_the_car():
    m = master(feed=40)
    s = gated(m, state=0x03, actual=0.0)
    beat(m, s)
    assert s.lastAmpsOffered >= s.minAmpsToOffer()
    assert ("startCarsCharging", VIN) in calls(m)


def test_an_error_state_is_not_started():
    m = master(feed=40)
    s = gated(m, state=0x02, actual=0.0)
    beat(m, s)
    assert s.lastAmpsOffered >= s.minAmpsToOffer()
    names = [name for name, _ in calls(m)]
    assert "startCarsCharging" not in names


def test_offered_below_the_minimum_and_not_drawing_decides_nothing():
    # A start already wanted waits for room in the reconciler
    m = master(feed=3)
    s = gated(m, state=0x03, actual=0.0)
    beat(m, s)
    assert s.lastAmpsOffered == 0
    assert calls(m) == []


def test_protocol_1_and_keyless_twcs_decide_nothing():
    # Protocol 1: offered 0 while drawing (a stop), then not drawing, then
    # offered the minimum (a start) at 2
    m = master(feed=3)
    s = gated(m, protocol=1, actual=10.0)
    age(m, 61)
    beat(m, s)
    beat(m, s, actual=0.0)
    assert calls(m) == []
    budget(m, 40)
    beat(m, s)
    assert "startCarsCharging" not in [name for name, _ in calls(m)]
    # Protocol 2 with no car key: nothing plugged in, so no session
    m = master(feed=3)
    s = gated(m, vin=None, state=0x00, actual=0.0)
    beat(m, s)
    budget(m, 40)
    beat(m, s)
    assert s.vehicleKey is None and calls(m) == []


def test_no_gate_runs_during_the_mode_2_silence():
    m = master(feed=3)
    s = gated(m, actual=10.0)
    beat(m, s)
    assert calls(m) == [("cancelStopCarsCharging", VIN)]  # the offer just changed
    m.settings["respondToSlaves"] = 0
    m.settings["respondToSlavesExpiry"] = time.time() + 60
    age(m, 61)
    tickThenBeat(m, s, 0x01, 40, 10.0)
    assert calls(m) == []  # silent TWCs are not started or stopped
    m.settings["respondToSlavesExpiry"] = time.time() - 1
    beat(m, s)
    assert calls(m) == [("stopCarsCharging", VIN)]
