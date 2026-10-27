# tests/unit/test_presence.py: presence from plug, drive, position and BLE
# events, on a real master and registry with a test clock. Every event gets
# its own time, so no rule is decided by a tie unless the test says so.
import functools
import logging
import re
import time
import urllib.parse
from types import SimpleNamespace
from unittest.mock import Mock, patch
from TWCManager.Vehicle.Registry.Fields import READ_MAX_AGE

import pytest

from TWCManager.Vehicle import Presence as P
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT, Attempt
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit
from TWCManager.Vehicle.Registry.Fields import Observation
from tests.unit.reconcile_helpers import (
    AWAY,
    HOME,
    ScriptedProvider,
    bleAnswers,
    placeAt,
    settled,
)
from tests.unit.teslatokens import FLEET_NA_URL, fleet_token, new_api
from tests.unit.test_limit_reconciler import VIN, ble, prov, setup
from tests.unit.test_teslable_pairing_status import scanner

VIN2, VIN3, VIN4 = "5YJ3E1EA1KF000002", "5YJ3E1EA1KF000003", "5YJ3E1EA1KF000004"
TM = "TeslaMateVehicle"


def home(*providers):
    m, r, v, c = setup(*providers, here=False)
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    return m, r, v, c


def placed(at=HOME):
    m, r, v, c = home()
    placeAt(m, v, at)
    return m, v, c


def later(c, seconds=10):
    c.t += seconds


def why(m, v):
    return P.decide(m, v)[1]


def test_nothing_known_is_presumed():
    m, r, v, c = home()
    assert P.presence(m, v) == P.PRESUMED and P.presence(m, v) != P.NOT_HOME
    assert P.inside(m, v) is None and not P.pluggedNow(m, v)


def test_inside_and_parked_is_home_with_no_shift_state_meaning_not_driving():
    m, v, c = placed()
    assert v.value("shiftState") is None and not P.drivingNow(v)
    assert P.presence(m, v) == P.HOME and why(m, v) == "parked inside the radius"
    later(c)
    v.observe("state", "online", TM)
    assert P.presence(m, v) == P.HOME


def test_driving_inside_is_presumed_even_with_ble_contact():
    m, v, c = placed()
    later(c)
    v.observe("state", "driving", TM)
    later(c)
    v.observe("bleReach", True, "TeslaBLE")  # contact proves presence, not parked
    assert P.drivingNow(v) and P.presence(m, v) == P.PRESUMED
    assert why(m, v) == "driving"


def test_home_is_sticky_while_plugged_or_asleep():
    m, v, c = placed()
    later(c)
    v.observe("chargingState", "Charging", TM)
    assert v.markAt("plugIn") == c.t and P.pluggedHere(m, v)
    later(c, 6 * 3600)  # six hours, no movement
    v.observe("state", "asleep", TM)
    assert P.presence(m, v) == P.HOME and why(m, v) == "plugged in"


def test_repeated_disconnected_reads_are_not_a_departure():
    m, v, c = placed()
    for _ in range(3):
        later(c)
        v.observe("chargingState", "Disconnected", TM)
    assert v.markAt("unplug") == P.NEVER and P.presence(m, v) == P.HOME


def test_an_unplug_event_newer_than_home_evidence_is_a_departure():
    m, v, c = placed()
    later(c)
    v.observe("pluggedIn", True, TM)
    later(c)
    v.observe("pluggedIn", False, TM)
    assert P.decide(m, v) == (P.NOT_HOME, "unplug", c.t) and P.leaving(v) == c.t


def twcNames(m, v, c, state=0x03):
    s = m.getSlaveTWCs()[0]
    s.reportedState = state
    later(c)
    m.vehicleRegistry.observeTwcVin("8244", v.vin)
    return s


def test_a_car_named_on_its_twc_after_an_unended_drive_is_not_driving():
    # The drive's end was never reported: the TWC naming the car, or the
    # car drawing current, says it has stopped, and its limit goes
    m, v, c = placed()
    p = scriptedBle(m)
    v.observe("chargeLimit", 80, TM)
    v.observe("state", "driving", TM)
    assert P.drivingNow(v)
    twcNames(m, v, c)
    assert not P.drivingNow(v) and P.presence(m, v) == P.HOME
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    m.reconciler.pass_()
    assert ("setChargeLimit", VIN, 50) in p.calls
    for at, evidence in (
        (HOME, ("actualCurrent", 48, "Gen2TWC")),
        (AWAY, ("chargingState", "Charging", "FleetAPI")),  # a charger elsewhere
        (HOME, ("pluggedIn", True, "Gen2TWC")),
    ):
        m, v, c = placed(at)
        v.observe("state", "driving", TM)
        later(c)
        v.observe(*evidence)
        assert not P.drivingNow(v), evidence


def test_twc_vin_is_overridden_by_a_newer_drive_start_or_outside_fix():
    m, r, v, c = home()
    twcNames(m, v, c)
    assert P.presence(m, v) == P.HOME and why(m, v) == "named by its charging point"
    later(c)
    v.observe("shiftState", "D", TM)  # position unknown: not outside
    assert v.markAt("driveStart") == c.t and P.twcVinAt(m, v) == P.NEVER
    later(c)
    v.observe("shiftState", "P", TM)
    assert P.presence(m, v) == P.PRESUMED
    m, r, v, c = home()
    twcNames(m, v, c)
    later(c)
    placeAt(m, v, AWAY)
    assert P.presence(m, v) == P.NOT_HOME and why(m, v) == "a fix outside the radius"


def test_twc_vin_counts_only_while_the_twc_reports_a_plug():
    m, r, v, c = home()
    s = twcNames(m, v, c, state=0x00)
    assert P.presence(m, v) == P.PRESUMED
    s.reportedState = 0x03
    assert P.presence(m, v) == P.HOME
    v.observe("twcVin", "Gen3TWC-10.0.0.1", "TWC")  # a Gen3 names a car while connected
    assert P.presence(m, v) == P.HOME


def test_another_vin_at_the_point_clears_the_old_cars_twc_vin():
    m, r, v, c = home()
    other = m.vehicleRegistry.getOrCreate(VIN2, "t")
    twcNames(m, v, c)
    later(c)
    m.vehicleRegistry.observeTwcVin("8244", VIN2)
    assert v.value("twcVin") is None and other.value("twcVin") == "8244"
    assert P.presence(m, v) == P.PRESUMED and P.presence(m, other) == P.HOME


def test_a_fix_while_a_twc_names_the_car_teaches_home():
    m, r, v, c = setup(here=False)
    m.queue_background_task = Mock()
    twcNames(m, v, c)
    v.observe("lat", AWAY[0], TM)
    v.observe("lon", AWAY[1], TM)  # no home yet; no read made for it
    assert m.getHomeLatLon() == list(AWAY)


def test_marks_record_events_when_observed_not_when_evaluated():
    m, v, c = placed()
    assert P.presence(m, v) == P.HOME
    later(c, 5)
    v.observe("shiftState", "D", TM)
    started = c.t
    later(c, 20)  # a 20 s drive, never evaluated in between
    v.observe("shiftState", "P", TM)
    assert (P.leaving(v), P.arriving(v)) == (started, c.t)
    assert P.presence(m, v) == P.PRESUMED  # the drive was seen


def test_lat_and_lon_in_either_order_mark_outside_once():
    m, r, v, c = home()
    v.observe("lon", AWAY[1], TM)  # separate MQTT topics
    later(c, 1)
    v.observe("lat", AWAY[0], TM)
    assert v.markAt("outside") == c.t
    m, v, c = placed()
    later(c)
    v.observe("lat", AWAY[0], TM)  # one coordinate changes alone
    assert v.markAt("outside") == c.t
    m, r, v, c = home()
    v.observe("lat", AWAY[0], TM)
    v.observe("lon", AWAY[1], "OwnerAPI")
    assert v.markAt("outside") == P.NEVER  # another source's coordinate


def test_ble_absence_only_from_a_scan_miss_or_unanswered_ping():
    from tests.unit.test_teslable_pairing_status import VIN as BVIN

    b, reg, master = scanner()
    reg.onObserve = functools.partial(P.noteObservation, master)
    v = reg.getOrCreate(BVIN, "t")
    b._claimUnproven(v)
    (p,) = b.providers()
    b.sendCommand = Mock(return_value=None)
    assert p.setChargeLimit(BVIN, 80).kind == O.UNREACHABLE
    assert v.markAt("absent") == P.NEVER  # a command failure is not absence
    b._noteBleProof(BVIN)
    assert v.markAt("contact") > P.NEVER  # an answered exchange is contact
    miss = (b"[NEW] Device 11:22:33:44:55:66 Other\n", b"", 0)
    b._run_command_with_timeout = lambda *a, **k: miss
    with patch("TWCManager.Vehicle.TeslaBLE.shutil.which", return_value="/bin/bt"):
        assert b.scanForVehicleInRange(BVIN) is False
    assert v.markAt("absent") > P.NEVER


def test_ties_resolve_to_presumed():
    m, r, v, c = home()
    v.mark("plugIn", c.t)
    v.mark("unplug", c.t)
    assert P.presence(m, v) == P.PRESUMED  # home evidence and an unplug at once
    m, r, v, c = home()
    v.mark("plugIn", c.t)
    placeAt(m, v, AWAY)  # a fix outside at the same time
    assert P.presence(m, v) != P.NOT_HOME


def chargingAt(v, c, **charge):
    # One API reading: the charger details and the charging state share a stamp
    later(c)
    for field, value in charge.items():
        v.observe(field, value, "OwnerAPI", at=c.t)
    v.observe("chargingState", "Charging", "OwnerAPI", at=c.t)


def test_dc_power_above_the_cars_ac_max_is_not_plug_evidence():
    m, v, c = placed()
    chargingAt(v, c, chargerPower=50, chargeCurrentRequestMax=48, voltage=400)
    assert v.markAt("plugIn") == P.NEVER and not P.pluggedNow(m, v)
    m, v, c = placed()
    chargingAt(v, c, chargerPower=11, chargeCurrentRequestMax=48, voltage=240)
    assert v.markAt("plugIn") == c.t and P.pluggedNow(m, v)


def test_a_fast_charger_inside_the_radius_marks_no_plug_in():
    for charger in ({"fastChargerPresent": True}, {"fastChargerType": "Supercharger"}):
        m, v, c = placed()
        chargingAt(v, c, **charger)
        assert v.markAt("plugIn") == P.NEVER and not P.pluggedNow(m, v)
    m, v, c = placed()
    chargingAt(v, c, fastChargerType="ACSingleWireCAN")
    assert v.markAt("plugIn") == c.t


def fleetFeed(m, v, c, **topics):
    # Fleet Telemetry values as the MQTT module receives them
    from TWCManager.Vehicle.FleetTelemetryMQTT import FleetTelemetryMQTT

    m.config = {"config": {}, "vehicle": {"teslaFleetTelemetryMQTT": {"enabled": True}}}
    m.getModuleByName = Mock(return_value=None)
    t = FleetTelemetryMQTT(m)
    later(c)
    for topic, payload in topics.items():
        t.applyDataToVehicle(v.vin, topic, payload)


def test_a_telemetry_only_car_at_a_fast_charger_is_not_plug_evidence():
    chargers = (
        {"FastChargerPresent": True},
        {"FastChargerType": "FastChargerSupercharger"},
        {
            "ChargingCableType": "CableTypeUnknown",
            "FastChargerType": "FastChargerCombo",
        },
        {"ACChargingPower": 50.0, "ChargeCurrentRequestMax": 48},
    )
    for charger in chargers:
        m, v, c = placed()
        fleetFeed(m, v, c, **charger, DetailedChargeState="DetailedChargeStateCharging")
        assert v.markAt("plugIn") == P.NEVER and not P.pluggedNow(m, v)
    m, v, c = placed()
    fleetFeed(
        m,
        v,
        c,
        FastChargerPresent=False,
        FastChargerType="FastChargerACSingleWireCAN",
        ChargingCableType="CableTypeIEC",
        ACChargingPower=7.2,
        ChargeCurrentRequestMax=32,
        DetailedChargeState="DetailedChargeStateCharging",
    )
    assert v.markAt("plugIn") == c.t and P.pluggedNow(m, v)


def test_a_plugged_report_with_no_cable_is_not_home():
    m, v, c = placed()
    chargingAt(v, c, connChargeCable="<invalid>")
    assert v.markAt("plugIn") == P.NEVER
    m, v, c = placed(AWAY)
    chargingAt(v, c, connChargeCable="IEC")
    assert v.markAt("plugIn") == P.NEVER  # a car source counts only inside


def test_charging_state_maps_to_plug_values():
    def plug(value, source="OwnerAPI"):
        return P._plug("chargingState", Observation(value, source, 0.0))

    assert plug("Disconnected") is False
    assert [plug(x) for x in ("", "Unknown", "SNA", "None", None)] == [None] * 5
    assert all(plug(x) is True for x in P._PLUGGED)
    assert (plug("Charging", "HomeAssistant"), plug("Stopped", "HomeAssistant")) == (
        True,
        None,
    )
    assert P._plug("pluggedIn", Observation(False, TM, 0.0)) is False


def test_home_radius_defaults_to_1000_ft_and_config_overrides(caplog):
    m, v, c = placed((HOME[0] + 1500 / 364488.888, HOME[1]))
    assert P.inside(m, v) is False
    m.config["config"]["atHomeRadius"] = 2000
    assert P.inside(m, v) is True
    m.settings.update(homeLat=10000, homeLon=10000)
    P._warnNoHome.cache_clear()
    with caplog.at_level(logging.WARNING):
        assert P.inside(m, v) is None and P.inside(m, v) is None
    assert caplog.text.count("No home location set") == 1


def test_candidates_for_a_broadcast_are_only_cars_base_reached():
    b = ScriptedProvider("TeslaBLE", 2, True, [], readyFor=lambda v, cap: True)
    m, r, v, c = home(b)
    reg = m.vehicleRegistry
    v.attach("TeslaAPI", SimpleNamespace())  # listed, no position: presumed
    away = reg.getOrCreate(VIN2, "t")
    away.attach("TeslaAPI", SimpleNamespace())
    placeAt(m, away, AWAY)
    key = reg.getOrCreate(VIN3, "t")
    key.claim("TeslaBLE", True, "paired")
    ha = reg.getOrCreate(VIN4, "t")
    ha.attach("HomeAssistant", SimpleNamespace())
    reg.beginPlugSession("8245")
    assert P.candidates(m, C.START_CHARGING) == [v, key]  # HA presumed: not reached
    placeAt(m, ha, HOME)
    assert P.candidates(m, C.START_CHARGING) == [v, key]  # HA home, on no TWC
    m.getSlaveTWCs()[0].currentVIN = VIN4
    assert P.candidates(m, C.START_CHARGING) == [v, key, ha]  # and on a TWC
    twcNames(m, v, c)
    assert P.candidates(m, C.START_CHARGING) == [key, ha]  # named by its point
    assert P.candidates(m) == [key, ha]  # attribution: presumed or home
    later(c)
    away.observe("bleReach", True, "TeslaBLE")
    later(c)
    placeAt(m, away, AWAY)
    assert P.presence(m, away) == P.NOT_HOME and away in P.candidates(m)
    later(c, P.REACH_WINDOW + 1)
    assert away not in P.candidates(m)  # BLE contact counts for five minutes
    later(c)
    away.observe("bleReach", True, "TeslaBLE")
    later(c)
    away.observe("bleReach", False, "TeslaBLE")
    later(c)
    placeAt(m, away, AWAY)
    assert away not in P.candidates(m)  # out of range since the contact


def test_counts_include_presumed_cars():
    m, r, v, c = home()
    api, reg = new_api(master=m), m.vehicleRegistry
    for i, vin in enumerate((VIN, VIN2, VIN3)):
        api.addVehicle({"id": i, "vin": vin, "display_name": None})
    placeAt(m, reg.get(VIN), HOME)
    placeAt(m, reg.get(VIN3), AWAY)
    reg.get(VIN).observe("batteryLevel", 60, TM)
    reg.get(VIN2).observe("batteryLevel", 40, TM)
    assert api.numCarsAtHome == 2 and api.minBatteryLevelAtHome == 40
    assert [x.vin for x, _ in P.homeCars(m, "TeslaAPI")] == [VIN, VIN2]


def fix(v):
    return v.latest("lat"), v.latest("lon")


def test_home_is_learned_only_from_a_twc_named_car_with_a_fresh_fix():
    m, r, v, c = setup(here=False)
    m.queue_background_task = Mock()
    assert P.learnHome(m, v, *fix(v)) is False  # no fix
    placeAt(m, v, AWAY)
    m.settings.update(homeLat=10000, homeLon=10000)
    later(c, P.HOME_FIX_MAX_AGE + 1)
    assert P.learnHome(m, v, *fix(v)) is False  # stale
    placeAt(m, v, AWAY)
    m.settings.update(homeLat=10000, homeLon=10000)
    assert P.learnHome(m, v, *fix(v)) is True and m.getHomeLatLon() == list(AWAY)
    assert P.learnHome(m, v, *fix(v)) is False  # already inside
    m.queue_background_task.assert_any_call({"cmd": "saveSettings"})


def bleFix(m, v, at, gpsAsOf):
    from TWCManager.Vehicle.Providers.TeslaBLEProvider import TeslaBLEProvider

    ble = Mock()
    loc = {"latitude": at[0], "longitude": at[1], "gpsAsOf": gpsAsOf}
    ble.get_location_state.return_value = loc
    return TeslaBLEProvider(ble, m).fetch(v, "lat")


def test_a_ble_fix_moves_home_only_while_its_gps_time_is_fresh():
    m, r, v, c = home()
    m.queue_background_task = Mock()
    later(c, 5 * 3600)
    assert bleFix(m, v, AWAY, c.t - 5 * 3600)  # GPS frozen since then
    assert m.getHomeLatLon() == list(HOME)
    later(c)
    assert bleFix(m, v, AWAY, c.t - 5)
    assert m.getHomeLatLon() == list(AWAY)


def test_a_fresh_fix_outside_moves_home_when_a_twc_reads_the_cars_vin(monkeypatch):
    m, r, v, c = home()
    m.queue_background_task = Mock()
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    s.reportedState, s.currentVIN = 0x03, VIN
    placeAt(m, v, AWAY)
    later(c, P.HOME_FIX_MAX_AGE + 1)
    m.recordVehicleVIN(s)
    assert m.getHomeLatLon() == list(HOME)  # stale by the time it is named
    placeAt(m, v, AWAY)
    later(c)
    m.recordVehicleVIN(s)
    assert m.getHomeLatLon() == list(AWAY) and P.presence(m, v) == P.HOME


def test_a_retained_position_never_moves_home_at_a_vin_read(monkeypatch):
    m, r, v, c = home()
    m.queue_background_task = Mock()
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    s.reportedState, s.currentVIN = 0x03, VIN
    v.observe("lat", AWAY[0], "TeslaMateVehicle", seen=False)
    v.observe("lon", AWAY[1], "TeslaMateVehicle", seen=False)
    m.recordVehicleVIN(s)
    assert m.getHomeLatLon() == list(HOME)


def test_a_home_assistant_position_never_moves_home_at_a_vin_read(monkeypatch):
    from TWCManager.Vehicle.HomeAssistant import HaVehicle

    m, r, v, c = home()
    m.queue_background_task = Mock()
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    s.reportedState, s.currentVIN = 0x03, VIN
    car = HaVehicle("Car", VIN, "d", {})
    car.parse_from_home_assistant = lambda api: {"lat": AWAY[0], "lon": AWAY[1]}
    car.refresh_from_home_assistant(SimpleNamespace(registry=m.vehicleRegistry))
    m.recordVehicleVIN(s)
    assert v.value("lat") == AWAY[0] and m.getHomeLatLon() == list(HOME)


def fleetCars(requests_mock, m):
    api = new_api(master=m)
    api.storeToken(fleet_token())
    for p in api.providers():
        m.providerOrder.register(p)
    listing = [
        {"id": i, "vin": x, "display_name": None} for i, x in enumerate((VIN, VIN2))
    ]
    requests_mock.get(FLEET_NA_URL, json={"count": 2, "response": listing})
    data = {
        "drive_state": {"latitude": HOME[0], "longitude": HOME[1], "shift_state": "P"},
        "charge_state": {"battery_level": 50},
        "vehicle_state": {},
    }
    for vin in (VIN, VIN2):
        requests_mock.get(
            FLEET_NA_URL + "/" + vin, json={"response": {"state": "online"}}
        )
        requests_mock.get(
            FLEET_NA_URL + "/" + vin + "/vehicle_data", json={"response": data}
        )
    requests_mock.post(
        re.compile(r".*/command/.*"), json={"response": {"result": True}}
    )
    api.loadList()
    return api


def test_a_power_start_asks_a_read_of_home_cars_only(requests_mock):
    m, r, v, c = setup(clock=False, here=False)
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    fleetCars(requests_mock, m)
    m.watch = Mock()
    placeAt(m, v, AWAY)
    m.updateChargeAtHome()
    assert not m.watch.readNow.called
    placeAt(m, v, HOME)
    m.updateChargeAtHome()
    m.watch.readNow.assert_called_once_with(VIN, "power start")  # VIN2: presumed


@pytest.mark.parametrize("gpsAge, moved", [(5 * 3600, False), (5, True)])
def test_a_fleet_fix_with_a_stale_gps_time_never_seeds_home(
    requests_mock, gpsAge, moved
):
    m, r, v, c = setup(here=False)
    api = fleetCars(requests_mock, m)
    m.queue_background_task = Mock()
    m.settings.update(homeLat=10000, homeLon=10000)
    twcNames(m, v, c)
    later(c, 5 * 3600)
    drive = {"latitude": AWAY[0], "longitude": AWAY[1], "gps_as_of": c.t - gpsAge}
    data = {"drive_state": drive, "charge_state": {}, "vehicle_state": {}}
    url = FLEET_NA_URL + "/" + VIN + "/vehicle_data"
    requests_mock.get(url, json={"response": data})
    assert api.update_vehicle_data(v) is True
    assert m.getHomeLatLon() == (list(AWAY) if moved else [10000, 10000])


def test_every_read_defers_the_next_by_the_read_max_age():
    # Near home or far, parked or driving: when to read again is the watch's
    m, r, v, c = setup(clock=False, here=False)
    api = new_api(master=m)
    api.addVehicle({"id": 1, "vin": VIN, "display_name": None})
    car = v.attachment("TeslaAPI")
    api.slotFor = Mock(return_value=Mock())
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    for at, shift in ((HOME, "P"), ((HOME[0] + 2, HOME[1]), "P"), (AWAY, "D")):
        drive = {"latitude": at[0], "longitude": at[1], "shift_state": shift}
        api.get_car_api = Mock(
            return_value=(True, {"drive_state": drive, "charge_state": {}})
        )
        car.statusDeferral = 0
        assert api.update_vehicle_data(v) is True
        assert round(car.statusDeferral - time.time()) == READ_MAX_AGE


def test_policy_wide_apply_skips_a_departed_car_on_every_channel(requests_mock):
    m, r, v, c = home(ble(O.Outcome(O.DONE)))
    gone = m.vehicleRegistry.getOrCreate(VIN2, "t")
    placeAt(m, v, HOME)
    placeAt(m, gone, AWAY)
    for car in (v, gone):
        car.observe("chargeLimit", 80, TM)
    m.setPolicyLimit(70, transition=True)
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 70
    assert m.desired.get(VIN2, CHARGE_LIMIT) is None
    r.pass_()
    assert prov(m, "TeslaBLE").calls == [
        ("wake", VIN, None),
        ("setChargeLimit", VIN, 70),
    ]
    assert requests_mock.call_count == 0


# One applicability rule per message


def car(state, plugged=False, gone=None, back=None, held=False):
    # A car in the given state; gone/back: the leaving and arriving marks
    m, r, v, c = home()
    if plugged:
        v.observe("pluggedIn", True, "Gen3TWC", at=c.t - 50)
    if gone is not None:
        v.mark("unplug", c.t + gone)
    if back is not None:
        v.mark("driveEnd", c.t + back)
    if state == P.HOME:
        v.mark("contact", c.t + 100)
    elif state == P.NOT_HOME:
        placeAt(m, v, AWAY)
    elif state == "driving":
        v.observe("state", "driving", TM)
    elif state == "inside, not parked":
        placeAt(m, v, HOME)
        v.observe("shiftState", "SNA", TM)
    elif state == "presumed, plugged":
        v.mark("absent", c.t + 10)  # out of BLE range since the plug-in
    elif state == "presumed after an unplug":
        v.mark("contact", c.t + 20)
        v.mark("driveStart", c.t + 30)
    if held:
        m.heldLimits.set(VIN, 90, 50)
    return m, v


def test_applicability_every_cell():
    start, limit, amps = C.START_CHARGING, C.SET_CHARGE_LIMIT, C.SET_AMPS
    plugged, presumedPlugged = "plugged in", "presumed, plugged"
    rows = [
        # (capability, car, verdict, the cell's reason)
        (start, car(P.HOME, plugged=True), P.SEND, plugged),
        (amps, car(P.HOME, plugged=True), P.SEND, plugged),
        (start, car(P.HOME), P.NOT_HERE, "not plugged in"),
        (amps, car(P.HOME), P.NOT_HERE, "not plugged in"),
        (start, car("driving", plugged=True), P.SEND, plugged),
        (start, car(P.PRESUMED, held=True), P.SEND, plugged),  # presumed
        (
            start,
            car("presumed after an unplug", gone=10, held=True),
            P.HOLD,
            "not plugged in",
        ),
        (amps, car(P.PRESUMED), P.HOLD, "not plugged in"),
        (start, car(P.NOT_HOME, plugged=True), P.NOT_HERE, "away"),
        (limit, car(P.HOME, plugged=True, gone=20), P.SEND, "home"),
        (limit, car(P.HOME), P.SEND, "home"),  # no departure seen
        (limit, car(P.HOME, gone=10, back=20), P.SEND, "home"),
        (limit, car(P.HOME, gone=20, back=10), P.NOT_HERE, "leaving"),
        (limit, car(P.HOME, gone=20, back=20), P.HOLD, "tie"),
        (limit, car("driving", plugged=True), P.HOLD, "driving"),
        (limit, car(presumedPlugged, plugged=True), P.SEND, plugged),
        (limit, car(P.PRESUMED), P.HOLD, "not known here"),
        (limit, car("inside, not parked"), P.HOLD, "not parked"),
        (limit, car(P.NOT_HOME), P.NOT_HERE, "away"),
    ]
    for i, (capability, (m, v), verdict, why) in enumerate(rows):
        assert P.applicable(m, v, capability) == (verdict, why), i
    m, v = car(presumedPlugged, plugged=True)
    assert P.presence(m, v) == P.PRESUMED and P.pluggedNow(m, v)


def test_a_restore_goes_wherever_the_car_is():
    restore = Attempt(
        VIN, CHARGE_LIMIT, C.SET_CHARGE_LIMIT, 90, "x", False, None, True, 0.0
    )
    for m, v in (
        car(P.HOME, plugged=True),
        car(P.HOME, gone=10, back=20),
        car(P.PRESUMED),
        car("driving"),
        car(P.NOT_HOME),
    ):
        assert m.reconciler.inapplicable(v, restore) is None


def scriptedBle(m, *outcomes):
    p = ble(*(outcomes or [O.Outcome(O.DONE)]))
    m.providerOrder.register(p)
    bleAnswers(m)
    return p


def test_a_presumed_car_is_pinged_first_and_an_answer_sends_free(requests_mock):
    from tests.unit.test_command_path_parity import (
        FleetCar,
        bleCommands,
        install,
        paid,
        realBle,
    )

    m, r, v, c, api = install(fleet=True, located=False)
    b = realBle(m)
    FleetCar(requests_mock, limit=50)
    v.observe("chargeLimit", 50, TM)
    assert P.presence(m, v) == P.PRESUMED
    m.setPolicyLimit(80, transition=True)
    r.pass_()
    assert b.pings == [VIN] and v.markAt("contact") == c.t
    assert bleCommands(b) == ["wake", "charging-set-limit"]  # a raise, woken free
    assert paid(requests_mock) == (0, 0, 0)


def test_one_missed_ping_does_not_open_the_paid_path(requests_mock):
    from tests.unit.test_command_path_parity import (
        FleetCar,
        install,
        paid,
        passes,
        realBle,
    )
    from TWCManager.Vehicle.Reconcile.Reconciler import PAID_AFTER

    m, r, v, c, api = install(fleet=True, located=False)
    b = realBle(m, ping=False)
    FleetCar(requests_mock, limit=50)
    v.observe("chargeLimit", 50, TM)
    m.setPolicyLimit(80, transition=True)
    r.pass_()
    assert b.pings == [VIN] and b.runs == [] and v.markAt("absent") == c.t
    last = m.desired.record(VIN, CHARGE_LIMIT).lastOutcome
    assert last == O.Outcome(O.UNREACHABLE, "no BLE answer")
    passes(r, c, PAID_AFTER[CHARGE_LIMIT] - 10)
    assert paid(requests_mock) == (0, 0, 0) and b.pings == [VIN]


@pytest.fixture
def nosleep(monkeypatch):
    import TWCManager.Vehicle.Providers.TeslaAPIProviders as providers
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    for module in (teslaapi, providers):
        monkeypatch.setattr(module.time, "sleep", lambda s: None)


def heldFleetCar(requests_mock):
    # A Fleet-only car, no home set: its read places nothing, so a raise holds
    from tests.unit.test_command_path_parity import FleetCar, install

    m, r, v, c, api = install(fleet=True, located=False)
    car = FleetCar(requests_mock, limit=50)
    v.observe("chargeLimit", 50, "OwnerAPI", at=c.t - 3600)
    m.setPolicyLimit(80, transition=True)
    return m, r, v, c, car


def test_a_hold_reads_once_and_waits_for_pushed_evidence(requests_mock, nosleep):
    from tests.unit.test_command_path_parity import paid, passes

    m, r, v, c, car = heldFleetCar(requests_mock)
    r.pass_()
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert paid(requests_mock) == (1, 1, 0) and rec.heldAt == c.t
    assert rec.lastOutcome.detail == "rules: held: not known here"
    assert rec.paidSpent == 2 and rec.settledAt is None
    seen = len(requests_mock.request_history)
    passes(r, c, 3600)
    assert len(requests_mock.request_history) == seen  # nothing new: no re-plan
    later(c)
    v.observe("state", "online", TM, seen=False)  # pushed, not a sighting
    r.pass_()
    assert rec.heldAt == c.t  # planned again, and held again
    assert paid(requests_mock) == (1, 1, 0)  # awake and freshly read: free


def test_a_driving_hold_waits_for_the_car_to_stop(requests_mock, nosleep, caplog):
    # A reading each second while it drives plans nothing more; once it
    # stops, the hold is planned again for its new reason
    from tests.unit.test_command_path_parity import paid, passes

    caplog.set_level(logging.INFO)
    m, r, v, c, car = heldFleetCar(requests_mock)
    car.state = "online"
    v.observe("state", "driving", TM)
    # The feed's own limit each second: fresh, so no attempt reads
    pushed = lambda: v.observe("chargeLimit", 50, TM)
    passes(r, c, 60, step=1, each=pushed)

    def lines(text):
        return [x for x in caplog.records if text in x.getMessage()]

    assert len(lines("held: driving")) == 1 and len(lines("hold (driving)")) == 1
    later(c)
    v.observe("state", "online", TM)
    r.pass_()
    assert len(lines("held: not known here")) == 1
    assert paid(requests_mock) == (0, 0, 0)


def test_a_held_car_driving_in_ble_range_is_never_pinged(requests_mock):
    # Inside the radius, BLE answering, a position each second for 120 s
    from tests.unit.test_command_path_parity import install, passes, push, realBle

    m, r, v, c, api = install()
    b = realBle(m)
    v.observe("chargeLimit", 90, TM)
    push(v, c, state="driving", shiftState="D")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, 120, step=1, each=lambda: placeAt(m, v, HOME))
    assert b.pings == [] and b.runs == []
    assert m.desired.record(VIN, CHARGE_LIMIT).heldAt is not None


def test_a_driving_hold_clears_once_the_car_is_seen_away(requests_mock):
    from tests.unit.test_command_path_parity import install, passes, push, realBle

    m, r, v, c, api = install()
    b = realBle(m)
    v.observe("chargeLimit", 90, TM)
    push(v, c, state="driving", shiftState="D")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, 30, step=1, each=lambda: placeAt(m, v, HOME))
    assert m.desired.record(VIN, CHARGE_LIMIT).heldAt is not None
    passes(r, c, 30, step=1, each=lambda: placeAt(m, v, AWAY))
    assert m.desired.get(VIN, CHARGE_LIMIT) is None
    assert b.pings == [] and b.runs == []


def test_held_re_plans_stop_paying_after_the_budget(requests_mock, nosleep):
    from tests.unit.test_command_path_parity import counts, passes

    m, r, v, c, car = heldFleetCar(requests_mock)

    def push():
        if c.t % 1800 == 0:
            car.state = "asleep"  # each re-plan wakes and reads again
            v.observe("state", "asleep", TM, seen=False)

    passes(r, c, 6 * 3600, each=push)
    spent = counts(requests_mock)
    assert spent["command"] == 0
    assert spent["vehicle_data"] + spent["wake_up"] <= 4
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.paidSends + rec.paidSpent >= 4 and rec.heldAt is not None


def test_a_held_desire_is_not_blocked_by_settled_at():
    m, v, c = placed()
    p = scriptedBle(m)
    m.vehicleRegistry.getOrCreate(VIN, "t")
    v.observe("chargeLimit", 50, TM)
    later(c)
    v.observe("state", "driving", TM)
    later(c)
    v.observe("bleReach", True, "TeslaBLE")  # in range, still moving
    m.desired.set(VIN, CHARGE_LIMIT, 60, "policy")
    m.reconciler.pass_()
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert p.calls == [] and rec.heldAt == c.t and rec.settledAt is None
    later(c, 180)
    v.observe("state", "online", TM)  # parked inside: an arrival
    m.reconciler.pass_()
    assert p.calls[-1] == ("setChargeLimit", VIN, 60) and rec.heldAt is None


def test_start_and_amps_need_a_plug_here():
    from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGING

    m, v, c = placed()
    p = scriptedBle(m)
    v.observe("chargingState", "Stopped", "TeslaBLE")
    later(c)
    v.observe("pluggedIn", False, TM)  # the cable is out
    m.desired.set(VIN, CHARGING, True, "distributor")
    m.desired.set(VIN, AMPS, 16, "distributor")
    for _ in range(2):
        m.reconciler.pass_()
    assert p.calls == [] and m.desired.get(VIN, CHARGING) is None
    assert m.desired.get(VIN, AMPS) is None  # not here: both withdrawn
    later(c)
    v.observe("pluggedIn", True, TM)  # plugged in again
    m.desired.set(VIN, CHARGING, True, "distributor")
    m.desired.set(VIN, AMPS, 16, "distributor")
    m.reconciler.pass_()
    assert [x[0] for x in p.calls if x[0] != "wake"] == ["setAmps", "startCharging"]


def unplugged(held=True):
    # Plugged in at home under a held limit, then unplugged
    m, v, c = placed()
    v.observe("chargeLimit", 50, TM)
    later(c)
    v.observe("chargingState", "Charging", TM)
    if held:
        m.heldLimits.set(VIN, 90, 50)
    later(c)
    v.observe("chargingState", "Disconnected", TM)
    return m, v, c


def test_a_restores_own_ble_reply_re_applies_nothing():
    m, v, c = unplugged()
    p = scriptedBle(m)
    m.setPolicyLimit(50, transition=False)  # the policy pass restores
    assert m.desired.get(VIN, CHARGE_LIMIT).restore
    m.reconciler.pass_()
    assert p.calls == [("setChargeLimit", VIN, 90)] and m.heldLimits.get(VIN) is None
    later(c)
    v.observe("bleReach", True, "TeslaBLE")  # the restore's own reply
    assert P.presence(m, v) == P.HOME and P.leaving(v) > P.arriving(v)
    for _ in range(3):
        later(c, 60)
        m.setPolicyLimit(50, transition=False)
        m.reconciler.pass_()
    assert len(p.calls) == 1 and settled(m.desired, VIN, CHARGE_LIMIT)


def test_a_plug_or_a_newer_arrival_cancels_a_pending_restore():
    for back in ("plug", "arrival"):
        m, v, c = unplugged()
        p = scriptedBle(m)
        later(c)
        v.observe("bleReach", True, "TeslaBLE")
        m.setPolicyLimit(50, transition=False)
        assert m.desired.get(VIN, CHARGE_LIMIT).restore  # leaving, not yet sent
        later(c)
        if back == "plug":
            v.observe("chargingState", "Charging", TM)  # the arrival decision
        else:
            v.observe("shiftState", "D", TM)
            later(c, 20)
            v.observe("shiftState", "P", TM)
            m.setPolicyLimit(50, transition=False)  # the next policy pass
        m.reconciler.pass_()
        d = m.desired.get(VIN, CHARGE_LIMIT)
        assert p.calls == [] and m.heldLimits.get(VIN) is not None, back
        assert d.value == 50 and not d.restore, back  # the policy limit, held


def test_a_policy_with_no_limit_is_an_apply_of_the_cars_own_once():
    from TWCManager.Vehicle.Reconcile.Desired import RELEASE

    m, v, c = placed()
    p = scriptedBle(m)
    v.observe("chargeLimit", 50, TM)
    later(c)
    v.observe("chargingState", "Charging", TM)
    m.heldLimits.set(VIN, 90, 50)
    m.setPolicyLimit(None, transition=False)  # the policy has no limit
    d = m.desired.get(VIN, CHARGE_LIMIT)
    assert (d.value, d.restore) == (RELEASE, False)
    for _ in range(5):
        m.reconciler.pass_()
        later(c, 60)
        m.setPolicyLimit(None, transition=False)
    # Back up to the outside 90: a raise, woken as any apply; then nothing
    assert p.calls == [("wake", VIN, None), ("setChargeLimit", VIN, 90)]
    assert m.heldLimits.get(VIN) is None and m.desired.get(VIN, CHARGE_LIMIT) is None
    m, v, c = placed()
    p = scriptedBle(m)
    v.observe("chargeLimit", 80, TM)
    m.heldLimits.set(VIN, 60, 80)
    m.setPolicyLimit(None, transition=False)
    m.reconciler.pass_()
    assert p.calls == [
        ("wake", VIN, None),
        ("setChargeLimit", VIN, 60),
    ]  # an unknown battery level is expected to charge: it wakes


def test_arrival_is_one_decision_for_the_plugged_car_only():
    m, v, c = placed()
    scriptedBle(m)
    other = m.vehicleRegistry.getOrCreate(VIN2, "t")
    other.claim("TeslaBLE", True, "paired")
    placeAt(m, other)
    m.policyLimit = 70
    later(c)
    v.observe("chargingState", "Charging", TM)  # the plug-in event
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 70
    assert m.desired.get(VIN2, CHARGE_LIMIT) is None  # only the plugged car
    m.desired.adopt(VIN, CHARGE_LIMIT, 85)
    later(c)
    v.observe("chargingState", "Charging", TM)  # the same session: no new event
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 85
    later(c)
    other.observe("fastChargerPresent", True, TM)
    other.observe("chargingState", "Charging", TM)  # a fast charger near home
    assert m.desired.get(VIN2, CHARGE_LIMIT) is None


def test_power_start_still_queues_check_charge():
    from tests.unit.gen2_helpers import drive
    from tests.unit.test_registry_callers import _handler

    m, r, v, c = home()
    s = m.getSlaveTWCs()[0]
    queued = []
    m.queue_background_task = lambda task, delay=0: queued.append(task["cmd"])
    drive(m, s, 0.0)
    drive(m, s, 10.0)  # power starts flowing
    assert "checkCharge" in queued
    for path in ("/api/checkArrival", "/api/checkDeparture"):
        request = Mock()
        request.url = urllib.parse.urlparse(path)
        request.post_data = b"{}"
        _handler(m).do_API_POST.__func__(request)
        request.send_response.assert_called_with(404)


def test_endpoints_list_cars_plugged_here_whatever_module_knows_them():
    from TWCManager.EVSEInstance.VehicleEndpoint import vehicleEndpoints

    m, v, c = placed()
    m.providerOrder.register(
        ScriptedProvider("TeslaBLE", 2, True, [], readyFor=lambda v, cap: True)
    )
    v.claim("TeslaBLE", True, "paired")  # known to BLE alone
    idle = m.vehicleRegistry.getOrCreate(VIN2, "t")
    idle.claim("TeslaBLE", True, "paired")
    placeAt(m, idle)
    assert vehicleEndpoints(m) == []  # home, nothing plugged
    later(c)
    v.observe("chargingState", "Stopped", "TeslaBLE")
    assert [e.currentVIN for e in vehicleEndpoints(m)] == [VIN]


def test_anonymous_00_edge_car_is_never_also_an_endpoint():
    from TWCManager.EVSEInstance.VehicleEndpoint import vehicleEndpoints

    m, v, c = placed()
    m.providerOrder.register(
        ScriptedProvider("TeslaBLE", 2, True, [], readyFor=lambda v, cap: True)
    )
    anon = m.vehicleRegistry.beginPlugSession("8244")  # the point at 00
    v.observe("state", "online", TM)
    v.observe("pluggedIn", True, TM)  # live telemetry: plugged in
    assert P.pluggedHere(m, v) and P.telemetryPlug(v)
    keys = [e.vehicleKey for e in vehicleEndpoints(m)]
    assert anon.key not in keys and VIN not in keys  # it may be the 00 car


def test_a_ble_fix_teaches_home_and_a_fleet_fix_alone_does_not():
    m, r, v, c = setup(here=False)
    m.queue_background_task = Mock()
    v.observe("lat", AWAY[0], "FleetAPI")
    v.observe("lon", AWAY[1], "FleetAPI")
    assert m.getHomeLatLon()[0] == 10000  # a cloud fix may be anywhere
    later(c)
    v.observe("lat", HOME[0], "TeslaBLE")
    v.observe("lon", HOME[1], "TeslaBLE")  # BLE answers only in range
    assert m.getHomeLatLon() == list(HOME)


def test_an_unanswered_ping_is_absence_only_when_the_range_scan_misses():
    from tests.unit.test_teslable_pairing_status import VIN as BVIN

    b, reg, master = scanner()
    reg.onObserve = functools.partial(P.noteObservation, master)
    v = reg.getOrCreate(BVIN, "t")
    b._tool = Mock(return_value=(None, None, None))  # timed out
    b.scanForVehicleInRange = Mock(return_value=None)  # no scan possible
    assert b.ping(BVIN) is False and v.markAt("absent") == P.NEVER
    b.scanForVehicleInRange.return_value = False  # the scan ran and missed it
    assert b.ping(BVIN) is False and v.markAt("absent") > P.NEVER
    b._tool.return_value = (b"{}", b"", 0)
    assert b.ping(BVIN) is True and v.markAt("contact") > P.NEVER
    b._tool.return_value = None  # no key: nothing ran, no scan
    calls = b.scanForVehicleInRange.call_count
    assert b.ping(BVIN) is False and b.scanForVehicleInRange.call_count == calls


def test_home_is_learned_from_the_triggering_sources_own_fix():
    m, r, v, c = setup(here=False)
    m.queue_background_task = Mock()
    v.observe("lon", HOME[1] + 1, "FleetAPI")  # another source, elsewhere
    later(c)
    v.observe("lat", HOME[0], "TeslaBLE")
    assert m.getHomeLatLon()[0] == 10000  # half a BLE fix: never a mixed one
    v.observe("lon", HOME[1], "TeslaBLE")
    assert m.getHomeLatLon() == list(HOME)


# A Gen2 status edge checks the car: here through a live feed, the cheapest
# answer. It departs the car the TWC names, else the only car it may serve


def onGen2(monkeypatch, m, v, c, named=True):
    # Linked at 03 with a car's 32 A
    from tests.unit.test_gen2_unplug import beats

    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    beats(m, s, c, 10, 0x03, 32.0)
    if named:
        m.vehicleRegistry.observeTwcVin("8244", v.vin)
    beats(m, s, c, 400, 0x03, 32.0)
    return s, lambda *a, **k: beats(m, s, c, *a, **k)


def fed(v, plugged=True):
    # TeslaMate live, reporting the plug
    v.observe("state", "online", TM)
    v.observe("pluggedIn", plugged, TM)


def test_a_feed_report_after_the_edge_keeps_the_car_through_a_twc_00(
    monkeypatch, caplog
):
    m, v, c = placed()
    fed(v)
    s, beat = onGen2(monkeypatch, m, v, c)
    with caplog.at_level(logging.INFO):
        beat(10, 0x00, 0.0)
        v.observe("pluggedIn", True, TM)
        beat(110, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"
    assert "TWC 8244: plugged in: {'%s': True}" % VIN in caplog.text


def test_a_feed_report_after_the_edge_keeps_the_car_through_a_max_drop_in_00(
    monkeypatch,
):
    m, v, c = placed()
    fed(v)
    s, beat = onGen2(monkeypatch, m, v, c)
    beat(49, 0x00, 47.0, step=7)  # woken by the app
    beat(10, 0x00, 0.0)
    v.observe("pluggedIn", True, TM)
    beat(110, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"


def test_a_car_its_twc_still_holds_at_00_is_offered_the_twcs_nothing(monkeypatch):
    from TWCManager.Vehicle.Reconcile import Belief

    m, v, c = placed()
    s, beat = onGen2(monkeypatch, m, v, c)
    beat(120, 0x00, 0.0)  # checked: no answer, so the car stays
    m.distributeEVSEPower()
    assert s.vehicleKey is None and m.offered[VIN] == 0
    assert Belief.whyQuiet(m, v) == "nothing offered"


@pytest.mark.parametrize("move", ["driveStart", "outside"])
def test_a_twc_at_00_stops_holding_a_car_that_moved_since_it_was_named(
    monkeypatch, move
):
    m, v, c = placed()
    s, beat = onGen2(monkeypatch, m, v, c)
    beat(120, 0x00, 0.0)
    assert s.heldKey == VIN
    later(c)
    v.mark(move, c.t)
    assert m.vehicleRegistry.vehicleAt(s.twcid) is v and s.heldKey is None


def test_a_feed_reading_older_than_the_edge_is_no_answer(monkeypatch, caplog):
    # A live feed silent since the edge says nothing about it: nothing else
    # can check the car, so it stays
    m, v, c = placed()
    fed(v, False)
    s, beat = onGen2(monkeypatch, m, v, c)
    with caplog.at_level(logging.INFO):
        beat(120, 0x00, 0.0)
    assert v.value("twcVin") == "8244" and v.markAt("unplug") == P.NEVER
    assert "TWC 8244: plugged in: {'%s': None}" % VIN in caplog.text


def test_a_retained_redelivery_inside_the_wait_is_no_answer(monkeypatch, caplog):
    # A reconnect redelivers the feed's last report, stamped now: it is no
    # report of this edge, and nothing else can check the car
    m, v, c = placed()
    fed(v)
    s, beat = onGen2(monkeypatch, m, v, c)
    with caplog.at_level(logging.INFO):
        beat(10, 0x00, 0.0)
        v.observe("pluggedIn", False, TM, seen=False)
        beat(110, 0x00, 0.0)
    assert v.value("twcVin") == "8244"
    assert "TWC 8244: plugged in: {'%s': None}" % VIN in caplog.text


def test_a_retained_unplugged_redelivery_marks_no_unplug_and_restores_nothing():
    m, v, c = placed()
    m.heldLimits.set(VIN, 90, 50)
    fed(v)
    later(c)
    v.observe("pluggedIn", False, TM, seen=False)  # a reconnect's retained copy
    m.setPolicyLimit(50, transition=False)
    assert v.markAt("unplug") == P.NEVER and P.departed(m, v) is None
    assert m.desired.get(VIN, CHARGE_LIMIT) is None


def test_a_live_unplug_after_a_retained_copy_is_marked_once_at_the_report():
    m, v, c = placed()
    fed(v)
    later(c)
    v.observe("pluggedIn", False, TM, seen=False)  # a reconnect's retained copy
    later(c)
    v.observe("pluggedIn", False, TM)  # the feed's live report
    assert v.markAt("unplug") == c.t
    later(c)
    v.observe("pluggedIn", False, TM)  # reported again: no new unplug
    assert v.markAt("unplug") == c.t - 10


def test_after_a_restart_a_live_unplug_after_a_retained_plugged_copy_is_marked():
    m, v, c = placed()
    v.observe("state", "online", TM)
    v.observe("pluggedIn", True, TM, seen=False)  # the reconnect's retained copy
    later(c)
    v.observe("pluggedIn", False, TM)
    assert v.markAt("unplug") == c.t


def test_after_a_restart_a_live_plugged_report_after_a_retained_copy_is_no_event(
    monkeypatch,
):
    import TWCManager.Vehicle.Reconcile.Targets as targets

    m, v, c = placed()
    arrivals = []
    monkeypatch.setattr(targets, "onArrival", lambda m, v: arrivals.append(v))
    m.attributePlugs = Mock()
    v.observe("state", "online", TM)
    v.observe("pluggedIn", True, TM, seen=False)
    later(c)
    life = v.markAt("lifeAt")
    v.observe("pluggedIn", True, TM)
    assert v.markAt("plugIn") == P.NEVER and v.markAt("lifeAt") == life
    assert arrivals == [] and not m.attributePlugs.called


def test_a_feed_report_after_the_edge_keeps_an_unnamed_session(monkeypatch, caplog):
    m, v, c = placed()
    v.observe("state", "online", TM)
    s, beat = onGen2(monkeypatch, m, v, c, named=False)
    with caplog.at_level(logging.INFO):
        beat(10, 0x00, 0.0)
        v.observe("pluggedIn", True, TM)
        beat(110, 0x00, 0.0)
    assert m.vehicleRegistry.vehicleAt("8244").isAnonymous
    assert v.markAt("unplug") == P.NEVER
    assert "TWC 8244: plugged in: {'%s': True}" % VIN in caplog.text


def test_charge_stop_keeps_twc_vin_until_the_unplug(monkeypatch):
    m, v, c = placed()
    fed(v)
    s, beat = onGen2(monkeypatch, m, v, c)
    beat(300, 0x01, 32.0, actual=10.0)
    beat(30, 0x03, 32.0)  # the charge stops
    assert s.isCharging == 0 and v.value("twcVin") == "8244"
    assert P.presence(m, v) == P.HOME
    beat(10, 0x00, 0.0)
    v.observe("pluggedIn", False, TM)
    beat(20, 0x00, 0.0)
    assert v.value("twcVin") is None and P.presence(m, v) == P.NOT_HOME


def test_vehicle_key_names_the_car_after_a_charge_stop_and_nobody_after_an_unplug(
    monkeypatch,
):
    m, v, c = placed()
    fed(v)
    s, beat = onGen2(monkeypatch, m, v, c)
    beat(300, 0x01, 32.0, actual=10.0)  # this charge's VIN read never lands
    beat(30, 0x03, 32.0)
    assert m.vehicleRegistry.get("anon:8244") is not None and s.vehicleKey == VIN
    beat(70, 0x00, 47.0)  # the car wakes: still plugged in, whatever the session
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"
    beat(70, 0x03, 32.0)
    beat(10, 0x00, 0.0)
    v.observe("pluggedIn", False, TM)
    beat(60, 0x00, 0.0)
    assert s.vehicleKey is None and v.value("twcVin") is None
    beat(10, 0x03, 32.0)  # another car: unnamed until its VIN is read
    assert s.vehicleKey == "anon:8244"


def test_a_vinless_unplug_departs_the_only_candidate(monkeypatch):
    m, v, c = placed()
    fed(v)
    s, beat = onGen2(monkeypatch, m, v, c, named=False)
    assert s.vehicleKey == "anon:8244" and P.candidates(m) == [v]
    beat(10, 0x00, 0.0)
    v.observe("pluggedIn", False, TM)
    beat(20, 0x00, 0.0)
    assert v.markAt("unplug") > P.NEVER and P.presence(m, v) == P.NOT_HOME
    assert m.vehicleRegistry.get("anon:8244") is None


def test_a_non_tesla_plug_departs_nobody(monkeypatch):
    m, v, c = placed(AWAY)  # the account's car is elsewhere
    s, beat = onGen2(monkeypatch, m, v, c, named=False)
    assert P.candidates(m) == []
    beat(70, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER
    assert m.vehicleRegistry.get("anon:8244") is None


def test_restart_steady_00_with_telemetry_plugged_is_home_and_restores_nothing(
    monkeypatch,
):
    from tests.unit.test_gen2_unplug import beats

    m, v, c = placed()
    m.heldLimits.set(VIN, 90, 50)
    v.observe("chargeLimit", 50, TM)
    p = scriptedBle(m)
    later(c)
    v.observe("state", "online", TM)
    v.observe("pluggedIn", True, TM)
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    for _ in range(30):  # 00 from the first reply after the restart
        beats(m, s, c, 30, 0x00, 0.0)
        m.setPolicyLimit(50, transition=False)
        m.reconciler.pass_()
    assert P.presence(m, v) == P.HOME and v.markAt("unplug") == P.NEVER
    assert p.calls == [] and m.heldLimits.get(VIN) == HeldLimit(90, 50)


def test_a_charging_point_s_own_start_always_applies():
    from TWCManager.Vehicle.Reconcile.Desired import CHARGING

    m, v, c = placed(AWAY)  # wherever the car is said to be, the point is here
    point = ScriptedProvider("Gen3TWC", 1, True, [O.Outcome(O.DONE)], module=None)
    m.providerOrder.register(point)
    m.desired.set(VIN, CHARGING, True, "distributor")
    m.reconciler.pass_()
    assert [x[0] for x in point.calls if x[0] != "wake"] == ["startCharging"]


def test_a_car_a_gen2_names_at_00_is_plugged_for_a_start_until_it_leaves():
    from TWCManager.Vehicle.Reconcile.Desired import CHARGING

    m, v, c = placed()
    p = scriptedBle(m)
    twcNames(m, v, c, state=0x00)  # a sleeping car: 00, still named
    later(c)
    v.observe("bleReach", True, "TeslaBLE")
    assert P.presence(m, v) == P.HOME and not P.pluggedNow(m, v)
    m.desired.set(VIN, CHARGING, True, "distributor")
    m.reconciler.pass_()
    assert [x[0] for x in p.calls if x[0] != "wake"] == ["startCharging"]
    later(c)
    v.observe("pluggedIn", False, "Gen2TWC")  # the checked unplug
    assert P.applicable(m, v, C.START_CHARGING) == (P.NOT_HERE, "away")


def test_an_unplugged_reading_after_the_naming_outweighs_it_and_an_older_one_does_not():
    m, v, c = placed()
    later(c)
    v.observe("chargingState", "Disconnected", "FleetAPI")  # before the plug-in
    twcNames(m, v, c, state=0x00)
    assert P.applicable(m, v, C.START_CHARGING) == (P.SEND, "plugged in")
    later(c)
    v.observe("chargingState", "Disconnected", "FleetAPI")  # read after it
    assert P.applicable(m, v, C.START_CHARGING) == (P.NOT_HERE, "not plugged in")


# One attribution names a VIN-less plug's car, for Gen2 and Gen3


def test_attribution_reads_only_local_providers(requests_mock):
    from tests.unit.test_command_path_parity import (
        VIN2 as TWO,
        chargeRead,
        install,
        realBle,
    )

    m, r, v, c, api = install(fleet=True, owner=True)
    api.addVehicle({"id": 2, "vin": TWO, "display_name": "Two"})
    placeAt(m, m.vehicleRegistry.get(TWO))  # both home: a fix can't tell them
    b = realBle(m)
    asked = []
    read = chargeRead(**{VIN: ("Charging", 32)})
    b._fetch_state_json = lambda vin, kind: asked.append((vin, kind)) or read(vin, kind)
    m.vehicleRegistry.beginPlugSession("8244")
    assert P.attribute(m, "8244", "Gen2TWC", 32.0, 0.0) == VIN
    assert asked == [(VIN, "charge")] and requests_mock.call_count == 0


def test_a_single_candidate_inside_is_attributed_merges_the_session_and_arrives_once(
    monkeypatch,
):
    from TWCManager.Vehicle.Reconcile import Targets
    from tests.unit.test_command_path_parity import attributeQueued
    from tests.unit.test_gen2_unplug import beats
    from tests.unit.test_gen3twc_provider import make as gen3, vitals

    arrivals = []
    monkeypatch.setattr(Targets, "onArrival", lambda m, v: arrivals.append(v.key))
    m, v, c = placed()
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    beats(m, s, c, 20, 0x03, 32.0)  # a Gen2 that reads no VIN
    assert m.vehicleRegistry.get("anon:8244").latest("actualCurrent") is not None
    attributeQueued(m)
    assert m.vehicleRegistry.get("anon:8244") is None and s.vehicleKey == VIN
    assert v.latest("actualCurrent").source == "Gen2TWC"  # the session's readings
    beats(m, s, c, 300, 0x03, 32.0)
    attributeQueued(m)
    assert arrivals == [VIN] and P.pluggedHere(m, v)
    g, ctl, gp = gen3()  # and a Gen3: its own plug reading is the arrival
    m.vehicleRegistry.endPlugSession("8244")
    (u,) = ctl.devices()
    u.master = m
    vitals(u, True)
    vitals(u, True)
    assert u.vehicleKey == VIN and arrivals == [VIN, VIN]


def test_a_car_lost_to_ble_is_named_on_its_plug_edge_by_one_scan(monkeypatch):
    # Last seen away and out of BLE range, the car comes home and plugs into
    # a Gen2 that reads no VIN: no candidate, so one scan of every car hears
    # it, and the same plug edge names it
    from tests.unit.test_command_path_parity import attributeQueued, realBle
    from tests.unit.test_gen2_unplug import beats

    m, v, c = placed(AWAY)
    monkeypatch.setattr(time, "time", c)
    b = realBle(m)
    b.isDockerCached, scans = False, []
    m.getModuleByName = lambda name: b if name == "TeslaBLE" else None

    def bluetoothctl(cmd, timeout, use_process_group=True):
        if cmd[-1] == "show":  # the scan's readiness check: a powered adapter
            return (b"Controller 00:1A:7D:DA:71:13 (public)\n\tPowered: yes\n", b"", 0)
        scans.append(cmd[-2:])
        return (
            b"[NEW] Device AA:BB:CC:DD:EE:FF " + b.bleLocalName(VIN).encode(),
            b"",
            0,
        )

    b._run_command_with_timeout = bluetoothctl
    later(c, 3600)
    assert P.presence(m, v) == P.NOT_HOME and P.candidates(m) == []
    s = m.getSlaveTWCs()[0]
    with patch("TWCManager.Vehicle.TeslaBLE.shutil.which", return_value="/bin/true"):
        beats(m, s, c, 20, 0x03, 32.0)
        attributeQueued(m)
        assert scans == [["scan", "on"]] and s.vehicleKey == VIN
        assert m.vehicleRegistry.get("anon:8244") is None
        assert P.presence(m, v) == P.HOME
        attributeQueued(m)  # the contact's own re-attempt finds it named
        assert scans == [["scan", "on"]]


def heard(m, v):
    v.observe("bleReach", True, "TeslaBLE")  # any scan or exchange


def cloudPlugAtHome(m, v):
    placeAt(m, v, HOME)
    v.observe("chargingState", "Charging", "FleetAPI")


@pytest.mark.parametrize("news", [heard, cloudPlugAtHome])
def test_an_unnamed_session_drawing_current_is_named_on_news_of_its_car(
    monkeypatch, news
):
    # The plug-in's scan missed the car; while the session still draws
    # current, later contact or a plug report names it, scanning no more
    from tests.unit.test_command_path_parity import attributeQueued, realBle
    from tests.unit.test_gen2_unplug import beats

    m, v, c = placed(AWAY)
    monkeypatch.setattr(time, "time", c)
    b = realBle(m)
    b.scanForVehicleInRange = Mock(return_value=False)
    m.getModuleByName = lambda name: b if name == "TeslaBLE" else None
    later(c, 3600)
    s = m.getSlaveTWCs()[0]
    beats(m, s, c, 20, 0x03, 32.0)
    attributeQueued(m)
    later(c, 300)
    s.reportedAmpsActual = 16.0  # charging, still unnamed
    m.attributePlugs()
    attributeQueued(m)
    assert b.scanForVehicleInRange.call_count == 1 and s.vehicleKey == "anon:8244"
    news(m, v)
    attributeQueued(m)
    assert s.vehicleKey == VIN and b.scanForVehicleInRange.call_count == 1


def test_two_candidates_attributed_by_ble_pilot():
    m, r, v, c = home()
    two = m.vehicleRegistry.getOrCreate(VIN2, "t")
    for car, pilot in ((v, 16), (two, 32)):
        placeAt(m, car)
        car.claim("TeslaBLE", True, "paired")
        later(c)
        car.observe("chargingState", "Charging", "TeslaBLE")
        car.observe("availableCurrent", pilot, "TeslaBLE")
    m.vehicleRegistry.beginPlugSession("8244")
    assert P.attribute(m, "8244", "Gen2TWC", 32.0, 0.0) == VIN2
    assert two.value("twcVin") == "8244" and v.value("twcVin") is None


def test_live_telemetry_unplugged_is_never_attributed_and_outranks_a_naming():
    m, v, c = placed()
    m.getSlaveTWCs()[0].reportedState = 0x03
    later(c)
    v.observe("state", "online", TM)
    v.observe("pluggedIn", False, TM)
    m.vehicleRegistry.beginPlugSession("8244")
    assert P.attribute(m, "8244", "Gen2TWC") is None and v.value("twcVin") is None
    m.vehicleRegistry.endPlugSession("8244")
    m2, v2, c2 = placed()
    m2.getSlaveTWCs()[0].reportedState = 0x03
    m2.vehicleRegistry.beginPlugSession("8244")
    assert P.attribute(m2, "8244", "Gen2TWC") == VIN and P.pluggedNow(m2, v2)
    later(c2)
    v2.observe("state", "online", TM)
    v2.observe("pluggedIn", False, TM)  # newer than the naming
    assert P.pluggedNow(m2, v2) is False and not P.pluggedHere(m2, v2)


def test_a_vinless_unplug_never_departs_a_car_another_twc_names(monkeypatch):
    m, v, c = placed()
    s, beat = onGen2(monkeypatch, m, v, c, named=False)
    other = m.newSlave(b"\x82\x45", 80)
    m.vehicleRegistry.observeTwcVin("8245", v.vin)  # asleep at 00 on that TWC
    assert P.twcVinAt(m, v) == P.NEVER and P.candidates(m) == []
    beat(70, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8245"
    assert other.reportedState == 0x00


def test_a_gen3_plug_in_never_reads_ble(requests_mock):
    # The vitals refresh must not wait on the radio: with no offer to
    # compare a pilot to, a Gen3 plug-in is attributed from what is known
    from tests.unit.test_command_path_parity import VIN2 as TWO, install, realBle
    from tests.unit.test_gen3twc_provider import make as gen3, vitals

    m, r, v, c, api = install(fleet=True)  # at home; no plug reading at all
    b = realBle(m, vins=(VIN, TWO))
    fetched = []
    b._fetch_state_json = lambda vin, kind: fetched.append(vin)
    g, ctl, gp = gen3()
    (u,) = ctl.devices()
    u.master = m
    vitals(u, True)
    assert fetched == [] and u.vehicleKey == VIN


def test_an_attributed_gen2_plug_ends_with_the_point_s_00_even_idle(monkeypatch):
    from TWCManager.EVSEInstance.VehicleEndpoint import vehicleEndpoints
    from tests.unit.test_command_path_parity import attributeQueued

    m, v, c = placed()
    scriptedBle(m)
    s, beat = onGen2(monkeypatch, m, v, c, named=False)
    attributeQueued(m)
    beat(60, 0x03, 0.0)  # named, idle at 0 A: no 00 here is an unplug
    assert v.value("twcVin") == "8244" and P.pluggedHere(m, v)
    assert [e.vehicle for e in vehicleEndpoints(m)] == [v]
    beat(120, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and not P.pluggedHere(m, v)
    assert vehicleEndpoints(m) == []


def test_an_anonymous_point_s_offer_covers_the_cars_it_fans_out_to(monkeypatch):
    from tests.unit.test_gen2_unplug import beats

    m, v, c = placed()
    scriptedBle(m)
    v.claim("TeslaBLE", True, "paired")
    v.observe("chargingState", "Charging", TM)  # plugged in here, unnamed
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    m.setMaxAmpsToDivideAmongSlaves(0)
    beats(m, s, c, 20, 0x03, 32.0)
    m.distributeEVSEPower()
    assert s.vehicleKey == "anon:8244" and m.offered[VIN] == 0


def vinReadingGen2(requests_mock, monkeypatch, located=True):
    # The box after a restart: one car, plugged in and asleep, on a Gen2
    # that reads VINs but has read none this session. Unlocated, nothing but
    # its name is known, and the sleep-safe check answers asleep
    from tests.unit.test_command_path_parity import gen2, install, realBle

    m, r, v, c, api = install(fleet=True, located=located)
    b = realBle(m, ping=located or (lambda vin: asleepPing(m, vin)))
    b.fetched = []
    b._fetch_state_json = lambda vin, kind: b.fetched.append(vin)
    m.settings["SlaveTWCs"] = {"8244": {"supportsVINQuery": 1}}
    return m, v, c, b, lambda: gen2(monkeypatch, m, c, vin=None)


def asleepPing(m, vin):
    m.vehicleRegistry.get(vin).observe("state", "asleep", "TeslaBLE")
    return True


@pytest.mark.parametrize("limit", [50, 80])
def test_a_failing_restore_to_an_away_car_logs_no_not_here(
    requests_mock, caplog, monkeypatch, limit
):
    import TWCManager.Vehicle.TeslaBLE as teslable
    from tests.unit.test_command_path_parity import install, passes, realBle

    monkeypatch.setattr(teslable.time, "sleep", lambda s: None)
    caplog.set_level(logging.INFO)
    m, r, v, c, api = install(owner=True, fleet=True, policy="ble_only", held=(90, 50))
    b = realBle(m, answers=None)  # every restore fails
    v.observe("chargeLimit", 50, TM)
    later(c, 1)
    placeAt(m, v, AWAY)
    passes(r, c, 3 * 3600, step=60, each=lambda: m.setPolicyLimit(limit, False))
    assert len(b.runs) >= 6  # the hourly restore and its quick retry
    assert not [r for r in caplog.records if P.NOT_HERE in r.getMessage()]


def scanOnly(life):
    # The sleep-safe check answered by the range scan alone: sleep unknown
    from tests.unit.test_command_path_parity import realBle

    m, v, c = placed()
    if life:
        v.observe("state", "online", TM)  # awake an hour ago
        later(c, 3600)
    b = realBle(m, ping=lambda vin: True)
    b.fetched = []
    b._fetch_state_json = lambda vin, kind: b.fetched.append(vin)
    return m, v, c, b


@pytest.mark.parametrize("life", [False, True])
def test_an_attribution_never_reads_a_car_the_scan_alone_answered_for(life):
    m, v, c, b = scanOnly(life)
    P.pick(m, offered=32, read=True, since=c.t)
    assert b.pings == [VIN] and b.fetched == []


@pytest.mark.parametrize("life", [False, True])
def test_a_plug_check_keeps_a_car_the_scan_alone_answered_for(life):
    m, v, c, b = scanOnly(life)
    assert P.checkPlug(m, v, c.t) is True
    assert b.pings == [VIN] and b.fetched == []


def test_a_vin_reading_twc_attributes_a_sleeping_car_without_reading_it(
    requests_mock, monkeypatch
):
    from tests.unit.test_command_path_parity import attributeQueued, paid

    m, v, c, b, plug = vinReadingGen2(requests_mock, monkeypatch)
    v.observe("pluggedIn", True, TM)
    v.observe("state", "asleep", TM)
    c.t += 600  # plugged in long before the restart
    s = plug()
    attributeQueued(m)
    assert v.value("twcVin") == "8244" and s.vehicleKey == VIN
    assert b.fetched == [] and b.pings == [] and b.runs == []
    assert paid(requests_mock) == (0, 0, 0)


def test_an_unknown_sleep_state_is_checked_before_any_charge_read(
    requests_mock, monkeypatch
):
    # The check says asleep, so no charge read runs; its contact names the car
    from tests.unit.test_command_path_parity import attributeQueued, paid

    m, v, c, b, plug = vinReadingGen2(requests_mock, monkeypatch, located=False)
    s = plug()
    attributeQueued(m)
    assert b.pings == [VIN] and b.fetched == [] and b.runs == []
    assert v.value("twcVin") == "8244" and paid(requests_mock) == (0, 0, 0)


def test_the_twcs_vin_read_overrides_an_attribution(requests_mock, monkeypatch):
    from tests.unit.test_command_path_parity import attributeQueued

    m, v, c, b, plug = vinReadingGen2(requests_mock, monkeypatch)
    v.observe("pluggedIn", True, TM)
    v.observe("state", "asleep", TM)
    c.t += 600
    s = plug()
    attributeQueued(m)
    assert v.value("twcVin") == "8244"
    c.t += 10
    s.currentVIN = VIN2
    for _ in range(2):  # an unknown VIN is confirmed by a second read
        m.recordVehicleVIN(s)
    two = m.vehicleRegistry.get(VIN2)
    assert s.vehicleKey == VIN2 and two.value("twcVin") == "8244"
    assert (
        v.value("twcVin") is None and v.reported("pluggedIn")["Gen2TWC"].value is False
    )
    assert v.markAt("unplug") == c.t


def test_a_car_seen_outside_while_still_driving_has_departed(requests_mock):
    # It came back (parked inside), then left with no drive start seen inside
    # the radius: the fix outside is the departure even while it drives on
    from tests.unit.test_command_path_parity import install, push

    m, r, v, c, api = install(held=(90, 50))
    push(v, c, shiftState="D")
    push(v, c, shiftState="P")
    assert v.markAt("driveEnd") == c.t
    c.t += 600
    placed_at = c.t
    placeAt(m, v, AWAY)
    push(v, c, shiftState="D")
    assert P.decide(m, v)[0] == P.NOT_HOME and P.drivingNow(v)
    assert P.departed(m, v) == placed_at
    m.setPolicyLimit(50, transition=False)
    assert m.desired.get(VIN, CHARGE_LIMIT).restore


def cloudFix(v, at, source="FleetAPI", gpsAsOf=None):
    if gpsAsOf is not None:
        v.observe("gpsAsOf", gpsAsOf, source)
    v.observe("lat", at[0], source)
    v.observe("lon", at[1], source)


def plugAndName(m, v, c, monkeypatch, seconds=13):
    # A Gen2 plug-in, its session, and its VIN read seconds later
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    s.reportedState, s.currentVIN = 0x03, VIN
    m.vehicleRegistry.beginPlugSession("8244")
    later(c, seconds)
    m.recordVehicleVIN(s)
    return s


def test_an_away_fix_from_before_the_plug_in_never_moves_home(monkeypatch):
    # The read that found the car away, 9 minutes before it was plugged in
    # at home: still under HOME_FIX_MAX_AGE, but not where the TWC is
    m, r, v, c = home()
    m.queue_background_task = Mock()
    cloudFix(v, AWAY)
    later(c, 540)
    plugAndName(m, v, c, monkeypatch)
    assert m.getHomeLatLon() == list(HOME) and P.presence(m, v) == P.HOME


def test_a_ble_fix_from_before_the_plug_in_still_teaches_home(monkeypatch):
    # BLE answers only in range: its fix keeps the age rule alone
    m, r, v, c = home()
    m.queue_background_task = Mock()
    plugAndName(m, v, c, monkeypatch)
    later(c)
    assert bleFix(m, v, AWAY, c.t - 480)
    assert m.getHomeLatLon() == list(AWAY)


def test_a_read_after_the_plug_in_teaches_home_whatever_its_gps_time(monkeypatch):
    # No home yet. Read 30 s after the plug-in, its GPS time 3 minutes
    # before it: the read shows where the car is now
    m, r, v, c = setup(here=False)
    m.queue_background_task = Mock()
    plugged = c.t
    plugAndName(m, v, c, monkeypatch)
    later(c, 30 - 13)
    cloudFix(v, AWAY, gpsAsOf=plugged - 180)
    assert m.getHomeLatLon() == list(AWAY) and P.presence(m, v) == P.HOME


def test_a_ble_fix_read_before_the_vin_read_still_moves_home_at_it(monkeypatch):
    # BLE answers only within range, so its fix needs no read after the
    # plug-in: a wrong home moves to it when the TWC reads the VIN
    m, r, v, c = home()
    m.queue_background_task = Mock()
    assert bleFix(m, v, HOME, c.t - 5)
    m.settings.update(homeLat=AWAY[0], homeLon=AWAY[1])
    later(c, 60)
    plugAndName(m, v, c, monkeypatch)
    assert m.getHomeLatLon() == list(HOME) and P.presence(m, v) == P.HOME

    v.observe("pluggedIn", True, TM)
    later(c, 86400)
    cloudFix(v, AWAY)
    assert v.markAt("outside") == c.t
    assert P.decide(m, v)[:2] == (P.NOT_HOME, "a fix outside the radius")


def test_a_closed_charge_port_while_awake_marks_an_unplug():
    m, v, c = placed()
    v.observe("pluggedIn", True, "TeslaBLE")  # plugged, strong
    later(c)
    v.observe("chargePortOpen", False, "TeslaBLE")
    assert v.markAt("unplug") == c.t and P.pluggedNow(m, v) is False


def test_an_open_charge_port_is_not_plug_evidence():
    m, v, c = placed()
    v.observe("chargePortOpen", True, "TeslaBLE")
    assert v.markAt("plugIn") == P.NEVER and P.pluggedNow(m, v, None) is None


def test_check_plug_answers_from_an_awake_closed_door_without_a_read():
    m, r, v, c = home(ble())
    v.observe("pluggedIn", True, "TeslaBLE")
    later(c)

    def ping(vin, scan=True):
        v.observe("state", "online", "TeslaBLE")
        v.observe("chargePortOpen", False, "TeslaBLE")
        return True

    m.providerOrder.provider("TeslaBLE").module = SimpleNamespace(ping=ping)
    reads = []
    v.read = lambda *a, **k: reads.append(a)
    assert P.checkPlug(m, v, c.t - 1) is False and reads == []


def test_only_a_keyed_cars_unanswered_ping_falls_back_to_a_range_scan():
    m, r, v, c = home(ble())
    placeAt(m, v)
    scans = []

    def ping(vin, scan=True):
        scans.append(scan)
        return False if scan else None

    m.providerOrder.provider("TeslaBLE").module = SimpleNamespace(ping=ping)
    v.read = lambda *a, **k: None
    P.checkPlug(m, v, c.t)
    P.pick(m, 32, 0, True, c.t)
    assert scans == [False, False]
    v.claim("TeslaBLE", True, "paired")
    P.checkPlug(m, v, c.t)
    P.pick(m, 32, 0, True, c.t)
    assert scans == [False, False, True, True]
