import pytest

from TWCManager.Vehicle.Presence import NOT_HOME, presence
from TWCManager.Vehicle.Watch import HOME_CHARGING, LoadStep, Watch, situation
from tests.unit.reconcile_helpers import AWAY, HOME, bleAnswers, bleReads, placeAt, runPending
from tests.unit.test_limit_reconciler import VIN, setup
from tests.unit.test_presence import VIN2, VIN3


def test_a_step_held_for_two_samples_is_reported_once():
    s = LoadStep(1200.0)
    assert [s.sample(w) for w in ([500, 500, 2000, 2000, 2000, 700, 700])] == [None, None, None, "up", None, None, "down"]
    assert s.size == 1300

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
    assert m.getChargerLoad() == pytest.approx(36 * 240)


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


def metered(requests_mock):
    # The meter sees the EVSE; the watch's belief is the charger load
    m, r, v, c, w, car = fleetStep(requests_mock)
    m.watch = w; m.subtractChargerLoad = True; m.getRealPowerFactor = lambda amps: 1
    return m, r, v, c, w, car

def feed(m, w, c, *watts):
    for x in watts: c.t += 30; m.setConsumption("t", x); w.loadSample()

def charging(v, amps):
    v.observe("chargingState", "Charging" if amps else "Complete", "FleetAPI"); v.observe("actualCurrent", amps, "FleetAPI")

def stepped(w):
    # The time of the load step a suspicion carries, if any
    return w._suspect.get(VIN, (None, None))[1]

def command(v):
    v.noteWrite("chargingState", "Charging", "FleetAPI"); v.noteWrite("chargeCurrentRequest", 32, "FleetAPI")

def test_a_belief_clearing_after_the_meter_fell_is_no_rise(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); charging(v, 32)
    feed(m, w, c, 8200, 8200, 500, 500); assert stepped(w)                          # the stop's own fall
    w._suspect.clear(); charging(v, 0); feed(m, w, c, 500, 500, 500)
    assert stepped(w) is None

def test_a_belief_arriving_after_the_meter_rose_is_no_fall(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); feed(m, w, c, 500, 500)
    T = c.t + 30; feed(m, w, c, 8200, 8200); assert stepped(w) == T                 # an unexplained start acts
    w._suspect.clear(); charging(v, 32); feed(m, w, c, 8200, 8200, 8200)
    assert stepped(w) is None

def test_a_start_the_belief_leads_makes_no_step(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); feed(m, w, c, 500, 500)
    command(v); feed(m, w, c, 500, 8200, 8200); assert stepped(w) is None

def test_a_belief_change_older_than_the_window_explains_nothing(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); feed(m, w, c, 500, 500)
    command(v); feed(m, w, c, 500, 500, 500, 500, 500); feed(m, w, c, 8200, 8200); assert stepped(w)

def test_a_belief_falling_never_explains_a_rise(requests_mock):
    m, r, v, c, w, car = metered(requests_mock)
    other = m.vehicleRegistry.getOrCreate(VIN2, "t"); placeAt(m, other, HOME)
    for f, x in (("state", "online"), ("chargingState", "Charging"), ("actualCurrent", 32)): other.observe(f, x, "TeslaBLE")
    feed(m, w, c, 8200, 8200); w._suspect.clear()
    other.observe("actualCurrent", 0, "TeslaBLE"); other.observe("chargingState", "Complete", "TeslaBLE")
    feed(m, w, c, 15900, 15900); assert stepped(w)                                  # VIN plugged in as VIN2 stopped

def test_without_subtracting_charger_load_a_leading_belief_explains_nothing(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); m.subtractChargerLoad = False; feed(m, w, c, 500, 500)
    command(v); feed(m, w, c, 500, 8200, 8200); assert stepped(w)

def test_a_fall_too_small_to_be_the_charging_car_is_ignored(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); charging(v, 32)
    feed(m, w, c, 8200, 8200, 6700, 6700); assert stepped(w) is None                # 1500 W < 3840 W

def test_a_car_sized_fall_while_charging_acts(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); charging(v, 32)
    feed(m, w, c, 8200, 8200, 4000, 4000); assert stepped(w)                        # 4200 W >= 3840 W

def test_a_small_fall_reads_no_ble_car(caplog):
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("chargingState", "Charging", "TeslaBLE"); v.observe("actualCurrent", 32, "TeslaBLE")
    w = m.watch = Watch(m, clock=c); m.getRealPowerFactor = lambda amps: 1
    step(m, w, c, 8200, 6700); assert " asked" not in caplog.text

def test_a_belief_from_before_the_clock_went_back_explains_nothing(requests_mock):
    m, r, v, c, w, car = metered(requests_mock); feed(m, w, c, 500, 500)
    command(v); c.t -= 600
    feed(m, w, c, 500, 500, 500, 500, 500); feed(m, w, c, 8200, 8200); assert stepped(w)


def plugIn(v, c, amps, source="TeslaBLE"):
    # Unplugged, then a minute on plugged in, read at amps
    v.observe("chargingState", "Disconnected", source); c.t += 60
    v.observe("chargingState", "Charging", source); v.observe("chargeCurrentRequest", amps, source)

def test_a_reading_while_plugged_in_but_idle_is_no_sample():
    # Plugged in and waiting, the car reports what the charger offers idle;
    # only once it charges does the reading show the rate it starts at
    m, v, c, w = homeCar(None); v.observe("chargingState", "Disconnected", "TeslaBLE"); c.t += 60
    v.observe("chargingState", "Stopped", "TeslaBLE"); v.observe("chargeCurrentRequest", 6, "TeslaBLE")
    w.pass_(); assert v.homeAmps() == {"amps": [], "since": None, "held": None}
    c.t += 600; v.observe("chargingState", "Charging", "TeslaBLE"); v.observe("chargeCurrentRequest", 48, "TeslaBLE")
    w.pass_(); assert v.homeAmps()["amps"] == [48] and v.homeAmps()["held"] == 48

def test_the_first_home_plug_in_reading_is_the_expected_rate():
    m, v, c, w = homeCar(None); plugIn(v, c, 48); w.pass_()
    assert v.homeAmps()["amps"] == [48] and w.expectedWatts() == pytest.approx(48 * 240)
    c.t += 60; v.observe("chargeCurrentRequest", 32, "TeslaBLE"); w.pass_()
    assert v.homeAmps()["amps"] == [48]                                          # the same plug-in
    plugIn(v, c, 32); w.pass_(); assert w.expectedWatts() == pytest.approx(32 * 240)

def test_with_no_plug_in_seen_the_expected_rate_is_none():
    m, v, c, w = homeCar(None); assert w.expectedWatts() == 0

def test_a_rate_after_an_amps_command_is_no_sample():
    m, v, c, w = homeCar(None); v.observe("chargingState", "Disconnected", "TeslaBLE"); c.t += 60
    v.observe("chargingState", "Stopped", "TeslaBLE"); c.t += 5; v.noteWrite("chargeCurrentRequest", 6, "TeslaBLE")
    v.observe("chargeCurrentRequest", 6, "TeslaBLE"); w.pass_(); assert v.homeAmps()["amps"] == []

def test_an_unplugged_or_away_reading_is_no_sample():
    m, v, c, w = homeCar(None); v.observe("chargingState", "Disconnected", "TeslaBLE")
    v.observe("chargeCurrentRequest", 48, "TeslaBLE"); w.pass_(); assert v.homeAmps()["amps"] == []   # its own maximum
    c.t += 60; placeAt(m, v, AWAY); v.observe("bleReach", False, "TeslaBLE"); assert presence(m, v) == NOT_HOME
    plugIn(v, c, 16, "FleetAPI"); w.pass_(); assert v.homeAmps()["amps"] == []                         # an away charger

def test_an_away_chargers_rate_read_before_arrival_is_no_sample():
    # Plugged in away, then home with no reading of its amps since it parked
    m, v, c, w = homeCar(None); c.t += 60; placeAt(m, v, AWAY); v.observe("bleReach", False, "TeslaBLE")
    plugIn(v, c, 16, "FleetAPI"); v.observe("shiftState", "D", "FleetAPI"); c.t += 600
    placeAt(m, v, HOME); v.observe("shiftState", "P", "FleetAPI"); v.observe("chargingState", "Charging", "TeslaBLE")
    w.pass_(); assert presence(m, v) != NOT_HOME and v.homeAmps()["amps"] == []
    c.t += 60; v.observe("chargeCurrentRequest", 48, "FleetAPI"); w.pass_(); assert v.homeAmps()["amps"] == [48]


def test_a_rise_too_small_for_the_car_opens_no_suspicion(requests_mock):
    m, r, v, c, w, car = fleetStep(requests_mock); v.noteHomeAmps(48, since=0)
    step(m, w, c, 500, 3000); assert VIN not in w._suspect                          # 2.5 kW < half 11.5 kW
    step(m, w, c, 3000, 500); T = c.t; step(m, w, c, 500, 12000); assert w._suspect[VIN][0] == T

def test_with_no_samples_a_small_rise_suspects_as_before(requests_mock):
    m, r, v, c, w, car = fleetStep(requests_mock); step(m, w, c, 500, 3000); assert VIN in w._suspect

def test_every_cars_plug_ins_set_the_rate_for_all(caplog):
    # The charger caps the rate more often than the car: one car's plug-ins
    # speak for another with none, and the least of them sets the cutoff
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    other = m.vehicleRegistry.getOrCreate(VIN2, "t"); placeAt(m, other, HOME); other.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); runPending(r.ble); bleAnswers(m, answer=None); v.noteHomeAmps(48, since=0)
    step(m, w, c, 500, 3000); assert " asked" not in caplog.text
    other.noteHomeAmps(16, since=0); assert w.expectedWatts() == pytest.approx(16 * 240)
    c.t += 30; step(m, w, c, 3000, 500); step(m, w, c, 500, 3000)
    assert VIN2 + " asked" in caplog.text and VIN + " asked" in caplog.text

def test_a_small_rise_reads_no_ble_car_with_a_known_rate():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); runPending(r.ble); reads = bleReads(m); v.noteHomeAmps(48, since=0)
    step(m, w, c, 500, 3000); w.pass_(); runPending(r.ble); assert reads.count == 0
    c.t += 1200; step(m, w, c, 3000, 500); step(m, w, c, 500, 12000); w.pass_(); runPending(r.ble)
    assert reads.count == 1


def test_an_away_reading_is_no_sample_when_only_ble_contact_says_home():
    # The away read's own fix marks it outside; contact alone brings it home
    m, v, c, w = homeCar(None); c.t += 60; v.observe("bleReach", False, "TeslaBLE")
    v.observe("lat", AWAY[0], "FleetAPI"); v.observe("lon", AWAY[1], "FleetAPI")
    v.observe("chargingState", "Charging", "FleetAPI"); v.observe("chargeCurrentRequest", 48, "FleetAPI")
    c.t += 600; v.observe("bleReach", True, "TeslaBLE"); w.pass_()
    assert presence(m, v) != NOT_HOME and v.homeAmps()["amps"] == []
    c.t += 60; v.observe("chargeCurrentRequest", 16, "TeslaBLE"); w.pass_(); assert v.homeAmps()["amps"] == [16]

def test_the_amps_a_command_leaves_the_car_holding_is_its_next_start():
    # The car keeps a commanded rate for home, so its next plug-in begins there
    m, v, c, w = homeCar(None); plugIn(v, c, 48); w.pass_(); c.t += 60
    v.noteWrite("chargeCurrentRequest", 8, "TeslaBLE"); w.pass_()
    assert v.homeAmps()["amps"] == [48] and w.expectedWatts() == pytest.approx(8 * 240)

def test_a_held_rate_survives_a_restart():
    from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
    m, v, c, w = homeCar(None); plugIn(v, c, 48); w.pass_(); c.t += 60
    v.noteWrite("chargeCurrentRequest", 8, "TeslaBLE"); w.pass_()
    again = VehicleRegistry({}, clock=c); again.load(m.vehicleRegistry.toJSON())
    assert again.get(VIN).homeAmps()["held"] == 8

def test_a_small_rise_suspects_a_car_holding_a_low_rate(requests_mock):
    m, r, v, c, w, car = fleetStep(requests_mock); v.noteHomeAmps(48, since=0); v.noteHomeHeld(8)
    step(m, w, c, 500, 2500); assert VIN in w._suspect

def test_a_car_a_feed_covers_still_teaches_the_rate():
    from tests.unit.reconcile_helpers import liveFeed
    m, r, v, c = setup(here=False); placeAt(m, v, HOME); liveFeed(m, "TeslaMateVehicle")
    v.observe("state", "online", "TeslaMateVehicle"); v.observe("chargingState", "Disconnected", "TeslaMateVehicle")
    w = Watch(m, clock=c); c.t += 60; plugIn(v, c, 32, "TeslaMateVehicle"); w.pass_()
    assert w.expectedWatts() == pytest.approx(32 * 240)
