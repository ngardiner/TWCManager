import pytest

from TWCManager.Vehicle.Watch import HOME_CHARGING, LoadStep, Watch, situation
from tests.unit.reconcile_helpers import AWAY, HOME, bleAnswers, bleReads, placeAt, runPending
from tests.unit.test_limit_reconciler import VIN, setup
from tests.unit.test_presence import VIN2, VIN3


def test_a_step_held_for_two_samples_is_reported_once():
    s = LoadStep(1200.0)
    assert [s.sample(w) for w in ([500, 500, 2000, 2000, 2000, 700, 700])] == [None, None, None, "up", None, None, "down"]

def test_a_dryer_with_no_awake_watched_car_triggers_no_read(caplog):
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m); m.setConsumption("t", 500); w.loadSample()
    m.setConsumption("t", 2500); w.loadSample(); c.t += 30; w.loadSample(); w.pass_(); runPending(r.ble)
    assert reads.count == 0 and " asked" not in caplog.text                                 # no ask held for its wake

def test_a_step_up_reads_the_awake_home_car_and_a_step_down_reads_the_charging_one():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); runPending(r.ble)                # the wake event read
    reads = bleReads(m, charging="Charging", amps=20)
    m.setConsumption("t", 500); w.loadSample(); m.setConsumption("t", 5300); w.loadSample(); c.t += 30; w.loadSample()
    w.pass_(); runPending(r.ble); assert reads.count == 1 and situation(m, v) == HOME_CHARGING
    reads.charging = "Complete"
    m.setConsumption("t", 500); w.loadSample(); c.t += 30; w.loadSample(); w.pass_(); runPending(r.ble)
    assert reads.count == 2 and v.value("chargingState") == "Complete"

def test_known_draw_is_subtracted_only_when_configured():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("chargingState", "Charging", "TeslaBLE"); v.observe("actualCurrent", 20, "TeslaBLE")
    w = m.watch = Watch(m, clock=c); m.setConsumption("t", 6000)
    m.subtractChargerLoad = False; assert w.unexplainedWatts() == 6000
    m.subtractChargerLoad = True;  assert w.unexplainedWatts() == pytest.approx(6000 - 20 * 240)

def step(m, w, c, before, after):
    # A step held for the next sample
    m.setConsumption("t", before); w.loadSample(); m.setConsumption("t", after); w.loadSample(); c.t += 30; w.loadSample()

def test_a_car_not_home_is_asked_only_once_it_could_be_back(caplog):
    m, r, v, c = setup(here=False, ble=True); v.observe("state", "online", "TeslaBLE")         # presumed: no fix
    home = m.vehicleRegistry.getOrCreate(VIN2, "t"); placeAt(m, home, HOME); home.observe("state", "online", "TeslaBLE")
    away = m.vehicleRegistry.getOrCreate(VIN3, "t"); placeAt(m, away, AWAY); away.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); runPending(r.ble); bleAnswers(m, answer=None)           # no ping makes it home
    step(m, w, c, 500, 5300)                                                                 # inside the return time
    assert VIN2 + " asked" in caplog.text and VIN + " asked" not in caplog.text
    c.t += 300; step(m, w, c, 5300, 500); step(m, w, c, 500, 5300)
    assert VIN + " asked" in caplog.text
    c.t += 4 * 3600; step(m, w, c, 5300, 500); step(m, w, c, 500, 5300)                      # past its return time too
    assert VIN3 + " asked" not in caplog.text                                                 # never: not home

def test_with_no_meter_the_known_draw_alone_makes_no_step(caplog):
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("chargingState", "Charging", "TeslaBLE"); m.subtractChargerLoad = True
    w = Watch(m, clock=c); v.observe("actualCurrent", 0, "TeslaBLE"); w.loadSample()
    v.observe("actualCurrent", 20, "TeslaBLE"); w.loadSample(); c.t += 30; w.loadSample()
    assert " asked" not in caplog.text

def test_a_cycling_load_reads_a_car_once_a_courtesy_window():
    # A compressor stepping up every few minutes must not keep the car awake
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); runPending(r.ble); reads = bleReads(m)
    step(m, w, c, 500, 2500); w.pass_(); runPending(r.ble); assert reads.count == 1
    step(m, w, c, 2500, 500); step(m, w, c, 500, 2500); w.pass_(); runPending(r.ble); assert reads.count == 1
    c.t += 600; step(m, w, c, 2500, 500); step(m, w, c, 500, 2500); w.pass_(); runPending(r.ble)
    assert reads.count == 1
    c.t += 600; step(m, w, c, 2500, 500); step(m, w, c, 500, 2500); w.pass_(); runPending(r.ble)
    assert reads.count == 2


def test_the_charger_load_counts_gen3_units_and_watched_cars_charging_at_home():
    # A car's own draw left in the house's consumption shrinks its green offer
    from types import SimpleNamespace
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("chargingState", "Charging", "TeslaBLE"); v.observe("actualCurrent", 20, "TeslaBLE")
    gen3 = SimpleNamespace(allEVSEs=[SimpleNamespace(currentAmps=16.0)])
    lookup = m.getModuleByName
    m.getModuleByName = lambda name: gen3 if name == "Gen3TWCs" else lookup(name)
    m.getRealPowerFactor = lambda amps: 1
    assert m.getChargerLoad() == pytest.approx(16 * 240)                 # no watch: Gen3 alone
    m.watch = Watch(m, clock=c)
    assert m.getChargerLoad() == pytest.approx((16 + 20) * 240)
    m.subtractChargerLoad = True; m.setConsumption("t", 9000)
    assert m.watch.unexplainedWatts() == pytest.approx(9000 - 36 * 240)


def test_the_charger_load_counts_a_feed_covered_car_once_and_a_twc_named_car_not_at_all():
    # A feed covers the car, so the watch leaves it alone; its draw is still
    # the charger's. A TWC naming it counts the draw on its own
    from tests.unit.reconcile_helpers import gen2Slave, liveFeed
    m, r, v, c = setup(here=False); placeAt(m, v, HOME); liveFeed(m, "TeslaMateVehicle")
    for field, value in (("state", "online"), ("chargingState", "Charging"), ("actualCurrent", 20)):
        v.observe(field, value, "TeslaMateVehicle")
    m.getRealPowerFactor = lambda amps: 1
    w = m.watch = Watch(m, clock=c)
    assert w.chargingAmps() == 20
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244", state=0x03)
    assert w.chargingAmps() == 0


def test_the_watch_wants_the_meter_read_while_its_step_can_run():
    # Without a green policy nothing else reads the meter, and the
    # consumption step would never see a car start on another charger
    m, r, v, c = setup(here=False, ble=True)
    w = Watch(m, clock=c)
    m.getModulesByType = lambda kind: [{"name": "Meter"}] if kind == "EMS" else []
    assert w.wantsEMS()
    w._settings["emsTrigger"] = False
    assert not w.wantsEMS()
    w._settings["emsTrigger"] = True
    m.vehicleRegistry.forget(VIN)  # no watched car: nothing for the step to find
    assert not w.wantsEMS()
    m.getModulesByType = lambda kind: []
    assert not w.wantsEMS()


def homeCar(c, state="Stopped", amps=0):
    # A watched car at home whose last reading was taken now; writes after it are newer
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("chargingState", state, "TeslaBLE"); v.observe("actualCurrent", amps, "TeslaBLE")
    m.getRealPowerFactor = lambda amps: 1
    return m, v, c, Watch(m, clock=c)

def test_an_accepted_start_counts_at_the_commanded_amps_until_a_reading():
    m, v, c, w = homeCar(None); c.t += 5
    v.noteWrite("chargingState", "Charging", "FleetAPI"); v.noteWrite("chargeCurrentRequest", 32, "FleetAPI")
    assert w.chargingAmps() == 32
    c.t += 5; v.observe("chargingState", "Charging", "TeslaBLE"); v.observe("actualCurrent", 30, "TeslaBLE")
    assert w.chargingAmps() == 30                                      # a reading after the command is the truth

def test_a_start_with_no_rate_written_counts_at_the_cars_maximum():
    m, v, c, w = homeCar(None); v.observe("chargeCurrentRequestMax", 48, "TeslaBLE"); c.t += 5
    v.noteWrite("chargingState", "Charging", "FleetAPI"); assert w.chargingAmps() == 48
    c.t += 5; v.noteWrite("chargingState", "Stopped", "FleetAPI"); assert w.chargingAmps() == 0

def test_a_rate_written_to_a_stopped_car_counts_nothing():
    m, v, c, w = homeCar(None); c.t += 5
    v.noteWrite("chargeCurrentRequest", 32, "FleetAPI"); assert w.chargingAmps() == 0

def test_a_lowered_rate_keeps_the_old_draw_until_a_newer_reading():
    m, v, c, w = homeCar(None, "Charging", 48); c.t += 5
    v.noteWrite("chargeCurrentRequest", 16, "FleetAPI"); assert w.chargingAmps() == 48
    c.t += 5; v.observe("actualCurrent", 16, "TeslaBLE"); assert w.chargingAmps() == 16

def test_a_stop_keeps_the_old_draw_until_a_newer_reading():
    m, v, c, w = homeCar(None, "Charging", 32); c.t += 5
    v.noteWrite("chargingState", "Stopped", "FleetAPI"); assert w.chargingAmps() == 32
    c.t += 5; v.observe("chargingState", "Stopped", "TeslaBLE"); v.observe("actualCurrent", 0, "TeslaBLE")
    assert w.chargingAmps() == 0

def test_a_raised_rate_counts_at_once():
    m, v, c, w = homeCar(None, "Charging", 16); c.t += 5
    v.noteWrite("chargeCurrentRequest", 32, "FleetAPI"); assert w.chargingAmps() == 32

def test_a_car_away_counts_nothing_even_with_a_start_written():
    m, v, c, w = homeCar(None); c.t += 5; placeAt(m, v, AWAY); c.t += 5
    v.noteWrite("chargingState", "Charging", "FleetAPI"); v.noteWrite("chargeCurrentRequest", 32, "FleetAPI")
    assert w.chargingAmps() == 0

def test_without_the_ledger_a_fleet_only_car_is_left_out_and_a_ble_car_kept():
    import TWCManager.Vehicle.Watch as W
    m, v, c, w = homeCar(None, "Charging", 20)
    assert w.chargingAmps(ledgered=False) == 20
    orig, W.fleetOnly = W.fleetOnly, lambda master, car, deaf=False: True
    try: assert w.chargingAmps(ledgered=False) == 0 and w.chargingAmps() == 20
    finally: W.fleetOnly = orig


def fleetStep(requests_mock, state="online"):
    from tests.unit.test_watch_poll import start
    m, r, v, c, w, saved, car = start(requests_mock, state=state); m.setConsumption("t", 500); w.loadSample()
    return m, r, v, c, w, car

def test_an_up_step_suspects_an_awake_fleet_only_car_from_the_steps_first_sample(requests_mock):
    m, r, v, c, w, car = fleetStep(requests_mock); c.t += 30; T = c.t
    m.setConsumption("t", 5300); w.loadSample(); c.t += 30; w.loadSample()
    assert w._suspect[VIN] == (T, T)                                                       # since, and the step's own time

def test_an_up_step_asks_an_asleep_car_online_and_suspects_it_only_if_awake(requests_mock):
    from tests.unit.test_command_path_parity import counts
    from tests.unit.test_watch import later
    m, r, v, c, w, car = fleetStep(requests_mock, state="asleep"); later(m, v, c, 300); n = counts(requests_mock)["free"]
    step(m, w, c, 500, 5300); w.pass_(); runPending(r.cloud)                              # at the next pass, off its cadence
    assert counts(requests_mock)["free"] == n + 1 and VIN not in w._suspect               # still asleep
    car.state = "online"; later(m, v, c, 60); step(m, w, c, 5300, 500); T = c.t; step(m, w, c, 500, 5300)
    w.pass_(); runPending(r.cloud); assert counts(requests_mock)["free"] == n + 2 and w._suspect[VIN] == (T, T)
    w.pass_(); runPending(r.cloud); assert counts(requests_mock)["free"] == n + 2            # asked once

def test_a_cycling_load_does_not_throttle_a_fleet_only_suspicion(requests_mock):
    m, r, v, c, w, car = fleetStep(requests_mock)
    step(m, w, c, 500, 2500); assert VIN in w._suspect
    w._suspect.clear(); c.t += 60; step(m, w, c, 2500, 500); assert VIN not in w._suspect   # not charging: a fall says nothing
    T = c.t; step(m, w, c, 500, 2500); assert w._suspect[VIN][0] == T

def test_a_down_step_suspects_a_charging_car(requests_mock):
    m, r, v, c, w, car = fleetStep(requests_mock); m.setConsumption("t", 8000); w.loadSample()
    v.observe("chargingState", "Charging", "FleetAPI"); v.observe("actualCurrent", 32, "FleetAPI")
    T = c.t; step(m, w, c, 8000, 500); assert w._suspect[VIN][0] == T


def test_a_watch_made_after_the_settings_load_restores_the_saved_ledger():
    from TWCManager.Vehicle.ChargeLedger import Snapshot
    m, r, v, c = setup(here=False, ble=True)
    first = Watch(m, clock=c); first.ledger.observe(VIN, Snapshot(c.t, "Charging", 1.0, 9000.0), startHint=c.t - 60)
    m.settings["chargeLedger"] = first.ledger.toJSON()
    assert Watch(m, clock=c).ledger.toJSON() == first.ledger.toJSON() and first.ledger.toJSON()
