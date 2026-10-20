# tests/unit/test_presence.py: presence from plug, drive, position and BLE
# events, on a real master and registry with a test clock. Every event gets
# its own time, so no rule is decided by a tie unless the test says so.
import functools
import logging
import re
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from TWCManager.Vehicle import Presence as P
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT
from TWCManager.Vehicle.Registry.Fields import Observation
from tests.unit.reconcile_helpers import AWAY, HOME, ScriptedProvider, placeAt
from tests.unit.teslatokens import FLEET_NA_URL, fleet_token, new_api
from tests.unit.test_limit_reconciler import VIN, ble, prov, setup
from tests.unit.test_teslable_pairing_status import scanner

VIN2, VIN3, VIN4 = "5YJ3E1EA1KF000002", "5YJ3E1EA1KF000003", "5YJ3E1EA1KF000004"
TM = "TeslaMateVehicle"


def home(*providers):
    m, r, v, c = setup(*providers)
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
    assert P.presence(m, v) == P.PRESUMED and P.homeForLimits(m, v)
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
    m, r, v, c = setup()
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
    api.lastChargeCheck = c.t
    with patch("time.time", return_value=c.t):
        assert api.numCarsAtHome == 2 and api.minBatteryLevelAtHome == 40
    assert [x.vin for x, _ in P.homeCars(m, "TeslaAPI")] == [VIN, VIN2]


def test_home_is_learned_only_from_a_twc_named_car_with_a_fresh_fix():
    m, r, v, c = setup()
    m.queue_background_task = Mock()
    assert P.learnHome(m, v) is False  # no fix
    placeAt(m, v, AWAY)
    m.settings.update(homeLat=10000, homeLon=10000)
    later(c, P.HOME_FIX_MAX_AGE + 1)
    assert P.learnHome(m, v) is False  # stale
    placeAt(m, v, AWAY)
    m.settings.update(homeLat=10000, homeLon=10000)
    assert P.learnHome(m, v) is True and m.getHomeLatLon() == list(AWAY)
    assert P.learnHome(m, v) is False  # already inside
    m.queue_background_task.assert_any_call({"cmd": "saveSettings"})


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


def test_update_charge_at_home_reads_only_home_cars(requests_mock):
    m, r, v, c = setup(clock=False)
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    api = fleetCars(requests_mock, m)
    placeAt(m, v, HOME)  # VIN2: presumed
    api.updateChargeAtHome()
    read = [q.url for q in requests_mock.request_history if "/vehicle_data" in q.url]
    assert [url.split("/")[-2] for url in read] == [VIN]


def test_away_cars_keep_the_status_deferral_ladder():
    m, r, v, c = setup(clock=False)
    api = new_api(master=m)
    api.addVehicle({"id": 1, "vin": VIN, "display_name": None})
    car = v.attachment("TeslaAPI")
    api.slotFor = Mock(return_value=Mock())
    near = (HOME[0] + 0.5, HOME[1])  # outside the radius, within an hour's drive
    far = (HOME[0] + 2, HOME[1])
    for at, shift, wait in (
        (AWAY, "P", 300),  # home not set: never deferred as away
        (HOME, "P", 300),
        (far, "P", 3600),
        (near, "P", 1800),
        (near, "D", 300),
    ):
        drive = {"latitude": at[0], "longitude": at[1], "shift_state": shift}
        api.get_car_api = Mock(
            return_value=(True, {"drive_state": drive, "charge_state": {}})
        )
        car.statusDeferral = 0
        assert api.update_vehicle_data(v) is True
        assert round(car.statusDeferral - time.time()) == wait
        m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    assert v.value("shiftState") == "D"


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
    assert prov(m, "TeslaBLE").calls == [("setChargeLimit", VIN, 70)]
    assert requests_mock.call_count == 0
