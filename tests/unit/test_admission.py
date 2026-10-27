# A plugged-in car is woken and started when its point is admitted: it would
# charge and its minimum fits. Wanted charging follows admission, and the
# wake check reads the control tick's admission. Real master, store and
# reconciler; a scripted BLE, or the real TeslaAPI with a scripted Fleet car.
import time

import pytest

from TWCManager.Vehicle.Reconcile import Belief as B
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile import Reconciler
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING
from tests.unit.gen2_helpers import drive
from tests.unit.test_command_path_parity import FleetCar, commands, install, paid
from tests.unit.test_command_path_parity import quiet, wakeUps  # noqa: F401
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


def onGen2(m, c, monkeypatch):
    # The setup's Gen2 TWC, heard from on protocol 2, at 243 V; the
    # heartbeat's clock is the test's
    monkeypatch.setattr(time, "time", c)
    m.config["config"].update(WIRING)
    (s,) = m.getSlaveTWCs()
    s.protocolVersion, s.minAmpsTWCSupports, s.voltsPhaseA = 2, 6, 243
    s.heardFrom, s.wiringMaxAmps = True, 48
    s.config, s.configConfig = m.config, m.config["config"]
    return s


def beats(m, r, c, s, seconds, amps, state=0x01, maxA=32.0):
    # A heartbeat (and with it the control pass) every 10 s, a reconcile pass
    for _ in range(seconds // 10):
        c.t += 10
        drive(m, s, amps, state=state, amps_max=maxA)
        r.pass_()


def test_last_nights_raise_wakes_and_starts_the_car():
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 60)
    assert names(p) == []  # at its limit: left asleep
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c)
    calls = names(p)
    assert calls[:2] == ["wake", "setChargeLimit"] and p.calls[1][2] == 90
    # Admitted, its rate goes before its start
    assert calls[2:] == ["wake", "setAmps", "wake", "startCharging"]


def test_last_nights_raise_on_fleet_costs_one_wake(requests_mock):
    # Fleet only: the car asleep, Complete at 70, plugged in; the policy
    # raises it to 90 with 32 A to share. One wake_up carries the limit, the
    # start and the rate
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="asleep", limit=70, charging="Complete", soc=70)
    for field, value in dict(
        pluggedIn=True, chargingState="Complete", chargeLimit=70, batteryLevel=70
    ).items():
        v.observe(field, value, "FleetAPI", at=c() - 600)
    v.observe("state", "asleep", "FleetAPI", at=c() - 600)
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 600)
    assert paid(requests_mock) == (0, 0, 0)  # at its limit: left asleep
    m.setPolicyLimit(90, transition=True)
    run(m, r, c, 1800)
    assert commands(requests_mock) == [
        "set_charge_limit",
        "set_charging_amps",
        "charge_start",
    ]
    assert wakeUps(requests_mock) == 1


def test_a_car_at_its_limit_is_neither_woken_nor_started():
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 3600, 60)
    assert names(p) == [] and m.desired.get(VIN, CHARGING) is None


def test_a_car_on_its_own_schedule_is_never_admitted():
    m, r, v, c, p = finished()
    v.observe("scheduledChargingPending", True, "TeslaBLE")
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c, 3600, 60)
    assert "wake" not in names(p) and "startCharging" not in names(p)
    assert m.desired.get(VIN, CHARGING) is None and m.desired.get(VIN, AMPS) is None
    assert B.whyQuiet(m, v).startswith("scheduled charging is pending")


def test_no_power_to_share_wakes_nothing():
    # Track Green Energy at night
    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(0)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c, 3600, 60)
    assert "wake" not in names(p) and "startCharging" not in names(p)
    assert B.whyQuiet(m, v) == "no room to start"


def test_a_car_held_at_00_waits_for_room_beside_a_drawing_twc(monkeypatch):
    # The car's TWC still holds it at 00 (6 A minimum); a second TWC draws
    # (6 A minimum). 11 A leaves no room; 12 A does
    from tests.unit.reconcile_helpers import gen2Slave

    m, r, v, c, p = finished(limit=90, state="Stopped")
    s = onGen2(m, c, monkeypatch)
    v.observe("twcVin", s.twcid, "TWC")
    other = gen2Slave(m, "8245", state=0x01, amps=6.0)
    other.heardFrom, other.minAmpsTWCSupports = True, 6
    other.config, other.configConfig = m.config, m.config["config"]
    m.setMaxAmpsToDivideAmongSlaves(11)
    s.reportedState, other.reportedAmpsMax = 0x00, 32.0
    m.controlPass(now=c())
    assert s.vehicleKey is None and s.heldKey == VIN
    assert B.whyQuiet(m, v) == "no room to start"
    assert m.desired.get(VIN, CHARGING) is None
    m.setMaxAmpsToDivideAmongSlaves(12)
    m.controlPass(now=c())
    assert B.whyQuiet(m, v) is None and round(s.pointAmps) == 6
    assert m.desired.get(VIN, CHARGING).value is True


def test_a_car_on_a_gen2_echoing_0_is_woken_and_started_for_a_raise(monkeypatch):
    m, r, v, c, p = finished(O.Outcome(O.DONE))
    s = onGen2(m, c, monkeypatch)
    v.observe("twcVin", s.twcid, "TWC")
    s.reportedState, s.reportedAmpsMax = 0x03, 0.0
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c)
    assert s.vehicleKey == VIN and s.offer.amps == 32
    calls = names(p)
    assert calls[:2] == ["wake", "setChargeLimit"] and "startCharging" in calls


def test_a_dusk_stop_does_not_block_a_midnight_start(monkeypatch):
    # Green energy stops the Gen2 car at dusk with a 0 offer; it sleeps at
    # 00. At midnight a schedule gives 32 A and raises its limit: the stale
    # stop gives way, and the car is woken and started
    m, r, v, c, p = finished(battery=60, limit=80, state="Charging")
    v.observe("state", "online", "TeslaBLE")
    s = onGen2(m, c, monkeypatch)
    s.currentVIN = VIN
    v.observe("twcVin", s.twcid, "TWC")
    m.setMaxAmpsToDivideAmongSlaves(0)
    beats(m, r, c, s, 300, 16.0)
    assert m.desired.get(VIN, CHARGING).value is False
    assert "stopCharging" in names(p)
    v.observe("chargingState", "Stopped", "TeslaBLE")
    v.observe("state", "asleep", "TeslaBLE")
    s.currentVIN = ""
    beats(m, r, c, s, 3600, 0.0, state=0x00, maxA=0.0)
    assert m.desired.get(VIN, CHARGING).value is False  # power-less: stays
    del p.calls[:]
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    beats(m, r, c, s, 600, 0.0, state=0x00, maxA=0.0)
    assert m.desired.get(VIN, CHARGING).value is True
    calls = names(p)
    assert calls[:2] == ["wake", "setChargeLimit"] and "startCharging" in calls


def test_the_gen2_gate_and_admission_never_loop(monkeypatch):
    # Twenty passes of each stable input: a car charging with room, the
    # same car with none (one stop), then stopped with none (no start), then
    # stopped with room (one start)
    m, r, v, c, p = finished(battery=60, limit=80, state="Charging")
    v.observe("state", "online", "TeslaBLE")
    s = onGen2(m, c, monkeypatch)
    s.currentVIN = VIN
    v.observe("twcVin", s.twcid, "TWC")

    def wants(power, amps, state):
        m.setMaxAmpsToDivideAmongSlaves(power)
        v.observe("chargingState", state, "TeslaBLE")
        seen = []
        for _ in range(20):
            beats(m, r, c, s, 10, amps)
            d = m.desired.get(VIN, CHARGING)
            seen.append(d and d.value)
        return [x for i, x in enumerate(seen) if i == 0 or x != seen[i - 1]]

    assert wants(32, 16.0, "Charging") == [None]
    assert wants(0, 16.0, "Charging")[-1] is False
    assert wants(0, 0.0, "Stopped") == [False]
    assert wants(32, 0.0, "Stopped") == [True]
    assert names(p).count("startCharging") == 1
    assert names(p).count("stopCharging") == 1


def test_stops_still_go_out_when_admission_raises():
    m, r, v, c, p = finished(battery=60, limit=80, state="Charging")
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 60)
    del p.calls[:]
    m.chargingPoints = lambda: 1 / 0
    try:
        m.controlPass(now=c())
    except ZeroDivisionError:
        pass
    assert m.admission == frozenset()
    m.desired.set(VIN, CHARGING, False, "distributor")
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy")
    r.pass_()
    assert names(p) == ["setChargeLimit", "stopCharging"]  # neither woke it


def test_a_withdrawn_pass_wakes_nothing():
    m, r, v, c, p = finished(limit=90, state="Stopped")
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.controlPass(now=c())
    assert B.whyQuiet(m, v) is None
    m.withdrawOffers()
    assert B.whyQuiet(m, v) == "no room to start"


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


def fleetOnlyRestart(requests_mock):
    # Just restarted: the car plugged in and asleep, and a car that does not
    # answer its wake, so its charging state stays unknown
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="asleep", wakes=False)
    v.observe("pluggedIn", True, "FleetAPI")
    m.setMaxAmpsToDivideAmongSlaves(32)
    run(m, r, c, 7200, 30)
    return m, v


def test_a_fleet_only_restart_wakes_a_car_of_unknown_state_once(requests_mock):
    m, v = fleetOnlyRestart(requests_mock)
    assert B.known(v, CHARGING) is None and wakeUps(requests_mock) == 1
    fleetOnlyRestart(requests_mock)  # a restart: one more
    assert wakeUps(requests_mock) == 2


def test_the_restart_wake_switches_off(requests_mock, monkeypatch):
    monkeypatch.setattr(Reconciler.VehicleReconciler, "wakeUnreadOnce", False)
    fleetOnlyRestart(requests_mock)
    assert wakeUps(requests_mock) == 0


def test_a_car_side_car_at_the_edge_is_stopped_only_after_a_minute_out():
    # Fleet sets its rate; the power swings across its 5 A minimum every
    # pass: no start or stop goes out. Out of room for a minute, it stops
    from tests.unit.test_limit_reconciler import fleet

    m, r, v, c = setup(fleet(*[O.Outcome(O.DONE)] * 9), battery=50)
    f = prov(m, "FleetAPI")
    for field, value in dict(chargeLimit=80, chargingState="Charging").items():
        v.observe(field, value, "FleetAPI")
    v.observe("state", "online", "FleetAPI")
    for n in range(40):
        m.setMaxAmpsToDivideAmongSlaves((6, 4)[n % 2])
        run(m, r, c, 10)
    assert "startCharging" not in names(f) and "stopCharging" not in names(f)
    m.setMaxAmpsToDivideAmongSlaves(4)
    run(m, r, c, 50)
    assert "stopCharging" not in names(f)
    run(m, r, c, 20)
    assert names(f).count("stopCharging") == 1


def test_an_anonymous_car_is_neither_started_nor_stopped_by_admission(monkeypatch):
    # Its stop would fan out to every car that might be it: here the named one
    from types import SimpleNamespace

    from TWCManager.Vehicle import Presence

    m, r, v, c, p = finished(battery=60, limit=80, state="Charging")
    monkeypatch.setattr(Presence, "candidates", lambda m, capability: [v])
    anon = m.vehicleRegistry.beginPlugSession("8244")
    anon.observe("actualCurrent", 12.0, "Gen2TWC")
    point = SimpleNamespace(
        vehicleKey=anon.key, localEndpoints=[], vehicleEndpoints=[], wantsToCharge=True
    )
    m.admission = frozenset()
    for t in (0, 61, 122):
        m._followAdmission({anon.key}, point, c() + t)
    assert m.desired.get(VIN, CHARGING) is None
    assert m.desired.get(anon.key, CHARGING) is None


def test_a_raise_set_between_ticks_finds_the_room_kept_for_it():
    # Live, the policy's raise reaches the reconciler before the next tick:
    # the admission the last tick made, with the car still at its limit,
    # already has room for it
    from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler

    m, r, v, c, p = finished()
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.controlPass(now=c())
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    VehicleReconciler.pass_(r)  # no tick in between
    assert names(p) == ["wake", "setChargeLimit"]


def test_a_raise_to_a_complete_car_of_unknown_battery_starts_it():
    # Its Complete predates our raise, so it may charge again
    m, r, v, c, p = finished(battery=None)
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    run(m, r, c)
    assert names(p)[:2] == ["wake", "setChargeLimit"]
    assert "startCharging" in names(p) and B.whyQuiet(m, v) is None


def test_a_failed_split_leaves_no_room():
    m, r, v, c, p = finished(limit=90, state="Stopped")
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.controlPass(now=c())
    assert VIN in m.admission and VIN in m._held
    points = m.chargingPoints()
    for point in points:
        point.setTargetPower = lambda watts: 1 / 0
    m.chargingPoints = lambda: points
    with pytest.raises(ZeroDivisionError):
        m.distributeEVSEPower()
    assert m.admission == frozenset()


class StaysStopped(FleetCar):
    # Accepts a start and never acts on it
    def _command(self, req, ctx):
        out = super()._command(req, ctx)
        self.charge["charging_state"] = "Stopped"
        return out


def carSide(requests_mock, cls=FleetCar, charging="Stopped"):
    # Fleet only, no TWC: the car is its own point, with a 5 A minimum
    m, r, v, c, api = install(fleet=True)
    car = cls(requests_mock, state="online", limit=80, charging=charging, soc=50)
    for field, value in dict(
        pluggedIn=True, chargingState=charging, chargeLimit=80, batteryLevel=50
    ).items():
        v.observe(field, value, "FleetAPI")
    v.observe("state", "online", "FleetAPI")
    return m, r, v, c, car


def swing(m, r, v, c, car, seconds, period, amps=(6, 4), every=60):
    # The power switches across the minimum every period s; a feed reports
    # the car's state every s
    t0 = c()
    while c() - t0 < seconds:
        m.setMaxAmpsToDivideAmongSlaves(amps[int((c() - t0) // period) % 2])
        if int(c() - t0) % every == 0:
            state = car.charge["charging_state"]
            v.observe("chargingState", state, "FleetTelemetryMQTT")
        run(m, r, c, 10)


@pytest.mark.parametrize("period", [10, 90, 150, None])
def test_a_start_without_room_waits_and_keeps_its_budget(requests_mock, period):
    # A car that accepts starts but stays Stopped, for an hour: power at the
    # edge costs no more paid starts than steady power (None) does
    m, r, v, c, car = carSide(requests_mock, StaysStopped)
    amps = (6, 4) if period else (32, 32)
    swing(m, r, v, c, car, 3600, period or 3600, amps)
    assert commands(requests_mock).count("charge_start") == 2


def test_power_across_the_minimum_every_4_minutes_for_2_hours(requests_mock):
    # Clouds: each 4 minutes in or out of room outlasts startStopDelay, so
    # every swing costs a stop and a start, and the rate a cut to the
    # minimum and a restore
    m, r, v, c, car = carSide(requests_mock)
    swing(m, r, v, c, car, 7200, 240, every=30)
    cmds = commands(requests_mock)
    counts = [cmds.count(k) for k in ("charge_stop", "charge_start")]
    assert counts + [cmds.count("set_charging_amps")] == [15, 15, 30]


@pytest.mark.parametrize("period", [10, 30])
def test_power_flapping_at_the_minimum_cuts_the_rate_once(requests_mock, period):
    # 6 A and 4 A by turns around a 5 A minimum for an hour: cut once to the
    # minimum, the car stays there until room has held, so never stops
    m, r, v, c, car = carSide(requests_mock, charging="Charging")
    swing(m, r, v, c, car, 3600, period, every=30)
    assert commands(requests_mock) == ["set_charging_amps"] * 2


def test_a_car_stopped_for_room_restarts_once_room_has_held(requests_mock):
    m, r, v, c, car = carSide(requests_mock, charging="Charging")
    swing(m, r, v, c, car, 120, 120, amps=(4, 4), every=10)
    assert commands(requests_mock)[-1:] == ["charge_stop"]
    swing(m, r, v, c, car, 60, 120, amps=(6, 6), every=10)
    assert "charge_start" not in commands(requests_mock)
    swing(m, r, v, c, car, 20, 120, amps=(6, 6), every=10)
    assert commands(requests_mock).count("charge_start") == 1


def test_a_car_side_stop_waits_start_stop_delay_with_the_draw_steady():
    # Without room for 10 minutes, the car starts itself (from the app):
    # the hold starts then, and again when its draw changes
    from tests.unit.test_limit_reconciler import fleet

    m, r, v, c = setup(fleet(*[O.Outcome(O.DONE)] * 9), battery=50)
    m.config["config"]["startStopDelay"] = 120
    f = prov(m, "FleetAPI")
    for field, value in dict(chargeLimit=80, chargingState="Stopped").items():
        v.observe(field, value, "FleetAPI")
    v.observe("state", "online", "FleetAPI")
    m.setMaxAmpsToDivideAmongSlaves(4)
    run(m, r, c, 600)
    v.observe("chargingState", "Charging", "FleetAPI")
    run(m, r, c, 100)
    v.observe("actualCurrent", 10, "FleetAPI")
    run(m, r, c, 120)
    assert "stopCharging" not in names(f)
    run(m, r, c, 20)
    assert names(f).count("stopCharging") == 1


def charging(amps=16):
    # A car-side car charging on Fleet's rate, with power for amps
    from tests.unit.test_limit_reconciler import fleet

    m, r, v, c = setup(fleet(*[O.Outcome(O.DONE)] * 9), battery=50)
    for field, value in dict(chargeLimit=80, chargingState="Charging").items():
        v.observe(field, value, "FleetAPI")
    v.observe("state", "online", "FleetAPI")
    m.setMaxAmpsToDivideAmongSlaves(amps)
    run(m, r, c, 60)
    return m, r, v, c, prov(m, "FleetAPI")


def rates(f):
    return [call[2] for call in f.calls if call[0] == "setAmps"]


def test_a_car_side_car_without_room_runs_at_its_minimum_then_stops():
    m, r, v, c, f = charging()
    assert rates(f) == [16]
    m.setMaxAmpsToDivideAmongSlaves(4)
    run(m, r, c, 60)
    assert rates(f) == [16, 5] and "stopCharging" not in names(f)
    run(m, r, c, 20)
    assert names(f).count("stopCharging") == 1


def test_under_a_fixed_policy_a_car_without_room_stops_at_once(monkeypatch):
    # No EMS noise to ride out
    from unittest.mock import Mock

    m, r, v, c, f = charging()
    lookup = m.getModuleByName
    fixed = Mock(**{"policyIsGreen.return_value": False})
    monkeypatch.setattr(
        m, "getModuleByName", lambda n: fixed if n == "Policy" else lookup(n)
    )
    m.setMaxAmpsToDivideAmongSlaves(4)
    run(m, r, c, 10)
    assert names(f).count("stopCharging") == 1


def gen3(requests_mock, monkeypatch):
    # The car on a Gen3 unit (6 A minimum) that names it, charging at 10 A;
    # returns the unit and the targets it is set
    from unittest.mock import Mock, patch

    from TWCManager.EVSEController.Gen3TWCs import Gen3TWCs
    from tests.unit.test_gen3twc_provider import vitals

    m, r, v, c, car = carSide(requests_mock, charging="Charging")
    m.config["controller.Gen3TWCs"] = {
        "enabled": True,
        "devices": [{"ip": "10.0.0.1", "fuseAmps": 48, "phases": 1}],
    }
    with patch.object(Gen3TWCs, "_start_server"):
        ctl = Gen3TWCs(m)
    ctl.setHouseWatts, targets, setUnit = Mock(), [], ctl.setUnitTarget
    ctl.setUnitTarget = lambda unit, watts: targets.append(watts) or setUnit(
        unit, watts
    )
    lookup = m.getModuleByName
    m.getModuleByName = lambda n: ctl if n == "Gen3TWCs" else lookup(n)
    (u,) = ctl.devices()
    vitals(u, True, 10.0)
    monkeypatch.setattr(u, "vitalsStale", lambda: False)
    return m, r, v, c, car, u, targets


def test_a_gen3_unit_without_room_is_held_at_its_minimum(requests_mock, monkeypatch):
    m, r, v, c, car, u, targets = gen3(requests_mock, monkeypatch)
    m.setMaxAmpsToDivideAmongSlaves(16)
    run(m, r, c, 60)
    m.setMaxAmpsToDivideAmongSlaves(5)
    run(m, r, c, 60)
    assert targets[-1] == u.minPower and "charge_stop" not in commands(requests_mock)
    run(m, r, c, 20)
    assert commands(requests_mock).count("charge_stop") == 1


@pytest.mark.parametrize("seconds", [30, 60])
@pytest.mark.parametrize("kind", ["car-side", "Gen2", "Gen3"])
def test_one_low_ems_reading_stops_nothing(requests_mock, monkeypatch, kind, seconds):
    # The EMS reads low once, for one interval, under the default hold
    if kind == "car-side":
        m, r, v, c, f = charging()
        tick = lambda n: run(m, r, c, n)  # noqa: E731
        stops = lambda: names(f).count("stopCharging")  # noqa: E731
    elif kind == "Gen2":
        m, r, v, c, p = finished(battery=60, limit=80, state="Charging")
        v.observe("state", "online", "TeslaBLE")
        s = onGen2(m, c, monkeypatch)
        s.currentVIN = VIN
        v.observe("twcVin", s.twcid, "TWC")
        tick = lambda n: beats(m, r, c, s, n, 16.0)  # noqa: E731
        stops = lambda: names(p).count("stopCharging")  # noqa: E731
    else:
        m, r, v, c, car, u, targets = gen3(requests_mock, monkeypatch)
        tick = lambda n: run(m, r, c, n)  # noqa: E731
        stops = lambda: commands(requests_mock).count("charge_stop")  # noqa: E731
    for power in (16, 0, 16):
        m.setMaxAmpsToDivideAmongSlaves(power)
        tick(seconds if power == 0 else 300)
    assert stops() == 0


def test_a_gen2_car_drawing_4_a_or_less_is_not_stopped_for_room(monkeypatch):
    # Ramping up or finishing
    m, r, v, c, p = finished(battery=60, limit=80, state="Charging")
    v.observe("state", "online", "TeslaBLE")
    s = onGen2(m, c, monkeypatch)
    s.currentVIN = VIN
    v.observe("twcVin", s.twcid, "TWC")
    m.setMaxAmpsToDivideAmongSlaves(0)
    beats(m, r, c, s, 300, 3.0)
    assert "stopCharging" not in names(p)
    beats(m, r, c, s, 300, 16.0)
    assert names(p).count("stopCharging") == 1


def test_a_cut_car_is_released_once_room_has_held_however_its_draw_wanders():
    # At its minimum, a three-phase car's reading may swing 5 A to 6 A
    m, r, v, c, f = charging()
    m.setMaxAmpsToDivideAmongSlaves(4)
    run(m, r, c, 30)
    assert rates(f) == [16, 5]
    m.setMaxAmpsToDivideAmongSlaves(16)
    for n in range(60):
        v.observe("actualCurrent", (5, 6)[n % 2], "FleetAPI")
        run(m, r, c, 10)
        if n == 5:
            assert rates(f) == [16, 5]  # 60 s of room: still held
    assert rates(f) == [16, 5, 16] and VIN not in m._cut


def test_a_car_that_unplugs_while_cut_comes_back_with_its_share():
    from tests.unit.test_limit_reconciler import leave

    m, r, v, c, f = charging()
    m.setMaxAmpsToDivideAmongSlaves(4)
    run(m, r, c, 30)
    assert rates(f) == [16, 5] and VIN in m._cut
    leave(v)
    run(m, r, c, 10)
    assert VIN not in m._cut and VIN not in m._held
    c.t += 600
    v.observe("pluggedIn", True, "Gen3TWC")
    m.setMaxAmpsToDivideAmongSlaves(16)
    run(m, r, c, 20)
    assert rates(f) == [16, 5, 16]
