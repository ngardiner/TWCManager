import logging
import time

from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint
from TWCManager.Policy.Policy import Policy
from TWCManager.Vehicle import Presence as P
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT
from TWCManager.Vehicle.Watch import (
    AWAY_ASLEEP, DEFAULTS, HOME_ASLEEP, HOME_AWAKE, HOME_CHARGING, UNKNOWN, Watch, notWatched, settingsOf, situation)
from TWCManager.Vehicle.WatchBudget import EVENT, WatchBudget
from TWCManager.VehicleCommandQueue import VehicleCommandQueue
from tests.unit.reconcile_helpers import (
    AWAY, HOME, attachFleet, attachOwner, bleAnswers, bleReads, entries, gen2Slave, inline, listApi, liveFeed, placeAt,
    runPending)
from tests.unit.test_command_path_parity import FLEET, OWNER, FleetCar, counts, paid
from tests.unit.test_limit_reconciler import VIN, setup
from tests.unit.test_presence import VIN2, VIN3, VIN4


def test_a_twc_named_car_and_a_feed_covered_car_are_not_watched():
    m, r, v, c = setup(here=False)
    assert notWatched(m, v) is None
    m.vehicleRegistry.observeTwcVin("8244", VIN); slave = gen2Slave(m, "8244", state=0x03)
    assert notWatched(m, v).startswith("named by")
    m2, r2, v2, c2 = setup(here=False)
    v2.observe("state", "online", "TeslaMateVehicle"); liveFeed(m2, "TeslaMateVehicle")     # registers liveness returning now
    assert notWatched(m2, v2) == "covered by TeslaMateVehicle"

def test_situations():
    m, r, v, c = setup(here=False)
    assert situation(m, v) == UNKNOWN
    placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    assert situation(m, v) == HOME_ASLEEP
    c.t += 10; v.observe("state", "online", "TeslaBLE")
    assert situation(m, v) == HOME_AWAKE
    v.observe("chargingState", "Charging", "TeslaBLE")
    assert situation(m, v) == HOME_CHARGING
    c.t += 10                                            # the fix after the plug-in, not tied with it
    placeAt(m, v, AWAY); v.observe("state", "offline", "FleetTelemetryMQTT")
    assert situation(m, v) == AWAY_ASLEEP

def test_ring_one_pings_on_the_cadence_and_not_before():
    m, r, v, c = setup(here=False, ble=True)             # a TeslaBLE module with ping stubbed to answer asleep
    placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); pings = bleAnswers(m, asleep=True)
    w.pass_(); runPending(r.ble)
    assert pings.count == 1
    c.t += 899; w.pass_(); runPending(r.ble); assert pings.count == 1
    c.t += 1;   w.pass_(); runPending(r.ble); assert pings.count == 2
    s = w.status(v)
    assert s["watched"] and s["last"] == "asleep" and s["lastAt"] == c.t and s["nextAt"] == c.t + 900

def test_cloud_status_check_for_a_car_ble_cannot_reach(requests_mock):
    m, r, v, c = setup(here=False); car = FleetCar(requests_mock, state="asleep", at=AWAY)   # test_command_path_parity.FleetCar
    attachFleet(m, v, car)                                # the parity file's way of listing the car on the Fleet slot
    w = Watch(m, clock=c); w.pass_(); c.t += 300; w.pass_(); runPending(r.cloud)        # no fix: the floor from the start
    assert counts(requests_mock)["free"] == 1 and paid(requests_mock) == (0, 0, 0)
    assert v.value("state") == "asleep"

def test_one_watch_job_in_flight_per_car_and_commands_are_not_starved():
    m, r, v, c = setup(here=False, ble=True); cars = [v] + [m.vehicleRegistry.getOrCreate(x, "t") for x in (VIN2, VIN3, VIN4)]
    inline(r, VehicleCommandQueue); v.claim("TeslaBLE", True, "paired")   # a busy worker: every entry waits
    for x in cars: placeAt(m, x, HOME); x.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); w.pass_()
    assert len(entries(r.ble)) == 4                       # one per car, not eight
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()
    late = m.vehicleRegistry.getOrCreate("5YJ3E1EA1KF000005", "t"); placeAt(m, late, HOME)
    w.pass_()                                             # a car's check due after the command waits behind it
    order = [e.kind if hasattr(e, "kind") else "attempt" for e in entries(r.ble)]
    assert order == ["status"] * 4 + ["attempt", "status"]

def test_a_car_in_flight_is_not_asked_again():
    m, r, v, c = setup(here=False, ble=True); inline(r, VehicleCommandQueue)
    placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); w.pass_(); first = entries(r.ble)
    c.t += DEFAULTS["awayAwakeSecs"] + DEFAULTS["homeAsleepSecs"]; w.pass_()
    assert len(first) == 1 and all(a is b for a, b in zip(entries(r.ble), first))
    assert w.status(v)["last"] == ""                      # no job replaced, so none reported

def test_a_feed_blip_shorter_than_the_cadence_makes_no_ping():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME)
    v.observe("state", "online", "TeslaMateVehicle"); feed = liveFeed(m, "TeslaMateVehicle")
    w = Watch(m, clock=c); pings = bleAnswers(m, asleep=True)
    w.pass_(); runPending(r.ble); assert pings.count == 0
    feed.down = True; c.t += 30; w.pass_(); runPending(r.ble)          # not yet due: covered when last checked
    feed.down = False; c.t += 30; w.pass_(); runPending(r.ble)
    assert pings.count == 0
    feed.down = True; c.t += 240; w.pass_(); runPending(r.ble)
    assert pings.count == 1

def test_status_says_why_a_car_is_not_watched_or_has_no_source():
    m, r, v, c = setup(here=False); w = Watch(m, clock=c)
    assert w.status(v)["watched"] and w.status(v)["why"] == "no status source"
    w.pass_(); assert entries(r.ble) == entries(r.cloud) == []
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244")
    assert w.status(v) == dict(w.status(v), watched=False, why="named by TWC 8244")

def test_a_dropped_job_frees_the_car():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); pings = bleAnswers(m, asleep=True)
    r.ble.close(); w.pass_()                              # shutting down: the queue takes no watch job
    r.ble._draining = False
    c.t += DEFAULTS["homeAsleepSecs"]; w.pass_(); runPending(r.ble)
    assert pings.count == 1

def test_settings_overlay_the_defaults():
    m, r, v, c = setup(here=False); m.config["watch"] = {"homeAsleepSecs": 600}
    s = settingsOf(m.config)
    assert s["homeAsleepSecs"] == 600 and s["awayAsleepSecs"] == DEFAULTS["awayAsleepSecs"]

def later(m, v, c, secs):
    # The cloud read dedupe runs on real time: age it with the test clock
    c.t += secs; car = v.attachment("TeslaAPI"); car.statusDeferral -= secs

def batteryPolicy(m):
    # A policy condition reading the Tesla API cars' battery level
    entry = dict(name="Low", match=["modules.TeslaAPI.minBatteryLevelAtHome"], condition=["lt"], value=[50])
    m.config["policy"] = {"override": [entry]}; policy, find = Policy(m), m.getModuleByName
    m.getModuleByName = lambda name: policy if name == "Policy" else find(name)

def chargingFleetCar(requests_mock, m, v, battery=True, **kw):
    car = FleetCar(requests_mock, state="online", charging="Charging", at=HOME, **kw); attachFleet(m, v, car)
    placeAt(m, v, HOME)
    if battery: batteryPolicy(m)
    # Frozen: no refill, so the routine floor is reached on schedule
    m.watchBudget = WatchBudget(3.0, clock=lambda: 1000.0)
    return car

def test_arrival_by_twc_plug_in_reads_the_battery_once():
    m, r, v, c = setup(here=False, ble=True); w = Watch(m, clock=c); reads = bleReads(m, soc=18)
    w.pass_()                                                        # the start: nothing is news yet
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244", state=0x03)
    v.observe("pluggedIn", True, "Gen2TWC")
    w.pass_(); runPending(r.ble)
    assert reads.count == 1 and v.value("batteryLevel") == 18
    c.t += 60; w.pass_(); runPending(r.ble); assert reads.count == 1

def test_a_restart_is_no_arrival():
    m, r, v, c = setup(ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("pluggedIn", True, "TeslaBLE")                         # plugged and awake before the start
    w = Watch(m, clock=c); reads = bleReads(m); bleAnswers(m, answer=None)
    w.pass_(); runPending(r.ble); assert reads.count == 0

def test_a_restart_buys_no_cloud_read(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    w = Watch(m, clock=c); w.pass_(); runPending(r.cloud); assert paid(requests_mock) == (0, 0, 0)

def test_a_car_first_seen_after_the_start_gets_its_arrival_read():
    m, r, v, c = setup(here=False, ble=True); w = Watch(m, clock=c); reads = bleReads(m)
    w.pass_()
    v2 = m.vehicleRegistry.getOrCreate(VIN2, "t"); v2.claim("TeslaBLE", True, "paired")
    m.vehicleRegistry.observeTwcVin("8244", VIN2); gen2Slave(m, "8244", state=0x03); v2.observe("pluggedIn", True, "Gen2TWC")
    w.pass_(); runPending(r.ble); assert reads.count == 1
    c.t += 60; w.pass_(); runPending(r.ble); assert reads.count == 1

def test_a_wake_at_home_reads_once_then_waits_the_courtesy_window():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m, charging="Stopped"); bleAnswers(m, asleep=False)
    w.pass_(); c.t += 10; v.observe("state", "online", "TeslaBLE"); w.pass_(); runPending(r.ble)
    assert reads.count == 1
    for _ in range(4): c.t += 240; w.pass_(); runPending(r.ble)
    assert reads.count == 1                                          # inside sleepCourtesy (1200 s)
    c.t += 300; w.pass_(); runPending(r.ble); assert reads.count == 1  # still no routine read while not charging

def test_the_courtesy_window_yields_to_a_plug_in_and_to_read_now():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m); bleAnswers(m, answer=None); w.pass_()
    c.t += 10; v.observe("state", "online", "TeslaBLE"); w.pass_(); runPending(r.ble); assert reads.count == 1
    c.t += 60; v.observe("state", "asleep", "TeslaBLE"); w.pass_()
    c.t += 60; v.observe("state", "online", "TeslaBLE"); w.pass_(); runPending(r.ble)
    assert reads.count == 1                                          # a wake inside the window
    c.t += 60; v.observe("pluggedIn", True, "TeslaBLE"); w.pass_(); runPending(r.ble); assert reads.count == 2
    c.t += 60; w.readNow(VIN, "test"); w.pass_(); runPending(r.ble); assert reads.count == 3

def test_an_asleep_car_is_never_read_and_its_ask_waits_for_the_wake():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m); bleAnswers(m, asleep=True)
    w.readNow(VIN, "test")
    for _ in range(3): w.pass_(); runPending(r.ble); c.t += 900
    assert reads.count == 0
    v.observe("state", "online", "TeslaBLE"); w.pass_(); runPending(r.ble); assert reads.count == 1

def test_a_charging_car_is_read_on_the_cadence_and_released_when_it_sleeps():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    v.observe("chargingState", "Charging", "TeslaBLE")               # a restart keeps reading a charging car
    w = Watch(m, clock=c); reads = bleReads(m, charging="Charging", amps=24); bleAnswers(m, answer=None)
    w.pass_(); runPending(r.ble); assert reads.count == 1 and situation(m, v) == HOME_CHARGING
    c.t += 300; w.pass_(); runPending(r.ble); assert reads.count == 2
    c.t += 10; v.observe("state", "asleep", "TeslaBLE")
    e = VehicleEndpoint(v, m.providerOrder, m); assert not e.wantsToCharge
    c.t += 300; w.pass_(); runPending(r.ble); assert reads.count == 2

def test_cloud_reads_draw_from_the_budget_and_stop_at_the_floor(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud)   # status check, then the event read
    paidBefore = paid(requests_mock)[0]; assert paidBefore == 1
    for _ in range(40): later(m, v, c, 900); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] <= 26                               # routine reads stop at half the day's tokens
    assert paid(requests_mock)[0] > 20                                # and run until then
    assert m.watchBudget.tokens() >= 0.05 - 0.002

def test_a_refused_read_waits_the_routine_cadence(requests_mock, caplog):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    m.watchBudget.spend(0.049, EVENT)                                 # one event read above the floor
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1
    spend, asked = m.watchBudget.spend, []
    m.watchBudget.spend = lambda *a: asked.append(a) or spend(*a)
    caplog.set_level(logging.DEBUG, "TWCManager.Vehicle.Watch")
    later(m, v, c, 900); w.pass_(); runPending(r.cloud)
    assert len(asked) == 1 and paid(requests_mock)[0] == 1 and "No budget" in caplog.text
    for _ in range(3): later(m, v, c, 60); w.pass_(); runPending(r.cloud)
    assert len(asked) == 1                                            # not asked again every pass
    later(m, v, c, 720); w.pass_(); runPending(r.cloud); assert len(asked) == 2

def test_a_forced_read_inside_the_cloud_dedupe_pays_nothing(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    w = Watch(m, clock=c); w.pass_(); runPending(r.cloud)
    car = v.attachment("TeslaAPI"); car.statusDeferral = until = time.time() + 300
    w.readNow(VIN, "test"); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock) == (0, 0, 0) and car.statusDeferral == until

def test_read_now_is_an_event_read_even_for_a_read_only_car(requests_mock):
    m, r, v, c = setup(here=False); car = FleetCar(requests_mock, state="online", at=HOME, api=OWNER); attachOwner(m, v, car)  # Owner-only: read-only
    placeAt(m, v, HOME)
    w = Watch(m, clock=c); w.pass_(); runPending(r.cloud); n = counts(requests_mock)["free"]
    w.readNow(VIN, "test"); w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["free"] == n + 1 and paid(requests_mock) == (0, 0, 0)

def test_a_read_only_car_gets_no_routine_reads(requests_mock):
    m, r, v, c = setup(here=False); car = FleetCar(requests_mock, state="online", charging="Charging", at=HOME, api=OWNER)
    attachOwner(m, v, car); placeAt(m, v, HOME)
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud); assert situation(m, v) == HOME_CHARGING
    n = counts(requests_mock)["free"]
    for _ in range(4): later(m, v, c, 900); w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["free"] - n == 4                     # the status check only, no vehicle_data

def test_a_twc_named_car_is_read_only_while_current_flows():
    m, r, v, c = setup(here=False, ble=True); w = Watch(m, clock=c); reads = bleReads(m, soc=50); w.pass_()
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244", state=0x03); v.observe("pluggedIn", True, "Gen2TWC")
    w.pass_(); runPending(r.ble); assert reads.count == 1            # the arrival read
    v.observe("actualCurrent", 0, "Gen2TWC", seen=False); c.t += 600; w.pass_(); runPending(r.ble); assert reads.count == 1
    v.observe("actualCurrent", 24, "Gen2TWC", seen=False); c.t += 300; w.pass_(); runPending(r.ble); assert reads.count == 2

def test_a_twc_named_car_is_not_read_for_a_wake():
    m, r, v, c = setup(here=False, ble=True); w = Watch(m, clock=c); reads = bleReads(m); w.pass_()
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244", state=0x03); v.observe("pluggedIn", True, "Gen2TWC")
    w.pass_(); runPending(r.ble); assert reads.count == 1
    c.t += 3000; v.observe("state", "asleep", "TeslaBLE"); w.pass_()
    c.t += 10; v.observe("state", "online", "TeslaBLE"); w.pass_(); runPending(r.ble)
    assert reads.count == 1

def test_a_car_of_unknown_sleep_state_is_pinged_before_a_radio_read():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME)
    w = Watch(m, clock=c); reads = bleReads(m); pings = bleAnswers(m, asleep=False)
    w.readNow(VIN, "test"); w.pass_(); runPending(r.ble); assert pings.count == 1 and reads.count == 0
    w.pass_(); runPending(r.ble); assert reads.count == 1

def test_a_covered_car_keeps_no_ask_for_later(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    v.observe("state", "online", "TeslaMateVehicle"); feed = liveFeed(m, "TeslaMateVehicle")
    w = Watch(m, clock=c); w.readNow(VIN, "test"); w.pass_(); runPending(r.cloud)
    feed.down = True; later(m, v, c, 60); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock) == (0, 0, 0)

def test_cloud_reads_while_charging_run_on_the_slower_cadence(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1
    later(m, v, c, DEFAULTS["chargingReadSecs"]); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1
    later(m, v, c, DEFAULTS["cloudChargingReadSecs"] - DEFAULTS["chargingReadSecs"]); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 2

def test_a_failed_read_the_courtesy_window_bounds_is_retried_within_it(requests_mock):
    m, r, v, c = setup(here=False); attachFleet(m, v, FleetCar(requests_mock, state="online", at=HOME)); placeAt(m, v, HOME)
    requests_mock.get(FLEET + VIN + "/vehicle_data", status_code=500, json={})
    v.observe("state", "asleep", "TeslaMateVehicle")
    w = Watch(m, clock=c); w.pass_(); later(m, v, c, 10); v.observe("state", "online", "TeslaMateVehicle")
    w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1     # the wake read fails
    for _ in range(4): later(m, v, c, 240); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 1                                     # not inside sleepCourtesySecs
    later(m, v, c, 240); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 2
    for _ in range(3): later(m, v, c, 300); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 2                                     # the next is the courtesy window's

def test_a_charge_port_opening_on_an_awake_car_at_home_reads_it_once():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m); bleAnswers(m, asleep=False, charging=False)
    w.pass_(); runPending(r.ble); w.pass_(); runPending(r.ble); assert reads.count == 1   # the wake, door shut
    bleAnswers(m, asleep=False, charging=True)
    c.t += 240; w.pass_(); runPending(r.ble); w.pass_(); runPending(r.ble)
    assert reads.count == 2                                                # inside the courtesy window
    for _ in range(3): c.t += 240; w.pass_(); runPending(r.ble)
    assert reads.count == 2                                                # open is no news

def test_a_failed_read_asked_for_is_tried_once_more_a_cadence_later(requests_mock):
    m, r, v, c = setup(here=False); attachFleet(m, v, FleetCar(requests_mock, state="online", at=HOME)); placeAt(m, v, HOME)
    requests_mock.get(FLEET + VIN + "/vehicle_data", status_code=500, json={})
    w = Watch(m, clock=c); w.pass_(); runPending(r.cloud)
    w.readNow(VIN, "test"); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1
    for _ in range(4): later(m, v, c, 60); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 1                                     # not every pass
    later(m, v, c, 60); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 2
    for _ in range(3): later(m, v, c, 300); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 2                                     # once

def test_a_twc_named_car_read_through_the_cloud_waits_the_cloud_cadence(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v); w = Watch(m, clock=c); w.pass_()
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244", state=0x03); v.observe("pluggedIn", True, "Gen2TWC")
    v.observe("actualCurrent", 24, "Gen2TWC", seen=False)
    w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1     # the plug-in read
    later(m, v, c, DEFAULTS["chargingReadSecs"]); w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1
    later(m, v, c, DEFAULTS["cloudChargingReadSecs"] - DEFAULTS["chargingReadSecs"]); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 2

def test_a_twc_named_car_gets_no_paid_reads_for_a_battery_no_policy_reads(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v, battery=False); w = Watch(m, clock=c); w.pass_()
    m.vehicleRegistry.observeTwcVin("8244", VIN); gen2Slave(m, "8244", state=0x03); v.observe("pluggedIn", True, "Gen2TWC")
    v.observe("actualCurrent", 24, "Gen2TWC", seen=False)
    w.pass_(); runPending(r.cloud); assert paid(requests_mock)[0] == 1     # the plug-in read
    for _ in range(4): later(m, v, c, DEFAULTS["cloudChargingReadSecs"]); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 1

def test_an_anonymous_car_is_not_visited():
    m, r, v, c = setup(here=False, ble=True); m.vehicleRegistry.beginPlugSession("8244")
    w = Watch(m, clock=c); w.pass_()
    assert set(w._seen) == {VIN} and {vin for vin, _ in w._last} <= {VIN}

def test_the_pre_command_ping_trusts_a_watch_ping_inside_its_cadence():
    # Every awake cadence fits inside the ping's own window, so a car the
    # watch heard from is never asked again before a no-wake command
    assert max(DEFAULTS["homeAwakeSecs"], DEFAULTS["awayAwakeSecs"]) <= P.REACH_WINDOW
    m, r, v, c = setup(ble=True, battery=80); placeAt(m, v, HOME); v.claim("TeslaBLE", True, "paired")
    v.observe("chargeLimit", 90, "TeslaMateVehicle"); w = m.watch = Watch(m, clock=c); pings = bleAnswers(m)
    w.pass_(); runPending(r.ble); assert pings.count == 1
    c.t += DEFAULTS["homeAwakeSecs"] - 1; m.desired.set(VIN, CHARGE_LIMIT, 70, "policy"); r.pass_(); runPending(r.ble)
    assert pings.count == 1 and m.desired.record(VIN, CHARGE_LIMIT).lastProvider == "TeslaBLE"


def test_an_api_only_car_charging_is_not_read_for_a_battery_no_policy_reads(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v, battery=False)
    v.observe("state", "online", "FleetAPI"); v.observe("chargingState", "Charging", "FleetAPI")
    w = Watch(m, clock=c); w.pass_()                                     # charging before the start
    for _ in range(12): later(m, v, c, 300); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock) == (0, 0, 0) and situation(m, v) == HOME_CHARGING
    requests_mock.get(FLEET + VIN, json={"response": {"state": "asleep", "vin": VIN}})
    later(m, v, c, 300); w.pass_(); runPending(r.cloud)                  # the charge ended: it sleeps
    e = VehicleEndpoint(v, m.providerOrder, m)
    assert situation(m, v) == HOME_ASLEEP and not e.wantsToCharge and paid(requests_mock)[0] == 0

def test_a_policy_reads_the_battery_where_it_names_it_or_overrides_the_limit():
    m, r, v, c = setup(here=False); batteryPolicy(m); assert m.getModuleByName("Policy").readsBattery()
    m.config["policy"] = {"override": [dict(name="Any", match=["settings.x"], condition=["none"], value=[0])]}
    plain = Policy(m); assert not plain.readsBattery()
    plain.overrideLimit(); assert plain.readsBattery()                  # the override reads the cars' level

def test_an_owner_api_car_charging_is_read_on_the_free_cadence(requests_mock):
    m, r, v, c = setup(here=False); car = FleetCar(requests_mock, state="online", charging="Charging", at=HOME, api=OWNER)
    listApi(m, VIN, owner=True, signing="off"); placeAt(m, v, HOME)      # unsigned: Owner sets its amps
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud)
    reads = lambda: len([q for q in requests_mock.request_history if "/vehicle_data" in q.url])
    n = reads()
    for _ in range(4): later(m, v, c, DEFAULTS["chargingReadSecs"]); w.pass_(); runPending(r.cloud)
    assert reads() - n == 4 and paid(requests_mock) == (0, 0, 0)

