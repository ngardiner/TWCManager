# A plugged-in car is woken and started when it would charge and there is
# room for it: its point's offer, which serves only cars already drawing,
# never decides it. Real master, store and reconciler; a scripted BLE.
from types import SimpleNamespace

from TWCManager.Vehicle.Reconcile import Belief as B
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT, CHARGING
from tests.unit.test_limit_reconciler import VIN, ble, names, prov, setup

WIRING = dict(wiringMaxAmpsAllTWCs=48, wiringMaxAmpsPerTWC=48, minAmpsPerTWC=6)


def finished(*outcomes, battery=70, limit=70, state="Complete"):
    # The car plugged in at home, asleep, done with its last session
    m, r, v, c = setup(ble(*(outcomes or [O.Outcome(O.DONE)])), battery=battery)
    v.observe("chargeLimit", limit, "TeslaBLE")
    v.observe("chargingState", state, "TeslaBLE")
    v.observe("state", "asleep", "TeslaBLE")
    return m, r, v, c, prov(m, "TeslaBLE")


def run(m, r, c, seconds=600, step=10):
    # The control tick and the reconciler, as live
    for _ in range(seconds // step):
        m.controlPass(now=c())
        r.pass_()
        c.t += step


def test_last_nights_raise_wakes_and_starts_the_car():
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 60)
    assert names(p) == []  # at its limit: left asleep
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c)
    calls = names(p)
    assert calls[:2] == ["wake", "setChargeLimit"] and p.calls[1][2] == 90
    assert calls[2:] == ["wake", "startCharging"]
    v.observe("chargingState", "Charging", "TeslaBLE")  # it draws: rated
    run(m, r, c, 60)
    assert "setAmps" in names(p)[4:]


def test_a_car_at_its_limit_is_neither_woken_nor_started():
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 3600, 60)
    assert names(p) == [] and m.desired.get(VIN, CHARGING) is None


def test_a_pending_scheduled_charge_is_neither_woken_nor_started():
    m, r, v, c, p = finished()
    v.observe("scheduledChargingPending", True, "TeslaBLE")
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c, 3600, 60)
    assert "wake" not in names(p) and "startCharging" not in names(p)
    assert m.desired.get(VIN, CHARGING) is None
    assert B.whyQuiet(m, v).startswith("scheduled charging is pending")


def test_a_car_wanted_stopped_is_neither_woken_nor_started():
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGING, False, "distributor")
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c, 3600, 60)
    assert "wake" not in names(p) and "startCharging" not in names(p)
    assert m.desired.get(VIN, CHARGING).value is False


def test_no_power_to_share_wakes_nothing():
    # Track Green Energy at night
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(0)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c, 3600, 60)
    assert "wake" not in names(p) and "startCharging" not in names(p)
    assert B.whyQuiet(m, v) == "no room to start"


def point(key, amps, drawing):
    return SimpleNamespace(
        vehicleKey=key,
        localEndpoints=[],
        minPower=amps * 240,
        wantsToCharge=drawing,
        isReadOnly=False,
    )


def test_a_car_is_admitted_only_into_room_left_by_the_drawing_cars():
    m, r, v, c, p = finished(limit=90)
    m.convertAmpsToWatts = lambda amps: amps * 240
    m.chargingPoints = lambda: [point("other", 6, True), point(VIN, 5, False)]
    m.setMaxAmpsToDivideAmongSlaves(9)
    assert B.wouldCharge(m, v) and not B.admissible(m, v)
    m.desired.set(VIN, CHARGE_LIMIT, 95, "policy")
    r.pass_()
    assert names(p) == []  # asleep, and no wake
    m.setMaxAmpsToDivideAmongSlaves(16)
    assert B.admissible(m, v)
    c.t += 60
    m.desired.set(VIN, CHARGE_LIMIT, 100, "policy")
    r.pass_()
    assert names(p) == ["wake", "setChargeLimit"]


def test_a_car_on_no_point_of_its_own_needs_an_idle_points_minimum():
    # Behind a TWC that cannot name it
    m, r, v, c, p = finished(limit=90)
    m.convertAmpsToWatts = lambda amps: amps * 240
    m.chargingPoints = lambda: [point("anon:8244", 6, False)]
    m.setMaxAmpsToDivideAmongSlaves(3)
    m.desired.set(VIN, CHARGE_LIMIT, 95, "policy")
    r.pass_()
    assert B.whyQuiet(m, v) == "no room to start" and names(p) == []
    m.setMaxAmpsToDivideAmongSlaves(6)
    assert B.admissible(m, v)


def test_a_car_on_a_gen2_echoing_0_is_woken_and_started_for_a_raise():
    m, r, v, c, p = finished(O.Outcome(O.DONE))
    m.config["config"].update(WIRING)
    (s,) = m.getSlaveTWCs()
    s.protocolVersion, s.minAmpsTWCSupports, s.voltsPhaseA = 2, 6, 243
    s.heardFrom = True
    s.config, s.configConfig, s.wiringMaxAmps = m.config, m.config["config"], 48
    v.observe("twcVin", s.twcid, "TWC")
    s.reportedState, s.reportedAmpsMax = 0x03, 0.0
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c)
    assert s.vehicleKey == VIN and s.offer.amps == 0
    calls = names(p)
    assert calls[:2] == ["wake", "setChargeLimit"] and "startCharging" in calls


def test_a_woken_car_that_refuses_the_start_is_not_woken_again():
    m, r, v, c, p = finished(battery=70, limit=80, state="Stopped")
    p.startCharging = lambda vin: p._next("startCharging", vin, None) and O.Outcome(
        O.REFUSED, "complete"
    )
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 600, 10)
    assert names(p).count("startCharging") == 1
    wakes = names(p).count("wake")
    run(m, r, c, 3600, 10)
    assert names(p).count("startCharging") == 1 and names(p).count("wake") == wakes
