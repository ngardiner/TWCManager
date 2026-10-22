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
    w = Watch(m, clock=c); m.setConsumption("t", 6000)
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
