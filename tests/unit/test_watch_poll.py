import time

import pytest
import requests

from TWCManager.Vehicle.Watch import DEFAULTS, Watch, fleetOnly
from tests.unit.reconcile_helpers import AWAY, HOME, attachFleet, bleAnswers, bleReads, placeAt, runPending
from tests.unit.test_command_path_parity import FLEET, FleetCar, counts, paid
from tests.unit.test_limit_reconciler import VIN, setup
from tests.unit.test_watch import apiOnly, chargingFleetCar, later, noScan

CHARGING = dict(charging_state="Charging", charger_actual_current=32, charger_voltage=240)
SAVE = {"cmd": "saveSettings"}


def upload(requests_mock, **charge):
    # The car's last snapshot as the test leaves it when asked: update the
    # returned dict to move it
    def answer(req, ctx):
        s = dict(charge); stamp = s.pop("stamp")
        return {"response": {"charge_state": dict(s, timestamp=int(stamp * 1000)), "vehicle_state": {}}}
    requests_mock.get(FLEET + VIN + "/vehicle_data_cached", json=answer)
    return charge

def start(requests_mock, at=HOME, state="online"):
    # A Fleet-only car, awake unless said, its first status check done; saved: the saves asked
    m, r, v, c, car = apiOnly(requests_mock, state=state, at=at); placeAt(m, v, at)
    w = Watch(m, clock=c); saved = []; m.queue_background_task = lambda task, delay=0: saved.append(task)
    w.pass_(); runPending(r.cloud)
    return m, r, v, c, w, saved, car

def run(m, v, c, w, r, n, secs=60):
    for _ in range(n): later(m, v, c, secs); w.pass_(); runPending(r.cloud)

def test_a_suspicion_polls_until_a_newer_snapshot_and_pays_nothing(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    snap = upload(requests_mock, stamp=T - 60, charging_state="Stopped"); w.suspect(VIN, T, "load step up", step=True)
    run(m, v, c, w, r, 4)
    assert counts(requests_mock)["cached"] == 4 and not w.ledger.isOpen(VIN) and saved == []
    snap.update(CHARGING, stamp=T + 250, charge_energy_added=0.82); run(m, v, c, w, r, 1)
    assert w.ledger.isOpen(VIN) and saved == [SAVE]
    assert w.ledger.toJSON()[VIN]["segments"][0][:2] == [T, T + 250]                   # from the step
    run(m, v, c, w, r, 1)                                                                  # the session polls on
    assert counts(requests_mock)["cached"] == 6 and paid(requests_mock) == (0, 0, 0)

def test_the_poll_gives_up_without_paying(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    upload(requests_mock, stamp=T - 60, charging_state="Stopped"); w.suspect(VIN, T, "wake")
    run(m, v, c, w, r, 15)
    assert counts(requests_mock)["cached"] == DEFAULTS["pollGiveUpSecs"] // 60 - 1          # T+60 .. T+540
    assert paid(requests_mock) == (0, 0, 0) and saved == []

def test_a_suspicion_keeps_its_earliest_time(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    upload(requests_mock, stamp=T - 60, charging_state="Stopped")
    w.suspect(VIN, T, "wake"); run(m, v, c, w, r, 5); w.suspect(VIN, c.t, "command")    # extends, not reopens
    run(m, v, c, w, r, 10)
    assert counts(requests_mock)["cached"] == 9

def test_a_session_polls_each_minute_and_ends_on_a_closing_snapshot(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    snap = upload(requests_mock, **dict(CHARGING, stamp=T + 5, charge_energy_added=0.1)); w.suspect(VIN, T, "command")
    run(m, v, c, w, r, 4)                                                                  # opened, then the same snapshot
    assert w.ledger.isOpen(VIN) and counts(requests_mock)["cached"] == 4 and saved == [SAVE]
    snap.update(stamp=T + 250, charge_energy_added=0.9); run(m, v, c, w, r, 1)
    assert len(saved) == 2 and w.ledger.isOpen(VIN)
    snap.update(stamp=T + 320, charging_state="Complete", charge_energy_added=1.0, charger_actual_current=0)
    run(m, v, c, w, r, 1); assert not w.ledger.isOpen(VIN) and len(saved) == 3
    run(m, v, c, w, r, 5)
    assert counts(requests_mock)["cached"] == 6 and paid(requests_mock) == (0, 0, 0)

def test_a_charge_resumed_on_the_same_plug_in_counts_only_its_growth(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    snap = upload(requests_mock, stamp=T + 5, charging_state="Complete", charge_energy_added=3.3); w.suspect(VIN, T, "wake")
    run(m, v, c, w, r, 1); assert saved == [SAVE]                                          # its counter, kept for a resume
    w.suspect(VIN, c.t, "load step up", step=True); S = c.t
    snap.update(CHARGING, stamp=S + 250, charge_energy_added=3.6); run(m, v, c, w, r, 5)
    [seg] = w.ledger.toJSON()[VIN]["segments"]
    assert seg[:2] == [S, S + 250] and seg[2] == pytest.approx(0.3 * 3.6e6 / 0.93 / 250)

def test_a_charge_only_a_paid_read_saw_is_counted(requests_mock):
    # Plugged in on arrival, its limit lowered at once: the free snapshot
    # after the command already says Complete
    m, r, v, c, w, saved, car = start(requests_mock); S = c.t
    w.suspect(VIN, S, "load step up", step=True)
    for f, x in dict(chargingState="Charging", actualCurrent=48, voltage=240, phases=1, chargeEnergyAdded=0.15).items():
        v.observe(f, x, "FleetAPI", at=S + 100)
    upload(requests_mock, stamp=S + 106, charging_state="Complete", charge_energy_added=0.16, charger_actual_current=0)
    run(m, v, c, w, r, 3)
    segs = w.ledger.toJSON()[VIN]["segments"]
    assert not w.ledger.isOpen(VIN) and segs[0][0] == S and S + 100 < segs[-1][1] < S + 106
    assert sum((e - s) * x for s, e, x in segs) == pytest.approx(0.16 * 3.6e6 / 0.93)
    assert paid(requests_mock) == (0, 0, 0)

def test_a_paid_read_alone_opens_a_session(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); S = c.t
    upload(requests_mock, stamp=S - 60, charging_state="Stopped"); w.suspect(VIN, S, "load step up", step=True)
    for f, x in dict(chargingState="Charging", actualCurrent=48, voltage=240, phases=1, chargeEnergyAdded=0.15).items():
        v.observe(f, x, "FleetAPI", at=S + 100)
    run(m, v, c, w, r, 3)
    assert w.ledger.isOpen(VIN) and w.ledger.toJSON()[VIN]["segments"][0][:2] == [S, S + 100]
    assert VIN not in w._suspect and paid(requests_mock) == (0, 0, 0)

def test_an_asleep_car_is_never_polled_and_its_session_ends(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    upload(requests_mock, **dict(CHARGING, stamp=T + 5, charge_energy_added=0.1)); w.suspect(VIN, T, "command")
    run(m, v, c, w, r, 1); assert w.ledger.isOpen(VIN)
    car.state = "asleep"; run(m, v, c, w, r, 4); n = counts(requests_mock)["cached"]         # the status check sees it
    assert not w.ledger.isOpen(VIN) and saved == [SAVE, SAVE]
    w.suspect(VIN, c.t, "load step up"); run(m, v, c, w, r, 10)
    assert counts(requests_mock)["cached"] == n and paid(requests_mock) == (0, 0, 0)

def test_no_routine_paid_read_while_a_fleet_only_car_charges(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)                  # a policy reads the battery
    v.observe("state", "online", "FleetAPI"); v.observe("chargingState", "Charging", "FleetAPI")
    w = Watch(m, clock=c); m.queue_background_task = lambda task, delay=0: None; w.pass_(); runPending(r.cloud)
    upload(requests_mock, **dict(CHARGING, stamp=c.t, charge_energy_added=0.1)); w.suspect(VIN, c.t, "wake")
    run(m, v, c, w, r, 30)                                                                # believed charging: polled from the first pass
    assert counts(requests_mock)["cached"] == 31 and paid(requests_mock) == (0, 0, 0)

def test_an_away_car_is_not_polled(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock, at=AWAY); T = c.t
    upload(requests_mock, **dict(CHARGING, stamp=T + 30, charge_energy_added=5.0)); w.suspect(VIN, T, "command")
    run(m, v, c, w, r, 5)
    assert counts(requests_mock)["cached"] == 0 and not w.ledger.isOpen(VIN)

def test_a_snapshot_charging_away_opens_no_session(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    upload(requests_mock, **dict(CHARGING, stamp=T + 30, charge_energy_added=5.0)); w.suspect(VIN, T, "command")
    later(m, v, c, 60); w.pass_(); placeAt(m, v, AWAY); runPending(r.cloud)              # it left before the poll ran
    assert counts(requests_mock)["cached"] == 1 and not w.ledger.isOpen(VIN) and saved == []

def test_a_paused_fleet_slot_keeps_the_car_fleet_only_and_its_session(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    upload(requests_mock, **dict(CHARGING, stamp=T + 5, charge_energy_added=0.1)); w.suspect(VIN, T, "command")
    run(m, v, c, w, r, 1); assert w.ledger.isOpen(VIN)
    slot = m.getModuleByName("TeslaAPI").fleetSlot; slot.pausedUntil = time.time() + 3600
    run(m, v, c, w, r, 3)
    assert fleetOnly(m, v) and w.ledger.isOpen(VIN) and w.chargingAmps(ledgered=False) == 0
    slot.pausedUntil = 0; car.state = "asleep"; run(m, v, c, w, r, 5)
    assert not w.ledger.isOpen(VIN)

def deafCar(requests_mock):
    # At home in a BLE install, two pings missed where no scan can run: its
    # one read for where it is done
    m, r, v, c = setup(here=False, ble=True); attachFleet(m, v, FleetCar(requests_mock, state="online", at=HOME))
    placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE"); m.queue_background_task = lambda task, delay=0: None
    w = Watch(m, clock=c); bleReads(m); bleAnswers(m, answer=None); noScan(m)
    for _ in range(3): w.pass_(); runPending(r.ble); runPending(r.cloud); later(m, v, c, 240)
    assert paid(requests_mock)[0] == 1 and fleetOnly(m, v, deaf=True)
    return m, r, v, c, w

def test_a_deaf_car_pays_nothing_for_a_detection_ask(requests_mock):
    m, r, v, c, w = deafCar(requests_mock); later(m, v, c, 300)                         # past the cloud read dedupe
    w.readNow(VIN, "load step up"); w.pass_(); runPending(r.ble); runPending(r.cloud); later(m, v, c, 300)
    w.readNow(VIN, "plug-in", exempt=False); w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert paid(requests_mock)[0] == 1

def test_a_deaf_car_heard_again_ends_its_session(requests_mock):
    m, r, v, c, w = deafCar(requests_mock)
    upload(requests_mock, **dict(CHARGING, stamp=c.t + 5, charge_energy_added=0.1)); w.suspect(VIN, c.t, "command")
    w.pass_(); runPending(r.ble); runPending(r.cloud); assert w.ledger.isOpen(VIN)
    bleAnswers(m, asleep=False)
    for _ in range(3): later(m, v, c, 240); w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert not w.ledger.isOpen(VIN)

def test_fetch_errors_keep_the_cadence_and_never_pause_the_slot(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    ok = {"response": {"charge_state": dict(CHARGING, timestamp=int((T + 200) * 1000), charge_energy_added=0.5), "vehicle_state": {}}}
    requests_mock.get(FLEET + VIN + "/vehicle_data_cached", [
        {"status_code": 429, "json": {}}, {"status_code": 404, "json": {}}, {"exc": requests.exceptions.ConnectTimeout},
        {"json": ok}])
    w.suspect(VIN, T, "load step up"); run(m, v, c, w, r, 3)
    api = m.getModuleByName("TeslaAPI"); assert not w.ledger.isOpen(VIN)
    assert api.fleetSlot.usable("read") and api.fleetSlot.pausedUntil == 0
    run(m, v, c, w, r, 1)                                                                  # the fourth minute answers
    assert w.ledger.isOpen(VIN) and paid(requests_mock) == (0, 0, 0)

def test_a_snapshot_stamped_just_before_the_suspicion_is_not_taken(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    snap = upload(requests_mock, **dict(CHARGING, stamp=T - 2, charge_energy_added=0.1)); w.suspect(VIN, T, "command")
    run(m, v, c, w, r, 3)
    assert counts(requests_mock)["cached"] == 3 and not w.ledger.isOpen(VIN)                # still polling
    snap.update(stamp=T + 150); run(m, v, c, w, r, 1); assert w.ledger.isOpen(VIN)

def test_a_ble_car_in_range_is_unchanged(requests_mock):
    m, r, v, c = setup(here=False, ble=True); attachFleet(m, v, FleetCar(requests_mock, state="online", at=HOME))
    placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE"); v.observe("chargingState", "Charging", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m, charging="Charging", amps=24); bleAnswers(m, asleep=False)
    w.pass_(); runPending(r.ble); w.suspect(VIN, c.t, "load step up")
    for _ in range(4): c.t += 300; w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert reads.count == 5 and counts(requests_mock)["cached"] == 0 and (VIN, "poll") not in w._last

def test_an_accepted_command_suspects_the_car_from_its_send_time(requests_mock):
    from TWCManager.Vehicle.Reconcile import Run
    from TWCManager.Vehicle.Reconcile.Desired import CHARGING as ASPECT, Attempt
    from TWCManager.Vehicle.Providers.Capability import STOP_CHARGING
    m, r, v, c, w, saved, car = start(requests_mock); m.watch = w; T = c.t
    def slow(req, ctx): c.t += 20; return {"response": {"result": True, "reason": ""}}   # answered later than sent
    requests_mock.post(FLEET + VIN + "/command/charge_stop", json=slow)
    Run.runAttempt(r, Attempt(VIN, ASPECT, STOP_CHARGING, False, "FleetAPI", False, None, False, T))
    assert w._suspect[VIN][0] == T and c.t == T + 20

def test_a_refused_command_suspects_nothing(requests_mock):
    from TWCManager.Vehicle.Reconcile import Run
    from TWCManager.Vehicle.Reconcile.Desired import CHARGING as ASPECT, Attempt
    from TWCManager.Vehicle.Providers.Capability import STOP_CHARGING
    m, r, v, c, w, saved, car = start(requests_mock); m.watch = w; car.script = [(403, False, "")]
    out = Run.runAttempt(r, Attempt(VIN, ASPECT, STOP_CHARGING, False, "FleetAPI", False, None, False, c.t))
    assert out.kind == "refused" and VIN not in w._suspect

def test_an_outcome_never_sent_suspects_nothing_and_still_saves_the_limit(requests_mock):
    from TWCManager.Vehicle.Reconcile import Outcome as O
    from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT, Attempt
    from TWCManager.Vehicle.Providers.Capability import SET_CHARGE_LIMIT
    m, r, v, c, w, saved, car = start(requests_mock); m.watch = w; kept = []
    r._save = lambda vehicle, *save: kept.append(save)
    r.desired.record(VIN, CHARGE_LIMIT).pendingSave = (80, 90)
    attempt = Attempt(VIN, CHARGE_LIMIT, SET_CHARGE_LIMIT, 90, "FleetAPI", False, 0, False, c.t)
    r.afterWrite(v, attempt, O.Outcome(O.SATISFIED, "read"))                                # sentAt None: nothing went
    assert VIN not in w._suspect and kept == [(80, 90)]

def stale(m, w, c, v, consumption):
    # The car believed drawing 32 A beside a house drawing consumption
    m.watch = w; m.subtractChargerLoad = True; m.getRealPowerFactor = lambda amps: 1
    v.observe("chargingState", "Charging", "HomeAssistant"); v.observe("actualCurrent", 32, "HomeAssistant")
    for watts in consumption: m.setConsumption("t", watts); w.loadSample(); c.t += 30

def test_believed_load_above_consumption_twice_suspects_the_car(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    stale(m, w, c, v, [9000, 2000, 2000]); assert w._suspect[VIN][0] == T + 30           # the first of the two

def test_one_sample_above_consumption_is_a_ramp_not_staleness(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock)
    stale(m, w, c, v, [9000, 2000, 9000, 2000, 9000]); assert VIN not in w._suspect

def test_a_fleet_only_car_believed_charging_with_no_session_is_suspected(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock); placeAt(m, v, HOME); T = c.t                # as after a restart
    upload(requests_mock, stamp=T - 600, charging_state="Charging"); v.observe("chargingState", "Charging", "FleetAPI", at=T - 600)
    w = Watch(m, clock=c); m.queue_background_task = lambda task, delay=0: None; w.pass_()
    assert w._suspect[VIN][0] == T
    run(m, v, c, w, r, 2); assert w._suspect[VIN][0] == T                                # one open, not reopened

def test_a_wake_and_a_plug_in_suspect_a_fleet_only_car_and_pay_nothing(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock, state="asleep"); w._settings["homeAsleepSecs"] = 300
    car.state = "online"; run(m, v, c, w, r, 6)                                           # the check sees it awake
    assert VIN in w._suspect and paid(requests_mock)[0] == 1                              # the wake's read is for where it is
    w._suspect.clear(); later(m, v, c, 60); T = c.t; v.mark("plugIn", T); w.pass_()
    assert w._suspect[VIN][0] == T and paid(requests_mock)[0] == 1

def test_a_belief_no_snapshot_settles_is_polled_one_window_until_new_evidence(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); T = c.t
    upload(requests_mock, stamp=T - 600, charging_state="Complete")
    v.observe("chargingState", "Charging", "HomeAssistant")                              # a feed no snapshot bears out
    run(m, v, c, w, r, 60)
    assert counts(requests_mock)["cached"] == 9 and paid(requests_mock) == (0, 0, 0)      # T+60 .. T+540, then given up
    v.observe("actualCurrent", 0, "FleetAPI"); run(m, v, c, w, r, 20)                    # something newer is known
    assert counts(requests_mock)["cached"] == 19                                          # one more window, polled at once

def test_staleness_suspects_once_per_evidence(requests_mock):
    m, r, v, c, w, saved, car = start(requests_mock); upload(requests_mock, stamp=c.t - 600, charging_state="Complete")
    def minutes(n):
        for _ in range(2 * n): m.setConsumption("t", 2000); w.loadSample(); later(m, v, c, 30); w.pass_(); runPending(r.cloud)
    stale(m, w, c, v, [2000]); minutes(40); n = counts(requests_mock)["cached"]
    assert n == 9 and paid(requests_mock) == (0, 0, 0)                                   # one give-up window
    v.noteWrite("chargeCurrentRequest", 16, "FleetAPI"); minutes(40)                     # a command: new evidence
    assert counts(requests_mock)["cached"] == 2 * n
