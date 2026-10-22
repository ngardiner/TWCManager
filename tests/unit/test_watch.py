from types import SimpleNamespace
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
    AWAY, HOME, attachFleet, attachOwner, bleAnswers, bleReads, bleScans, entries, gen2Slave, inline, listApi, liveFeed, placeAt,
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

def awayLong(m, v, c, ago=6000):
    # Away, the fix old enough that the car could be home by now
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    for f, x in zip(("lat", "lon"), AWAY): v.observe(f, x, "TeslaMateVehicle", at=c.t - ago)

def test_an_away_awake_car_is_scanned_and_two_misses_record_absence():
    m, r, v, c = setup(here=False, ble=True); awayLong(m, v, c); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    w.pass_(); runPending(r.ble); assert scans.count == 0 and v.latest("bleReach") is None
    c.t += 300; w.pass_(); runPending(r.ble)
    assert scans.count == 1 and v.latest("bleReach").value is False

def test_a_car_believed_asleep_is_scanned_for_after_its_second_miss():
    # A car in range answers asleep, so two misses are RF or a departure,
    # whatever it last said; nothing else may ever refresh that asleep
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    w.pass_(); runPending(r.ble)
    assert scans.count == 0 and v.latest("bleReach") is None and P.presence(m, v) == P.HOME   # one miss says nothing
    c.t += 900; w.pass_(); runPending(r.ble)
    assert scans.count == 1 and v.latest("bleReach").value is False

def awayFleetCar(requests_mock, m, v):
    car = FleetCar(requests_mock, state="online", at=AWAY); attachFleet(m, v, car)
    m.settings.update(homeLat=AWAY[0] + 0.5, homeLon=AWAY[1])        # 55.6 km: 1668 s at 120 km/h
    v.observe("lat", AWAY[0], "FleetAPI"); v.observe("lon", AWAY[1], "FleetAPI")
    return car

def passes(m, v, c, w, r, n, secs=300):
    for _ in range(n): later(m, v, c, secs); w.pass_(); runPending(r.cloud); runPending(r.ble)

def test_a_car_ble_lost_away_is_scanned_for_not_pinged():
    m, r, v, c = setup(here=False, ble=True); awayLong(m, v, c); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    w.pass_(); runPending(r.ble); c.t += 300; w.pass_(); runPending(r.ble)
    assert pings.count == 2 and scans.count == 1
    for _ in range(4): c.t += 75; w.pass_(); runPending(r.ble)
    assert pings.count == 2 and scans.count == 3                       # every scanAwaySecs

def test_after_a_restart_a_car_with_no_fix_waits_the_floor_from_the_start(requests_mock):
    m, r, v, c = setup(here=False); car = FleetCar(requests_mock, state="online", at=AWAY); attachFleet(m, v, car)
    w = Watch(m, clock=c); w.pass_(); later(m, v, c, 299); w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["free"] == paid(requests_mock)[0] == 0
    later(m, v, c, 1); w.pass_(); runPending(r.cloud); assert counts(requests_mock)["free"] == 1
    passes(m, v, c, w, r, 1); assert paid(requests_mock)[0] == 1           # once the cloud says awake

def lostAway(m, v, c):
    awayLong(m, v, c); v.observe("state", "online", "TeslaBLE"); v.observe("bleReach", False, "TeslaBLE")

def test_a_lost_car_with_no_cloud_scans_often_only_while_lately_awake():
    m, r, v, c = setup(here=False, ble=True); lostAway(m, v, c)                # last awake at 1000
    w = Watch(m, clock=c); bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    def run(n):
        for _ in range(n): c.t += 150; w.pass_(); runPending(r.ble)
    run(11); assert scans.count == 11                                     # every scanAwaySecs to 2650
    run(12); assert scans.count == 12                                     # then every awayAsleepSecs
    v.observe("state", "online", "TeslaBLE"); run(2); assert scans.count == 14

def test_one_scan_answers_for_every_car_due_one():
    m, r, v, c = setup(here=False, ble=True); v2 = m.vehicleRegistry.getOrCreate(VIN2, "t")
    lostAway(m, v, c); lostAway(m, v2, c)
    w = Watch(m, clock=c); bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    w.pass_(); runPending(r.ble)
    assert scans.count == 1 and scans.asked == [{VIN, VIN2}]
    c.t += 60; w.pass_(); runPending(r.ble); assert scans.count == 1         # both settled

def northOfHome(m, v, km, at):
    # A fix km due north of home, made at at
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    v.observe("lat", HOME[0] + km / (P._FT_PER_DEGREE / 3280.84), "TeslaMateVehicle", at=at); v.observe("lon", HOME[1], "TeslaMateVehicle", at=at)

def test_nothing_runs_for_an_away_car_before_it_could_be_back():
    m, r, v, c = setup(here=False, ble=True); v.observe("state", "online", "TeslaBLE"); start = c.t
    northOfHome(m, v, 359.99, start)                                       # 3 h home at 120 km/h
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None)
    def at(t): c.t = start + t; w.pass_(); runPending(r.ble); return pings.count
    assert at(0) == at(3600) == 0
    northOfHome(m, v, 359.99, c.t)                                         # a new fix inside the window
    assert at(10800) == at(14399) == 0 and at(14400) == 1

def test_a_near_car_waits_the_floor():
    m, r, v, c = setup(here=False, ble=True); v.observe("state", "online", "TeslaBLE"); start = c.t
    northOfHome(m, v, 1, start); w = Watch(m, clock=c); pings = bleAnswers(m, answer=None)
    c.t = start + 299; w.pass_(); runPending(r.ble); assert pings.count == 0
    c.t = start + 300; w.pass_(); runPending(r.ble); assert pings.count == 1

def test_where_no_scan_can_run_two_unanswered_pings_lose_an_away_car():
    m, r, v, c = setup(here=False, ble=True); awayLong(m, v, c); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None); m.providerOrder.provider("TeslaBLE").module.canScan = lambda: False
    w.pass_(); runPending(r.ble); assert v.latest("bleReach") is None                # one miss says nothing
    c.t += 300; w.pass_(); runPending(r.ble)
    assert v.latest("bleReach").value is False and not v.latest("bleReach").seen     # a miss, as a scan's
    assert w.status(v)["via"] == "ble"                                        # the lost car's back-off ping, not the 300 s one
    for _ in range(24): c.t += 300; w.pass_(); runPending(r.ble)
    assert pings.count == 2 + 4 + 3                                        # to 2800 lately awake, then every 1800 s

def test_a_lost_car_nothing_else_can_hear_is_pinged_on_the_scan_back_off():
    m, r, v, c = setup(here=False, ble=True); lostAway(m, v, c)                # last awake at 1000
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None); m.providerOrder.provider("TeslaBLE").module.canScan = lambda: False
    def run(n):
        for _ in range(n): c.t += 150; w.pass_(); runPending(r.ble)
    run(11); assert pings.count == 11                                     # every scanAwaySecs to 2650
    run(12); assert pings.count == 12                                     # then every awayAsleepSecs
    s = w.status(v); assert s["via"] == "ble" and s["nextAt"] == s["lastAt"] + DEFAULTS["awayAsleepSecs"]

def test_a_lost_car_last_said_asleep_with_no_cloud_stays_on_the_back_off():
    # Nothing can refresh that asleep: the car may wake and drive home unseen
    m, r, v, c = setup(here=False, ble=True); lostAway(m, v, c); c.t += 1; v.observe("state", "asleep", "TeslaBLE")
    w = Watch(m, clock=c); bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    def run(n):
        for _ in range(n): c.t += 150; w.pass_(); runPending(r.ble)
    run(12); assert scans.count == 1                                      # the asleep reading replaced its sign of life
    run(1); assert scans.count == 2                                       # every awayAsleepSecs

def test_status_shows_the_scan_a_lost_car_with_no_cloud_is_listened_for_by():
    m, r, v, c = setup(here=False, ble=True); lostAway(m, v, c)
    w = Watch(m, clock=c); bleAnswers(m, answer=None); bleScans(m, inRange=False)
    c.t += 150; w.pass_(); runPending(r.ble); s = w.status(v)
    assert s["watched"] and s["via"] == "scan" and s["lastAt"] == c.t and s["nextAt"] == c.t + DEFAULTS["scanAwaySecs"]

def noScan(m):
    m.providerOrder.provider("TeslaBLE").module.canScan = lambda: False

def test_where_no_scan_can_run_two_misses_at_home_lose_nothing():
    m, r, v, c = setup(here=False, ble=True); placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None); noScan(m)
    for _ in range(3): w.pass_(); runPending(r.ble); c.t += 240
    assert pings.count == 3 and v.latest("bleReach") is None and P.presence(m, v) == P.HOME

def test_where_no_scan_can_run_two_misses_at_home_keep_the_radio_and_read_once(requests_mock):
    m, r, v, c = setup(here=False, ble=True); attachFleet(m, v, FleetCar(requests_mock, state="online", at=AWAY))
    placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); reads = bleReads(m); pings = bleAnswers(m, answer=None); noScan(m)
    for _ in range(2): w.pass_(); runPending(r.ble); runPending(r.cloud); c.t += 240
    assert pings.count == 2 and counts(requests_mock)["free"] == paid(requests_mock)[0] == 0
    w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert pings.count == 3 and w.status(v)["via"] == "ble"                       # the radio keeps the car
    assert paid(requests_mock)[0] == 1 and P.presence(m, v) == P.NOT_HOME         # the read finds it gone
    assert reads.count == 0                                                       # not over the radio that missed it

def test_a_car_at_home_bluetooth_stopped_hearing_is_read_once_until_it_answers(requests_mock):
    # Present but out of radio reach for hours: one read, not one a courtesy window
    m, r, v, c = setup(here=False, ble=True); attachFleet(m, v, FleetCar(requests_mock, state="online", at=HOME))
    placeAt(m, v, HOME); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); bleReads(m); bleAnswers(m, answer=None); noScan(m)
    for _ in range(90): w.pass_(); runPending(r.ble); runPending(r.cloud); later(m, v, c, 240)
    assert paid(requests_mock)[0] == 1 and P.presence(m, v) == P.HOME
    pings = bleAnswers(m, asleep=False)
    for _ in range(2): w.pass_(); runPending(r.ble); runPending(r.cloud); later(m, v, c, 240)   # the ping answers within two checks
    assert pings.count >= 1; bleAnswers(m, answer=None)
    for _ in range(4): w.pass_(); runPending(r.ble); runPending(r.cloud); later(m, v, c, 240)
    assert paid(requests_mock)[0] == 2                                     # heard again, then lost again

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

def test_status_next_check_waits_for_the_return_time():
    m, r, v, c = setup(here=False, ble=True); awayLong(m, v, c); v.observe("state", "online", "TeslaBLE")
    w = Watch(m, clock=c); bleAnswers(m, answer=None); w.pass_(); runPending(r.ble)
    northOfHome(m, v, 359.99, c.t)                                         # a fix after the check
    assert w.status(v)["nextAt"] > c.t + 10000

def test_a_lost_car_only_home_assistant_can_ask_is_scanned_on_the_back_off():
    # Home Assistant never says asleep or awake, so it gates no scan
    m, r, v, c = setup(here=False, ble=True); awayLong(m, v, c); v.observe("bleReach", False, "TeslaBLE")
    hv = SimpleNamespace(refresh_from_home_assistant=lambda ha: True); v.attach("HomeAssistant", hv)
    ha, find = SimpleNamespace(), m.getModuleByName; m.getModuleByName = lambda name: ha if name == "HomeAssistant" else find(name)
    w = Watch(m, clock=c); bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    def run(n):
        for _ in range(n): c.t += 150; w.pass_(); runPending(r.ble); runPending(r.cloud)
    run(12); assert scans.count == 1                                        # no sign of life: every awayAsleepSecs
    run(1); assert scans.count == 2 and w.status(v)["via"] == "cloud"

def test_with_no_home_a_new_fix_still_moves_the_return_time():
    m, r, v, c = setup(here=False, ble=True); v.observe("state", "online", "TeslaBLE"); start = c.t
    w = Watch(m, clock=c); pings = bleAnswers(m, answer=None); w.pass_()           # no fix: the floor from the start
    c.t = start + 1000; v.observe("lat", 1.0, "TeslaMateVehicle"); v.observe("lon", 1.0, "TeslaMateVehicle")
    c.t = start + 1299; w.pass_(); runPending(r.ble); assert pings.count == 0
    c.t = start + 1300; w.pass_(); runPending(r.ble); assert pings.count == 1

def test_status_shows_the_back_off_for_a_lost_car_last_said_asleep():
    m, r, v, c = setup(here=False, ble=True); lostAway(m, v, c)
    w = Watch(m, clock=c); bleAnswers(m, answer=None); bleScans(m, inRange=False)
    c.t += 150; w.pass_(); runPending(r.ble); v.observe("state", "asleep", "TeslaBLE"); w.pass_()
    assert w.status(v)["lastAt"] == c.t and w.status(v)["nextAt"] == c.t + DEFAULTS["awayAsleepSecs"]

def test_a_lost_car_with_a_cloud_attachment_is_scanned_on_the_back_off_alone():
    m, r, v, c = setup(here=False, ble=True); lostAway(m, v, c); v.attach("TeslaAPI", SimpleNamespace())
    c.t += 3600                                                            # awake long ago: no other reason to scan often
    w = Watch(m, clock=c); bleAnswers(m, answer=None); bleScans(m, inRange=False)
    w.pass_(); runPending(r.ble); s = w.status(v)
    assert s["via"] == "scan" and s["nextAt"] == s["lastAt"] + DEFAULTS["awayAsleepSecs"]

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

# A car only the Tesla API can see: no TeslaBLE module loaded

def apiOnly(requests_mock, state="online", at=HOME):
    m, r, v, c = setup(here=False); car = FleetCar(requests_mock, state=state, at=at); attachFleet(m, v, car)
    return m, r, v, c, car

def readsAt(requests_mock, m, v, c, w, r, until, secs=60):
    # The times of the paid reads made, one pass each secs, to until
    out, start = [], c.t
    while c.t < until:
        n = paid(requests_mock)[0]; later(m, v, c, secs); w.pass_(); runPending(r.cloud)
        out += [c.t - start] * (paid(requests_mock)[0] - n)
    return out

def test_an_api_only_car_is_read_on_its_wake_at_home(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock, state="asleep"); m.config["watch"] = {"homeAsleepSecs": 300}
    placeAt(m, v, HOME); w = Watch(m, clock=c)                           # a fix at 1000
    assert readsAt(requests_mock, m, v, c, w, r, 1360) == [] and situation(m, v) == HOME_ASLEEP
    car.state = "online"                                                 # seen at 1660, read at once
    assert readsAt(requests_mock, m, v, c, w, r, 2160) == [360]          # before the fix is a window old

def test_an_api_only_car_is_read_on_its_wake_away(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock, state="asleep", at=AWAY); m.config["watch"] = {"awayAsleepSecs": 300}
    northOfHome(m, v, 5, c.t); w = Watch(m, clock=c); w.pass_()        # back in 300 s at the earliest
    assert readsAt(requests_mock, m, v, c, w, r, 1360) == [] and situation(m, v) == AWAY_ASLEEP
    car.state = "online"                                                 # seen at 1600, read at once
    assert readsAt(requests_mock, m, v, c, w, r, 2160) == [300]          # before the fix is a window old

def test_an_awake_api_only_car_at_home_is_read_once_a_courtesy_window(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock); placeAt(m, v, HOME)       # a fix at 1000
    w = Watch(m, clock=c); w.pass_()
    assert readsAt(requests_mock, m, v, c, w, r, c.t + 4000) == [1200, 2400, 3600]
    assert P.presence(m, v) == P.HOME

def test_an_api_only_car_away_is_left_alone_until_it_could_be_back(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock, at=AWAY); start = c.t
    northOfHome(m, v, 359.99, start)                                     # 3 h home at 120 km/h
    w = Watch(m, clock=c)
    for _ in range(179): later(m, v, c, 60); w.pass_(); runPending(r.cloud)
    assert requests_mock.request_history == []                           # no request of any kind
    later(m, v, c, 60); w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["free"] == 1

def test_an_awake_api_only_car_past_its_return_time_is_read_and_found_home(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock, at=HOME); m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    v.observe("lat", HOME[0] + 0.5, "FleetAPI"); v.observe("lon", HOME[1], "FleetAPI")   # 55.6 km out: 1668 s
    w = Watch(m, clock=c); w.pass_(); times = readsAt(requests_mock, m, v, c, w, r, c.t + 1800)
    assert times == [1740] and P.presence(m, v) == P.HOME                # once the first check says awake

def test_an_api_only_car_leaving_while_awake_is_seen_within_a_courtesy_window(requests_mock):
    m, r, v, c, car = apiOnly(requests_mock, state="asleep"); placeAt(m, v, HOME)
    w = Watch(m, clock=c); passes(m, v, c, w, r, 1, 900)
    car.state = "online"; passes(m, v, c, w, r, 1, 900); passes(m, v, c, w, r, 1, 60)
    assert paid(requests_mock)[0] == 1                                   # the wake read
    car.at = AWAY                                                        # it drives off, awake
    gone = DEFAULTS["sleepCourtesySecs"] + DEFAULTS["homeAwakeSecs"]
    readsAt(requests_mock, m, v, c, w, r, c.t + gone)
    assert paid(requests_mock)[0] == 2 and P.presence(m, v) == P.NOT_HOME

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

# A BLE install with the Tesla API: the cloud is asked only for a held command

def test_a_lost_car_in_a_ble_install_gets_no_cloud_status_checks(requests_mock):
    m, r, v, c = setup(here=False, ble=True); attachFleet(m, v, FleetCar(requests_mock, state="online", at=AWAY))
    lostAway(m, v, c); w = Watch(m, clock=c); bleAnswers(m, answer=None); scans = bleScans(m, inRange=False)
    for _ in range(40): later(m, v, c, 150); w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert scans.count > 1 and requests_mock.request_history == []

def test_a_failed_read_of_an_api_only_car_away_waits_a_window(requests_mock):
    # A failed read leaves the fix as old as it was: the last read, not the
    # fix, holds off the next
    m, r, v, c, car = apiOnly(requests_mock, at=AWAY); northOfHome(m, v, 5, c.t)
    requests_mock.get(FLEET + VIN + "/vehicle_data", status_code=500, json={})
    w = Watch(m, clock=c); w.pass_()
    assert readsAt(requests_mock, m, v, c, w, r, c.t + 3500) == [1200, 1500, 2700, 3000]   # each window, and its retry

def deadRadio(m, reason):
    # The radio's readiness check found reason
    b = m.providerOrder.provider("TeslaBLE").module; b.canScan = lambda: False; b._scanBlocked = reason
    return b

def test_with_the_radio_down_a_ble_install_follows_the_api_rule_until_it_returns(requests_mock):
    m, r, v, c = setup(here=False, ble=True); car = FleetCar(requests_mock, state="online", at=HOME); attachFleet(m, v, car)
    placeAt(m, v, HOME); b = deadRadio(m, "no Bluetooth controller visible to bluetoothd"); pings = bleAnswers(m, answer=None)
    w = Watch(m, clock=c); w.pass_(); runPending(r.cloud)
    assert readsAt(requests_mock, m, v, c, w, r, c.t + 1300, 240) == [1200]   # the courtesy window's read
    assert pings.count == 0 and counts(requests_mock)["free"] >= 5 and w.status(v)["via"] == "cloud"
    b._scanBlocked = None; pings = bleAnswers(m, asleep=False); n = len(requests_mock.request_history)
    for _ in range(10): later(m, v, c, 240); w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert pings.count > 0 and len(requests_mock.request_history) == n and w.status(v)["via"] == "ble"

def test_with_no_bluetoothctl_a_lost_car_is_pinged_and_the_cloud_never_asked(requests_mock):
    m, r, v, c = setup(here=False, ble=True); attachFleet(m, v, FleetCar(requests_mock, state="online", at=AWAY))
    lostAway(m, v, c); deadRadio(m, "bluetoothctl not installed (install the bluez package)")
    pings = bleAnswers(m, answer=None); w = Watch(m, clock=c)
    for _ in range(40): later(m, v, c, 150); w.pass_(); runPending(r.ble); runPending(r.cloud)
    assert pings.count > 1 and requests_mock.request_history == [] and w.status(v)["via"] == "ble"
