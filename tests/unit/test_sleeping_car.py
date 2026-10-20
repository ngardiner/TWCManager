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
from tests.unit.test_command_path_parity import (
    TM,
    VIN,
    FleetCar,
    bleCommands,
    install,
    paid,
    passes,
    quiet,  # noqa: F401  (autouse: no real sleeps, unanswered URLs counted)
    realBle,
)
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


def test_a_raise_held_at_0_a_wakes_once_offered(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m)
    m.offered = {VIN: 0}
    v.observe("chargeLimit", 50, TM)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 10)
    passes(r, c, 600, each=noise(v, c))
    assert b._tool.call_count == 1 and bleCommands(b) == []
    assert m.desired.record(VIN, CHARGE_LIMIT).lastOutcome.detail == O.HELD + "asleep"
    m.offered = {VIN: 16}
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
    assert bleCommands(b) == ["wake", "charging-set-limit", "charging-set-limit"]
    assert b._tool.call_count == 0
    assert m.desired.record(VIN, CHARGE_LIMIT).heldAt is None


def test_a_charging_cars_lowering_needs_no_check(requests_mock):
    m, r, v, c, api = install()
    b = sleepy(m)
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 10, each=lambda: v.observe("actualCurrent", 16, "Gen2TWC"))
    assert b._tool.call_count == 0 and bleCommands(b) == ["charging-set-limit"]


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
    v.observe("chargeLimit", 90, TM)
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 3600, each=noise(v, c))
    assert b._tool.call_count == 0 and bleCommands(b) == []
    assert paid(requests_mock) == (0, 0, 0)


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
