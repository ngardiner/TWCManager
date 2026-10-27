# A message that may not wake leaves a sleeping car alone: one sleep-safe
# pre-flight, then nothing until the car shows life or a message must wake
# it. Real master, TeslaBLE (only its tesla-control runs are replaced),
# TeslaAPI and reconciler, as the parity rows.
import logging
from unittest.mock import Mock

import pytest

from TWCManager.Vehicle import Presence
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from TWCManager.Vehicle.Watch import Watch, notWatched
from tests.unit.test_command_path_parity import (
    TM,
    VIN,
    FleetCar,
    bleCommands,
    commands,
    counts,
    install,
    paid,
    passes,
    quiet,  # noqa: F401  (autouse: no real sleeps, unanswered URLs counted)
    realBle,
)
from tests.unit.reconcile_helpers import AWAY, placeAt, runPending
from tests.unit.test_limit_reconciler import setup

# body-controller-state's answer, as the live box's tesla-control prints it
ASLEEP = (
    b'{\n\t"vehicleLockState": "VEHICLELOCKSTATE_UNLOCKED",\n'
    b'\t"vehicleSleepStatus": "VEHICLE_SLEEP_STATUS_ASLEEP",\n'
    b'\t"userPresence": "VEHICLE_USER_PRESENCE_NOT_PRESENT"\n}\n'
)
AWAKE = ASLEEP.replace(b"STATUS_ASLEEP", b"STATUS_AWAKE")


def sleepy(m, answer=ASLEEP):
    # realBle, with the real ping on a scripted body-controller-state
    b = realBle(m)
    b._runPing = TeslaBLE._runPing.__get__(b)
    b._tool = Mock(return_value=(answer, b"", 0))
    return b


def helds(caplog):
    return [r for r in caplog.records if "held: asleep" in r.getMessage()]


def noise(v, c):
    # A charging point's idle reading every second, and the car's feed
    # repeating that it sleeps: none of it is a sign of life
    def each():
        v.observe("actualCurrent", 0, "Gen2TWC", seen=False)
        v.observe("state", "asleep", TM)

    return each


def test_a_lowering_to_a_sleeping_car_holds_after_one_preflight(requests_mock, caplog):
    caplog.set_level(logging.INFO)
    m, r, v, c, api = install()
    b = sleepy(m)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 10)
    passes(r, c, 3600, each=noise(v, c))
    assert b._tool.call_count == 1 and bleCommands(b) == []
    assert v.reported("state")["TeslaBLE"].value == "asleep"
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.lastOutcome.detail == O.HELD + "asleep" and rec.heldAt is not None
    assert len(helds(caplog)) == 1 and requests_mock.call_count == 0
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 70
    assert not [r for r in caplog.records if "no wake for" in r.getMessage()]

    # The car's feed says it is awake: the limit goes, with no second check
    c.t += 1
    v.observe("state", "online", TM)
    passes(r, c, 60)
    assert b._tool.call_count == 1 and bleCommands(b) == ["charging-set-limit"]


def test_a_raise_still_wakes_a_sleeping_car(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m)
    v.observe("chargeLimit", 50, TM)
    v.observe("state", "asleep", TM)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 60)
    assert bleCommands(b) == ["wake", "charging-set-limit"]
    assert b._tool.call_count == 0 and requests_mock.call_count == 0


def test_a_raise_held_at_0_a_wakes_once_there_is_room(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m)
    m.setMaxAmpsToDivideAmongSlaves(0)
    v.observe("chargeLimit", 50, TM)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 10)
    passes(r, c, 600, each=noise(v, c))
    assert b._tool.call_count == 1 and bleCommands(b) == []
    assert m.desired.record(VIN, CHARGE_LIMIT).lastOutcome.detail == O.HELD + "asleep"
    m.setMaxAmpsToDivideAmongSlaves(16)
    passes(r, c, 60, each=noise(v, c))
    assert bleCommands(b) == ["wake", "charging-set-limit"]


def test_a_ble_wake_is_a_sign_of_life(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m)
    v.observe("chargeLimit", 50, TM)
    v.observe("state", "asleep", TM)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 60)
    assert v.reported("state")["TeslaBLE"].value == "online"
    m.setPolicyLimit(60, transition=True)
    passes(r, c, 60)
    assert (
        bleCommands(b) == ["wake", "charging-set-limit"] * 2
    )  # an unknown battery level: expected to charge, so each wakes
    assert b._tool.call_count == 0
    assert m.desired.record(VIN, CHARGE_LIMIT).heldAt is None


def test_a_charging_cars_lowering_needs_no_check(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m)
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 10, each=lambda: v.observe("actualCurrent", 16, "Gen2TWC"))
    assert b._tool.call_count == 0 and bleCommands(b) == [
        "wake",
        "charging-set-limit",
    ]  # still charging after the lower: it may wake


def test_ble_contact_never_masks_a_feeds_drive(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m, AWAKE)
    v.observe("shiftState", "D", TM)
    v.observe("state", "driving", TM)
    c.t += 1
    assert b.ping(VIN) is True and v.latest("state").source == "TeslaBLE"
    assert Presence.decide(m, v)[:2] == (Presence.PRESUMED, "driving")


def test_a_fleet_online_check_that_finds_it_asleep_suppresses_ble(requests_mock):
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="asleep")
    b = sleepy(m)
    assert api.is_awake(v) is False
    assert v.latest("state").value == "asleep" and Presence.asleep(v)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 3600, each=noise(v, c))
    assert b._tool.call_count == 0 and bleCommands(b) == []
    assert paid(requests_mock) == (0, 0, 0)


def onlineChecks(requests_mock):
    return sum(
        q.method == "GET" and q.url.split("?")[0].endswith(VIN)
        for q in requests_mock.request_history
    )


@pytest.mark.parametrize("state", ["asleep", "offline"])
def test_a_fleet_lowering_to_a_car_found_asleep_holds_and_checks_each_15_min(
    requests_mock, state
):
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state=state, limit=90)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    idle = lambda: v.observe("actualCurrent", 0, "Gen2TWC", seen=False)  # noqa: E731
    passes(r, c, 3600 - 60, step=60, each=idle)
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.lastOutcome == O.ASLEEP and paid(requests_mock) == (0, 0, 0)
    assert onlineChecks(requests_mock) == 4  # at once, then every 15 min at home
    car.state = "online"
    passes(r, c, 900, step=60)
    assert commands(requests_mock) == ["set_charge_limit"]


def test_a_fleet_restore_to_a_car_away_and_asleep_is_checked_each_30_min(
    requests_mock,
):
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    FleetCar(requests_mock, state="asleep", limit=50, at=AWAY)
    v.observe("chargeLimit", 50, TM)
    c.t += 1
    placeAt(m, v, AWAY)
    m.setPolicyLimit(50, transition=False)
    passes(r, c, 3600 - 60, step=60)
    assert onlineChecks(requests_mock) == 2 and paid(requests_mock) == (0, 0, 0)


def test_a_live_feed_replaces_the_online_checks_of_a_car_held_asleep(
    requests_mock, caplog
):
    caplog.set_level(logging.INFO)
    m, r, v, c, api = install(fleet=True)
    m.vehicleRegistry.registerLiveness(TM, lambda vehicle: c())
    FleetCar(requests_mock, state="online", limit=90)
    v.observe("state", "asleep", TM)
    v.observe("batteryLevel", 80, TM)
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 3600, step=60, each=noise(v, c))
    assert m.desired.record(VIN, CHARGE_LIMIT).lastOutcome == O.ASLEEP
    assert len(helds(caplog)) == 1 and requests_mock.call_count == 0
    c.t += 1
    v.observe("state", "online", TM)
    passes(r, c, 60)
    assert commands(requests_mock) == ["set_charge_limit"]


def test_a_watched_fleet_car_held_asleep_is_checked_only_by_the_watch(
    requests_mock,
):
    # The watch's status checks refresh a watched car's sleep, so the hold
    # adds no checks of its own: one each homeAsleepSecs, never doubled
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="asleep", limit=90)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.watch = w = Watch(m, clock=c)
    assert notWatched(m, v) is None
    start, at = c(), []

    def run(seconds):
        for _ in range(seconds // 60):
            c.t += 60
            v.observe("actualCurrent", 0, "Gen2TWC", seen=False)
            n = onlineChecks(requests_mock)
            w.pass_()
            runPending(r.cloud)  # the watch's job, as its worker would
            r.pass_()
            at.extend([c() - start] * (onlineChecks(requests_mock) - n))

    run(420)
    m.setPolicyLimit(70, transition=True)  # held off the watch's clock
    run(3600 - 420)
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.lastOutcome == O.ASLEEP and paid(requests_mock) == (0, 0, 0)
    assert at == [60, 480, 960, 1860, 2760]
    car.state = "online"
    run(900)
    assert commands(requests_mock) == ["set_charge_limit"]
    assert at[5:] == [3660, 3900, 4140, 4380]  # then the awake cadence


def test_a_ble_install_asks_the_cloud_only_for_a_held_fleet_command(
    requests_mock,
):
    # The radio watches the car (one with no key, so commands go by Fleet);
    # the cloud is asked only by the hold's own check, on its cadence
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="asleep", limit=90)
    sleep = {"asleep": "asleep"}
    realBle(
        m,
        vins=(),
        ping=lambda vin: v.observe("state", sleep.get(car.state, "online"), "TeslaBLE")
        or True,
    )
    v.observe("batteryLevel", 80, TM)
    v.observe("chargeLimit", 90, TM)
    m.watch = w = Watch(m, clock=c)
    start, at = c(), []

    def run(seconds):
        for _ in range(seconds // 60):
            c.t += 60
            v.observe("actualCurrent", 0, "Gen2TWC", seen=False)
            n = onlineChecks(requests_mock)
            w.pass_()
            runPending(r.ble)
            runPending(r.cloud)
            r.pass_()
            at.extend([c() - start] * (onlineChecks(requests_mock) - n))

    run(1800)
    assert at == []  # watched over the radio alone
    m.setPolicyLimit(70, transition=True)
    run(3600 - 1800)
    assert m.desired.record(VIN, CHARGE_LIMIT).lastOutcome == O.ASLEEP
    assert at == [1860, 2760] and paid(requests_mock) == (0, 0, 0)
    car.state = "online"
    run(1800)
    assert commands(requests_mock) == ["set_charge_limit"]
    n = len(at)
    run(3600)
    assert len(at) == n  # nothing held: no more


def test_a_cloud_command_with_no_online_check_holds_for_a_car_known_asleep(
    requests_mock,
):
    from tests.unit.test_command_path_parity import realHa

    m, r, v, c, api = install()
    realHa(m, requests_mock, (VIN, {"charge_limit_number": "number.limit"}))
    v.observe("state", "asleep", TM)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 600, step=60)
    assert m.desired.record(VIN, CHARGE_LIMIT).lastOutcome == O.ASLEEP
    assert counts(requests_mock)["ha"] == 0
    c.t += 1
    v.observe("state", "online", TM)
    passes(r, c, 60)
    assert counts(requests_mock)["ha"] == 1


def test_a_fleet_wake_is_a_sign_of_life(requests_mock):
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    FleetCar(requests_mock, limit=50)
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 60)
    assert paid(requests_mock) == (1, 1, 1) and not Presence.asleep(v)
    assert v.reported("state")["FleetAPI"].value == "online"


def test_another_sources_sleep_reading_keeps_the_feed_in_sync():
    m, r, v, c = setup(here=False)
    v.observe("state", "online", TM)
    c.t += 1
    v.observe("state", "asleep", "TeslaBLE")
    assert v.syncSource == TM and v.telemetryState()[0].value == "online"


@pytest.mark.parametrize(
    "life",
    [
        ("state", "online", "TeslaMateVehicle"),
        ("state", "online", "TeslaBLE"),
        ("shiftState", "D", "TeslaMateVehicle"),
        ("actualCurrent", 16, "Gen2TWC"),
    ],
)
def test_a_sign_of_life_ends_known_asleep(life):
    m, r, v, c = setup(here=False)
    v.observe("state", "asleep", "TeslaBLE")
    c.t += 1
    for field, value, source in [
        ("state", "asleep", TM),
        ("actualCurrent", 0, "Gen2TWC"),
        ("chargingState", "Stopped", TM),
        ("shiftState", None, TM),
    ]:
        v.observe(field, value, source)
    assert Presence.asleep(v)
    c.t += 1
    v.observe(*life)
    assert not Presence.asleep(v)


def test_a_gen2_plug_attributed_after_a_restart_is_not_a_sign_of_life(requests_mock):
    # A restarted Gen2's first heartbeat begins a plug session for a car that
    # was plugged in all along; attributing it must not end the hold
    m, r, v, c, api = install()
    b = sleepy(m)
    assert b.ping(VIN) and Presence.asleep(v)
    c.t += 4
    m.vehicleRegistry.beginPlugSession("8244")
    c.t += 3
    assert Presence.attribute(m, "8244", "Gen2TWC") == VIN
    assert v.markAt("plugIn") > v.latest("state").at and Presence.asleep(v)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 60, each=noise(v, c))
    assert b._tool.call_count == 1 and bleCommands(b) == []

    # A plug the car itself reports afterwards is life
    c.t += 1
    v.observe("pluggedIn", True, TM)
    v.observe("pluggedIn", False, TM)
    v.observe("pluggedIn", True, TM)
    assert not Presence.asleep(v)


@pytest.mark.parametrize(
    "field, value", [("state", "online"), ("state", "suspended"), ("pluggedIn", True)]
)
def test_a_retained_telemetry_reading_is_not_a_sign_of_life(
    field, value, requests_mock
):
    m, r, v, c, api = install()
    b = sleepy(m)
    assert b.ping(VIN) and Presence.asleep(v)
    c.t += 5
    v.observe(field, value, TM, seen=False)
    assert Presence.asleep(v)
    v.observe("batteryLevel", 80, TM)  # above the new limit: the car quiesces
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 60, each=noise(v, c))
    assert b._tool.call_count == 1 and bleCommands(b) == []
    c.t += 5
    if field == "pluggedIn":
        v.observe(field, False, TM)
        c.t += 1
    v.observe(field, value, TM)  # the same report, live this time
    assert not Presence.asleep(v)


def test_a_charger_plug_it_did_not_see_happen_is_not_life():
    m, r, v, c = setup(here=False)
    v.observe("state", "asleep", "TeslaBLE")
    c.t += 1
    v.observe("pluggedIn", True, "Gen3TWC")  # its first reading, after a restart
    assert Presence.asleep(v)


@pytest.mark.parametrize("source", ["Gen2TWC", "Gen3TWC"])
def test_a_charger_replug_after_an_observed_unplug_is_life(source):
    m, r, v, c = setup(here=False)
    v.observe("pluggedIn", False, source)
    v.observe("state", "asleep", "TeslaBLE")
    c.t += 1
    v.observe("pluggedIn", True, source)
    assert not Presence.asleep(v)


def test_an_unplug_is_a_sign_of_life():
    m, r, v, c = setup()
    v.observe("state", "asleep", "TeslaBLE")
    assert Presence.asleep(v)
    c.t += 1
    v.observe("pluggedIn", False, "Gen3TWC")
    assert not Presence.asleep(v)


@pytest.mark.parametrize(
    "out, state", [(ASLEEP, "asleep"), (AWAKE, "online"), (b"{}", None), (b"x", None)]
)
def test_the_ping_reads_the_sleep_status(out, state):
    m, r, v, c = setup(here=False)
    b = sleepy(m, out)
    assert b.ping(VIN) is True
    seen = v.reported("state").get("TeslaBLE")
    assert (seen and seen.value) == state


def test_the_fleet_feed_compares_with_its_own_state_not_a_sleep_check():
    from TWCManager.Vehicle.FleetTelemetryMQTT import FleetTelemetryMQTT
    from tests.unit.test_telemetry_registry import make

    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    t.applyDataToVehicle(VIN, "VehicleName", "C")
    v = reg.get(VIN)

    def msg(path, payload):
        t.mqttMessage(
            None, None, Mock(topic="telemetry/" + VIN + path, payload=payload)
        )

    msg("/connectivity", b'{"Status": "DISCONNECTED"}')
    v.observe("state", "online", "TeslaBLE")
    msg("/connectivity", b'{"Status": "CONNECTED"}')
    assert v.reported("state")["FleetTelemetryMQTT"].value == "online"
    assert v.telemetryState()[0].source == "FleetTelemetryMQTT"


def test_a_newer_offline_is_not_a_sign_of_life():
    m, r, v, c = setup(here=False)
    v.observe("state", "asleep", "TeslaBLE")
    c.t += 1
    v.observe("state", "offline", "FleetTelemetryMQTT")
    assert Presence.asleep(v)


def test_a_fleet_restore_held_asleep_logs_once_and_rechecks_without_replanning(
    requests_mock, caplog
):
    # Fleet-only, no feed: the restore finds the car asleep and holds. Each
    # free recheck that finds it still asleep neither plans the restore again
    # nor logs the hold again; the car next seen online gets the limit once
    caplog.set_level(logging.INFO)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    car = FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, "FleetAPI", at=c() - 600)
    v.observe("batteryLevel", 95, "FleetAPI", at=c() - 600)  # it won't charge
    m.setPolicyLimit(-1, transition=True)
    idle = lambda: v.observe("actualCurrent", 0, "Gen2TWC", seen=False)  # noqa: E731
    passes(r, c, 4 * 3600, step=60, each=idle)
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.lastOutcome == O.ASLEEP and paid(requests_mock) == (0, 0, 0)
    attempts = [r for r in caplog.records if "setChargeLimit for" in r.getMessage()]
    assert len(helds(caplog)) == 1 and len(attempts) == 1
    # The free check still looks, at home every 15 minutes
    assert 4 * 3600 // 900 <= onlineChecks(requests_mock) <= 4 * 3600 // 900 + 2
    car.state = "online"
    passes(r, c, 900, step=60, each=idle)
    assert commands(requests_mock) == ["set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 90


def test_a_fleet_rate_held_asleep_waits_for_life_not_heartbeats(requests_mock, caplog):
    # Under mode 2 the Fleet API carries the rate, and a billed provider never
    # wakes for amps: the rate holds on an asleep car. The TWC's heartbeat
    # is no sign of life, so the hold is logged once; the free check that
    # finds the car online sends the rate
    from tests.unit.test_amps_reconciler import point

    caplog.set_level(logging.INFO)
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="asleep", charging="Charging")
    pt, s = point(m, v, 2)
    v.observe("chargingState", "Charging", "FleetAPI", at=c() - 600)

    def each():
        pt.setTargetPower(10 * 240)
        v.observe("actualCurrent", 0, "Gen2TWC", seen=False)

    passes(r, c, 2 * 3600, step=10, each=each)
    assert len(helds(caplog)) == 1 and paid(requests_mock) == (0, 0, 0)
    assert onlineChecks(requests_mock) <= 2 * 3600 // 900 + 2
    car.state = "online"
    passes(r, c, 900, step=10, each=each)
    assert commands(requests_mock) == ["set_charging_amps"]
