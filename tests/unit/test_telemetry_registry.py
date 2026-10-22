# tests/unit/test_telemetry_registry.py
import logging
import time
from unittest.mock import Mock
from TWCManager.Vehicle.Registry.Fields import READ_MAX_AGE

import pytest

from tests.unit.teslatokens import FakeMaster, new_api, owner_token, wire_reads
from TWCManager.Vehicle.Presence import telemetryPlug
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.Telemetry import TelmetryBase
from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle
from TWCManager.Vehicle.FleetTelemetryMQTT import FleetTelemetryMQTT

VIN = "5YJ3E1EA1KF000001"


class Probe(TelmetryBase):
    configName = "Probe"
    unmappedRetrySecs = 3600
    events = {"charge_limit_soc": ["chargeLimit", int], "latitude": ["lat", float]}

    def scheduleUnmappedRetry(self, delay=None):
        self.scheduled = True


def make(cls=Probe, carapi=None, vins=None, clock=None):
    reg = VehicleRegistry({}, **({"clock": clock} if clock else {}))
    master = Mock()
    master.vehicleRegistry = reg
    master.config = {"config": {}, "vehicle": {cls.configName: {"enabled": True}}}
    master.getModuleByName = Mock(return_value=carapi)
    t = cls(master)
    if vins is not None:
        t.carVINs = vins
    return t, reg


def test_vin_keyed_telemetry_creates_registry_vehicle_and_observes_with_source():
    t, reg = make()
    t.vinFor = lambda vid: VIN
    t.applyDataToVehicle("1", "charge_limit_soc", "80")  # before the name: parked
    assert reg.vehicles() == [] and t.unknownVehicles["1"] == [
        ["charge_limit_soc", "80"]
    ]
    t.applyDataToVehicle("1", "display_name", "TestCar")  # maps and replays at once
    v = reg.get(VIN)
    assert v.name == "TestCar" and t.vehicles["1"] is v
    assert (v.latest("chargeLimit").value, v.latest("chargeLimit").source) == (
        80,
        "Probe",
    )


def test_name_match_against_registry_without_cloud_list():
    t, reg = make()
    reg.getOrCreate(VIN, "TeslaBLE").observe("name", "TestCar", "OwnerAPI")
    assert t.updateVehicles("1", "TestCar", loadList=False) is True
    assert t.vehicles["1"] is reg.get(VIN)


def test_unidentifiable_car_is_parked_and_never_raises():  # Review Focus 4
    t, reg = make()
    t.applyDataToVehicle("1", "charge_limit_soc", "80")
    t.applyDataToVehicle("1", "display_name", "Ghost")
    assert (
        "1" not in t.vehicles
        and t.unknownVehicles["1"] == [["charge_limit_soc", "80"]]
        and t.scheduled
    )
    assert reg.vehicles() == []


def test_task9_legacy_sync_names_are_gone():
    from TWCManager.Vehicle.TeslaAPI import CarApiState

    assert not any(
        hasattr(CarApiState, n)
        for n in ("syncState", "syncLat", "syncLon", "_legacySyncSource")
    )


def test_connection_state_is_the_feed_liveness():
    t, reg = make()
    t.vinFor = lambda vid: VIN
    t.applyDataToVehicle("1", "display_name", "TestCar")
    v = reg.get(VIN)
    v.observe("chargeLimit", 50, "Probe", at=0.0)
    assert reg.sourceLiveAt("Probe") is None and v.age("chargeLimit") > 1e6
    t.mqttConnect(None, None, None, 0)
    assert v.age("chargeLimit") < 1.0  # healthy feed: value is current
    t.mqttDisconnect(None, None, None, 7)
    assert reg.sourceLiveAt("Probe", v) is None


def test_subclass_connects_set_liveness_and_teslamate_healthy_gates_it(
    monkeypatch,
):  # P8
    monkeypatch.setattr(TeslaMateVehicle, "loadCarVINs", lambda self: {"7": VIN})
    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    t, reg = make(TeslaMateVehicle)
    client = Mock()
    t.mqttConnect(client, None, None, 0)
    assert t.connected and any(
        "healthy" in c.args[0] for c in client.subscribe.call_args_list
    )
    t.applyDataToVehicle("7", "display_name", "C")
    v = reg.get(VIN)
    assert reg.sourceLiveAt("TeslaMateVehicle", v) is not None
    t.noteHealthy("7", "false")
    assert reg.sourceLiveAt("TeslaMateVehicle", v) is None
    t.noteHealthy("7", "true")
    assert reg.sourceLiveAt("TeslaMateVehicle", v) is not None
    f, _ = make(FleetTelemetryMQTT)
    f.mqtt_prefix = "telemetry"
    f.client = Mock()
    f.mqttConnect(f.client, None, None, 0)
    assert f.connected


def test_own_write_then_silent_healthy_feed_is_trusted_after_feed_lag():
    # 09:17 shape, end to end
    from TWCManager.Vehicle.Registry.Fields import FEED_LAG

    now = {"t": 1000.0}
    t, reg = make(clock=lambda: now["t"])
    t.vinFor = lambda vid: VIN
    t.applyDataToVehicle("1", "display_name", "C")
    v = reg.get(VIN)
    t.mqttConnect(None, None, None, 0)
    t.applyDataToVehicle(
        "1", "charge_limit_soc", "50"
    )  # TeslaMate value from before our write
    v.noteWrite("chargeLimit", 100, "TeslaBLE")  # we write 100 at t
    now["t"] = 1003.0
    assert v.trusted("chargeLimit") is None  # t+3 s: not trusted, apply uses 100
    now["t"] = 1000.0 + FEED_LAG + 1
    assert (
        v.trusted("chargeLimit").value == 50
    )  # healthy feed, no change event: write did not land
    t.mqttDisconnect(None, None, None, 7)
    assert v.trusted("chargeLimit") is None  # feed down: silence proves nothing


def test_teslamate_maps_by_db_vin(monkeypatch):
    monkeypatch.setattr(TeslaMateVehicle, "loadCarVINs", lambda self: {"7": VIN})
    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    t, reg = make(TeslaMateVehicle)
    assert t.vinFor("7") == VIN and t.vinFor("8") is None
    assert (
        TeslaMateVehicle.events["latitude"][0] == "lat"
        and TeslaMateVehicle.events["state"][0] == "state"
    )


def test_fleet_telemetry_topic_id_is_the_vin():
    t, reg = make(FleetTelemetryMQTT)
    assert t.vinFor(VIN) == VIN and FleetTelemetryMQTT.events["syncState"][0] == "state"


def test_fleet_telemetry_charge_current_request_is_the_cars_request():  # slim Task 12b
    t, reg = make(FleetTelemetryMQTT)
    t.applyDataToVehicle(VIN, "ChargeCurrentRequest", "16")
    obs = reg.get(VIN).latest("chargeCurrentRequest")
    assert (obs.value, obs.source) == (16, "FleetTelemetryMQTT")
    assert reg.get(VIN).latest("availableCurrent") is None  # never the pilot


def test_fleet_telemetry_topic_id_that_is_not_a_vin_maps_to_none():  # I3
    t, reg = make(FleetTelemetryMQTT)
    assert t.vinFor("status") is None and t.vinFor("0" * 17) is None
    t.applyDataToVehicle("status", "display_name", "Ghost")
    assert reg.vehicles() == []


# Controller rulings on top of the brief.


@pytest.fixture
def teslamate(monkeypatch):
    """A real TeslaAPI car and a TeslaMate feed on one registry (real clock)."""
    monkeypatch.setattr(TeslaMateVehicle, "loadCarVINs", lambda self: {"7": VIN})
    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    master = FakeMaster()
    master.vehicleRegistry = VehicleRegistry({})
    api = new_api(master=master)
    wire_reads(master.vehicleRegistry, *api.providers())
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    # The Owner API token TeslaMate syncs: the Owner provider can read.
    api.storeToken(owner_token(), "", "synced")
    # A cloud call returns nothing usable; tests assert it is never made.
    api.get_car_api = Mock(return_value=(False, None))
    master.config["vehicle"]["TeslaMate"] = {"enabled": True}
    tm = TeslaMateVehicle(master)
    tm.mqttConnect(Mock(), None, None, 0)
    tm.applyDataToVehicle("7", "display_name", "C")
    return tm, api, master.vehicleRegistry


def feed(tm, **topics):
    for topic, payload in topics.items():
        tm.applyDataToVehicle("7", topic, payload)


def test_an_empty_charger_payload_is_no_reading(teslamate):
    tm, api, reg = teslamate
    feed(tm, charger_power="", charge_current_request_max="16.0")
    assert reg.get(VIN).value("chargerPower", 0) is None
    assert reg.get(VIN).value("chargeCurrentRequestMax") == 16.0


def test_healthy_teslamate_car_reads_without_api_calls(teslamate):  # ruling 1
    tm, api, reg = teslamate
    feed(tm, state="online", latitude="37.0", longitude="-122.0", battery_level="61")
    assert reg.get(VIN).read("lat", 0) is not None  # maxAge 0: the feed vouches for now
    reg.get(VIN).read("batteryLevel", maxAge=READ_MAX_AGE)
    assert reg.get(VIN).value("batteryLevel", 10000) == 61
    assert not api.get_car_api.called
    assert reg.get(VIN).latest("lat").source == "TeslaMateVehicle"


def test_teslamate_reports_the_cars_own_ac_maximum(teslamate):
    tm, car, reg = teslamate
    feed(tm, charge_current_request_max="48")
    assert reg.get(VIN).value("chargeCurrentRequestMax") == 48
    feed(tm, charge_current_request_max="")  # an empty payload is no reading
    assert reg.get(VIN).value("chargeCurrentRequestMax") is None


def test_feed_vouches_for_a_value_the_api_reobserved(teslamate):  # ruling 3
    tm, api, reg = teslamate
    feed(tm, state="online", latitude="37.0", longitude="-122.0")
    reg.get(VIN).observe(
        "lat", 37.0, "OwnerAPI"
    )  # the cloud saw the same position after the feed
    reg.get(VIN).observe("lon", -122.0, "OwnerAPI")
    assert reg.get(VIN).latest("lat").source == "OwnerAPI"
    assert reg.get(VIN).read("lat", 0) is not None and not api.get_car_api.called
    reg.get(VIN).observe(
        "lat", 38.0, "OwnerAPI"
    )  # a different value: the live feed still vouches (C1)
    assert reg.get(VIN).read("lat", 0) is not None and not api.get_car_api.called
    tm.mqttDisconnect(None, None, None, 7)  # feed down: the value ages
    reg.get(VIN).read("lat", 0)
    assert api.get_car_api.called


def test_feed_outage_then_recovery_pays_one_cloud_read(teslamate):  # C1
    tm, api, reg = teslamate
    feed(tm, state="online", latitude="37.0", longitude="-122.0")
    calls = []

    def vehicleData(vehicle, slot=None):
        calls.append(1)
        reg.get(VIN).observe(
            "lat", 37.00000001, "OwnerAPI"
        )  # the API's digits differ from TeslaMate's
        reg.get(VIN).observe("lon", -122.0, "OwnerAPI")
        return True

    api.update_vehicle_data = vehicleData
    api.is_awake = lambda vehicle, slot=None: True
    tm.mqttDisconnect(None, None, None, 7)  # broker blip
    assert reg.get(VIN).read("lat", 0) is not None and len(calls) == 1
    tm.mqttConnect(Mock(), None, None, 0)  # back; parked car publishes nothing
    for _ in range(5):
        assert reg.get(VIN).read("lat", 0) is not None
    assert len(calls) == 1


def test_sleeping_car_on_healthy_feed_stays_telemetry_sourced(teslamate):  # ruling 2
    tm, api, reg = teslamate
    v = reg.get(VIN)
    # Reported hours ago; TeslaMate has had nothing to say since.
    v.observe("state", "asleep", "TeslaMateVehicle", at=0.0)
    v.observe("pluggedIn", True, "TeslaMateVehicle", at=0.0)
    assert v.syncSource == "TeslaMateVehicle"
    assert telemetryPlug(v) is True
    assert api.is_awake(v) is False and not api.get_car_api.called
    tm.noteHealthy("7", "false")  # TeslaMate stopped polling
    assert v.syncSource == "TeslaAPI"
    assert telemetryPlug(v) is None
    tm.noteHealthy("7", "true")
    tm.mqttDisconnect(None, None, None, 7)  # broker gone
    assert v.syncSource == "TeslaAPI"


def test_telemetry_writes_carry_the_feed_source(teslamate):  # ruling 4
    tm, api, reg = teslamate
    feed(tm, charge_limit_soc="80", plugged_in="true", charging_state="Charging")
    v = reg.get(VIN)
    for field in ("chargeLimit", "pluggedIn", "chargingState", "name"):
        assert v.latest(field).source == "TeslaMateVehicle", field
    feed(tm, display_name="Renamed")  # the rename path
    assert (v.name, v.latest("name").source) == ("Renamed", "TeslaMateVehicle")
    assert reg.get(VIN).name == "Renamed"


def test_resumed_line_only_on_reconnect(teslamate, caplog):  # ruling 5
    tm, api, reg = teslamate
    caplog.set_level(logging.INFO)
    for i in range(5):
        feed(tm, battery_level=str(60 + i), state="online")
    assert "resumed" not in caplog.text
    tm.mqttDisconnect(None, None, None, 7)
    tm.mqttConnect(Mock(), None, None, 0)
    tm.mqttConnect(Mock(), None, None, 0)
    assert caplog.text.count("resumed") == 1


def test_healthy_topic_is_routed(teslamate):
    tm, api, reg = teslamate
    msg = Mock(topic="teslamate/cars/7/healthy", payload=b"false")
    tm.mqttMessage(None, None, msg)
    assert tm.healthy["7"] is False
    assert reg.sourceLiveAt("TeslaMateVehicle", reg.get(VIN)) is None


def test_prefixed_teslamate_topic_is_applied(teslamate):
    tm, api, reg = teslamate
    tm.mqtt_prefix = "home"
    msg = Mock(topic="teslamate/home/cars/7/charge_limit_soc", payload=b"77")
    tm.mqttMessage(None, None, msg)
    assert reg.get(VIN).latest("chargeLimit").value == 77


def test_load_car_vins_from_db(monkeypatch):  # ruling 7
    import TWCManager.Vehicle.TeslaMateVehicle as mod

    conn = Mock()
    conn.cursor.return_value.fetchall.return_value = [(7, VIN), (8, None)]
    connect = Mock(return_value=conn)
    monkeypatch.setattr(mod.psycopg2, "connect", connect)
    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    reg = VehicleRegistry({})
    master = Mock()
    master.vehicleRegistry = reg
    db = {"db_host": "h", "db_name": "n", "db_user": "u", "db_pass": "secretpw"}
    master.config = {"config": {}, "vehicle": {"TeslaMate": dict(enabled=True, **db)}}
    t = TeslaMateVehicle(master)
    assert t.carVINs == {"7": VIN} and conn.close.called
    cur = conn.cursor.return_value
    assert "FROM cars" in cur.execute.call_args.args[0]


def test_load_car_vins_without_db_or_on_failure(monkeypatch, caplog):
    import TWCManager.Vehicle.TeslaMateVehicle as mod

    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    connect = Mock(
        side_effect=mod.psycopg2.OperationalError('password "secretpw" user "u"')
    )
    monkeypatch.setattr(mod.psycopg2, "connect", connect)
    t, _ = make(TeslaMateVehicle)
    assert t.carVINs == {} and not connect.called  # no DB settings: name match
    caplog.set_level(logging.DEBUG)
    reg = VehicleRegistry({})
    master = Mock()
    master.vehicleRegistry = reg
    db = {"db_host": "h", "db_name": "n", "db_user": "u", "db_pass": "secretpw"}
    master.config = {"config": {}, "vehicle": {"TeslaMate": dict(enabled=True, **db)}}
    t = TeslaMateVehicle(master)
    assert t.carVINs == {} and connect.called
    assert "secretpw" not in caplog.text


def test_fleet_state_transitions_use_registry_state():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    t.applyDataToVehicle(VIN, "VehicleName", "C")
    v = reg.get(VIN)

    def msg(path, payload):
        t.mqttMessage(
            None, None, Mock(topic="telemetry/" + VIN + path, payload=payload)
        )

    msg("/v/Gear", b'"ShiftStateD"')
    assert (v.value("state"), v.latest("state").source) == (
        "driving",
        "FleetTelemetryMQTT",
    )
    msg("/v/BatteryLevel", b"55")  # online does not override driving
    assert v.value("state") == "driving" and v.value("batteryLevel") == 55
    msg("/connectivity", b'{"Status": "DISCONNECTED"}')
    assert v.value("state") == "offline"
    msg("/v/BatteryLevel", b"not json")  # undecodable payload: ignored


def test_refused_connection_is_not_live():
    t, reg = make()
    t.mqttConnect(None, None, None, 5)  # paho v1: rc 5, not authorised
    assert not t.connected
    t.mqttConnect(None, None, None, Mock(is_failure=True))  # paho v2 ReasonCode
    assert not t.connected
    t.mqttConnect(None, None, None, Mock(is_failure=False))
    assert t.connected


# Fix round 1: retained MQTT redelivery and forget.


@pytest.fixture
def clocked(monkeypatch):
    """A TeslaMate feed on an injected registry clock, connected and mapped."""
    monkeypatch.setattr(TeslaMateVehicle, "loadCarVINs", lambda self: {"7": VIN})
    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    now = {"t": 1000.0}
    t, reg = make(TeslaMateVehicle, clock=lambda: now["t"])
    t.mqttConnect(Mock(), None, None, 0)
    return t, reg, now


def tm_msg(t, topic, payload, retain):
    msg = Mock(topic="teslamate/cars/7/" + topic, payload=payload.encode())
    msg.retain = retain
    t.mqttMessage(None, None, msg)


def test_retained_replay_after_reconnect_does_not_beat_own_write(clocked):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    tm_msg(t, "charge_limit_soc", "50", False)
    v = reg.get(VIN)
    v.noteWrite("chargeLimit", 100, "TeslaBLE")  # at 1000
    now["t"] = 1005.0
    t.mqttDisconnect(None, None, None, 7)
    t.mqttConnect(Mock(), None, None, 0)
    tm_msg(t, "display_name", "C", True)
    tm_msg(t, "charge_limit_soc", "50", True)  # the broker's retained copy
    assert v.trusted("chargeLimit") is None  # inside FEED_LAG: our write stands
    assert v.latest("chargeLimit").at == 1000.0  # not restamped


def test_retained_differing_value_after_own_write_waits_for_feed_lag(clocked):
    from TWCManager.Vehicle.Registry.Fields import FEED_LAG

    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    v = reg.get(VIN)
    v.observe("chargeLimit", 60, "OwnerAPI", at=900.0)
    v.noteWrite("chargeLimit", 100, "TeslaBLE")  # at 1000
    now["t"] = 1005.0
    tm_msg(t, "charge_limit_soc", "50", True)  # TeslaMate's pre-write value
    obs = v.latest("chargeLimit")
    assert (obs.value, obs.at) == (50, 1000.0)
    assert v.trusted("chargeLimit") is None
    now["t"] = 1000.0 + FEED_LAG + 1
    assert v.trusted("chargeLimit").value == 50
    tm_msg(t, "charge_limit_soc", "100", False)  # a live change is news
    assert v.trusted("chargeLimit").value == 100


def test_unhealthy_feed_retained_replay_keeps_ageing_and_is_not_a_sighting(
    clocked,
):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    tm_msg(t, "state", "online", False)
    v = reg.get(VIN)
    seen = v.lastSeen
    tm_msg(t, "healthy", "false", True)
    now["t"] = 1000.0 + 40 * 86400
    t.mqttDisconnect(None, None, None, 7)
    t.mqttConnect(Mock(), None, None, 0)
    for topic, payload in (("display_name", "C"), ("state", "online")):
        tm_msg(t, topic, payload, True)
    assert v.latest("state").at == 1000.0
    assert v.age("state") == 40 * 86400
    assert v.lastSeen == seen
    assert reg.expire() == [VIN]


def test_retained_value_never_seen_is_observed_but_not_a_sighting(clocked):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    v = reg.get(VIN)
    seen = v.lastSeen
    now["t"] = 2000.0
    tm_msg(t, "battery_level", "61", True)
    assert v.latest("batteryLevel").value == 61 and v.lastSeen == seen


def test_retained_flag_survives_parking():
    now = {"t": 1000.0}
    t, reg = make(clock=lambda: now["t"])
    t.vinFor = lambda vid: VIN
    v = reg.getOrCreate(VIN, "OwnerAPI")
    v.observe("name", "C", "OwnerAPI")
    now["t"] = 2000.0
    t.applyDataToVehicle("1", "charge_limit_soc", "80", retained=True)
    t.applyDataToVehicle("1", "display_name", "C", retained=True)
    assert t.vehicles["1"] is v and v.latest("chargeLimit").value == 80
    assert v.lastSeen == 1000.0  # a retained replay is not a sighting


def test_forget_then_recreate_remaps_the_feed(clocked):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    old = reg.get(VIN)
    assert reg.forget(VIN)
    new = reg.getOrCreate(VIN, "OwnerAPI")
    tm_msg(t, "charge_limit_soc", "70", False)
    assert t.vehicles["7"] is new and new.latest("chargeLimit").value == 70
    assert old.latest("chargeLimit") is None
    assert reg.sourceLiveAt("TeslaMateVehicle", new) is not None


def test_forget_then_retained_redelivery_does_not_recreate(clocked, caplog):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    reg.forget(VIN)
    caplog.set_level(logging.WARNING)
    tm_msg(t, "display_name", "C", True)  # retained, after a reconnect
    tm_msg(t, "charge_limit_soc", "70", True)
    assert reg.get(VIN) is None and "7" not in t.vehicles
    t.retryUnmappedVehicles()  # the retry timer replays the name: retained too
    assert reg.get(VIN) is None


def test_forget_then_live_message_recreates_as_a_sighting(clocked):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    reg.forget(VIN)
    now["t"] = 1100.0
    tm_msg(t, "charge_limit_soc", "70", False)
    v = reg.get(VIN)
    assert v is not None and t.vehicles["7"] is v
    assert v.latest("chargeLimit").value == 70 and v.lastSeen == 1100.0


def test_expired_car_is_not_revived_by_daily_retained_replay(clocked):
    t, reg, now = clocked
    tm_msg(t, "display_name", "C", False)
    tm_msg(t, "state", "asleep", False)
    now["t"] = 1000.0 + 40 * 86400
    assert reg.expire() == [VIN]
    t.mqttDisconnect(None, None, None, 0)  # the daily resetMQTT
    t.mqttConnect(Mock(), None, None, 0)
    for topic, payload in (("display_name", "C"), ("state", "asleep")):
        tm_msg(t, topic, payload, True)
    t.retryUnmappedVehicles()
    assert reg.get(VIN) is None


def test_orphan_without_a_stored_name_or_vin_does_not_warn(caplog):
    t, reg = make()
    v = reg.getOrCreate(VIN, "OwnerAPI")
    t.vehicles["1"] = v  # mapped, but no name was ever stored
    reg.forget(VIN)
    caplog.set_level(logging.WARNING)
    t.applyDataToVehicle("1", "charge_limit_soc", "80")
    assert "named ''" not in caplog.text and "1" not in t.vehicles


def test_retained_change_published_after_the_echo_is_kept():  # probe D
    from TWCManager.Vehicle.Registry.Fields import FEED_LAG

    now = {"t": 900.0}
    t, reg = make(clock=lambda: now["t"])
    t.vinFor = lambda vid: VIN
    t.mqttConnect(None, None, None, 0)
    t.applyDataToVehicle("1", "display_name", "C")
    t.applyDataToVehicle("1", "charge_limit_soc", "50")
    v = reg.get(VIN)
    now["t"] = 1000.0
    v.noteWrite("chargeLimit", 100, "TeslaBLE")
    now["t"] = 1010.0
    t.applyDataToVehicle("1", "charge_limit_soc", "100")  # the feed's echo
    t.mqttDisconnect(None, None, None, 7)
    now["t"] = 5000.0  # the owner set 80 in the app meanwhile
    t.mqttConnect(None, None, None, 0)
    t.applyDataToVehicle("1", "charge_limit_soc", "80", retained=True)
    assert v.latest("chargeLimit").value == 80
    assert v.trusted("chargeLimit").value == 80
    assert now["t"] - 1000.0 > FEED_LAG


def test_teslamate_unmapped_vehicle_assumed_healthy(clocked):
    t, reg, now = clocked
    other = reg.getOrCreate("5YJ3E1EA1KF000002", "OwnerAPI")
    assert t.carHealthy(other) is True


def test_car_vins_reloaded_by_unmapped_retry(monkeypatch):
    import TWCManager.Vehicle.TeslaMateVehicle as mod

    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    conn = Mock()
    conn.cursor.return_value.fetchall.return_value = [(7, VIN)]
    connect = Mock(side_effect=[mod.psycopg2.OperationalError("down"), conn])
    monkeypatch.setattr(mod.psycopg2, "connect", connect)
    reg = VehicleRegistry({})
    master = Mock()
    master.vehicleRegistry = reg
    master.getModuleByName = Mock(return_value=None)
    db = {"db_host": "h", "db_name": "n", "db_user": "u", "db_pass": "p"}
    master.config = {"config": {}, "vehicle": {"TeslaMate": dict(enabled=True, **db)}}
    t = TeslaMateVehicle(master)
    assert t.carVINs == {}
    assert connect.call_args.kwargs["connect_timeout"] == 5
    t.scheduleUnmappedRetry = lambda delay=None: None
    t.applyDataToVehicle("7", "display_name", "C")  # no name match, no VINs
    assert "7" not in t.vehicles
    t.retryUnmappedVehicles()  # reloads the VINs
    assert t.carVINs == {"7": VIN}
    t.applyDataToVehicle("7", "battery_level", "61")  # a live report attaches
    assert t.vehicles["7"] is reg.get(VIN) and reg.get(VIN).name == "C"


def test_unmapped_vehicle_uses_its_car_health_by_vin(clocked):
    t, reg, now = clocked
    t.noteHealthy("7", "false")
    assert t.carHealthy(reg.getOrCreate(VIN, "OwnerAPI")) is False


def test_fleet_retained_value_does_not_mean_online():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    t.applyDataToVehicle(VIN, "VehicleName", "C")
    v = reg.get(VIN)

    def msg(path, payload, retain):
        m = Mock(topic="telemetry/" + VIN + path, payload=payload)
        m.retain = retain
        t.mqttMessage(None, None, m)

    msg("/connectivity", b'{"Status": "DISCONNECTED"}', False)
    msg("/v/ChargeLimitSoc", b"80", True)  # redelivered on reconnect
    assert v.value("state") == "offline" and v.value("chargeLimit") == 80
    msg("/v/ChargeLimitSoc", b"81", False)  # a live report: the car is online
    assert v.value("state") == "online"


# Fix round 3.


def test_parked_store_keeps_one_entry_per_topic(clocked):
    t, reg, now = clocked
    for day in range(10):
        for topic, payload in (
            ("display_name", "C"),
            ("state", "asleep"),
            ("charge_limit_soc", str(70 + day)),
            ("battery_level", "61"),
        ):
            tm_msg(t, topic, payload, True)  # retained only: never attaches
    parked = t.unknownVehicles["7"]
    assert len(parked) == 3
    assert ["charge_limit_soc", "79", True] in parked


def test_unmapped_retry_warns_once_per_car(clocked, caplog):
    t, reg, now = clocked
    t.scheduleUnmappedRetry = lambda delay=None: None
    tm_msg(t, "display_name", "Ghost", True)
    caplog.set_level(logging.DEBUG)
    for _ in range(5):
        t.retryUnmappedVehicles()
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "Ghost" in r.message
    ]
    assert len(warnings) <= 1


def test_retry_for_a_forgotten_car_never_loads_the_vehicle_list(monkeypatch):
    monkeypatch.setattr(TeslaMateVehicle, "loadCarVINs", lambda self: {"7": VIN})
    monkeypatch.setattr(
        TeslaMateVehicle, "doSyncTokens", lambda self, firstrun=False: None
    )
    carapi = Mock()
    carapi.getCarApiRetryRemaining.return_value = 0
    t, reg = make(TeslaMateVehicle, carapi=carapi)
    t.scheduleUnmappedRetry = lambda delay=None: None
    t.mqttConnect(Mock(), None, None, 0)
    tm_msg(t, "display_name", "C", False)
    reg.forget(VIN)
    tm_msg(t, "charge_limit_soc", "70", True)  # orphan dropped, not revived
    t.retryUnmappedVehicles()
    assert not carapi.loadVehicleList.called and reg.get(VIN) is None


def test_fleet_derives_no_state_from_retained_values():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    t.applyDataToVehicle(VIN, "VehicleName", "C")
    v = reg.get(VIN)

    def msg(path, payload, retain):
        m = Mock(topic="telemetry/" + VIN + path, payload=payload)
        m.retain = retain
        t.mqttMessage(None, None, m)

    msg("/connectivity", b'{"Status": "DISCONNECTED"}', False)
    msg("/v/DetailedChargeState", b'"DetailedChargeStateCharging"', True)
    msg("/v/Gear", b'"ShiftStateD"', True)
    assert v.value("state") == "offline"


def test_fleet_live_message_attaches_by_topic_vin_without_a_name():
    t, reg = make(FleetTelemetryMQTT)  # after a restart: no name seen
    t.mqtt_prefix = "telemetry"
    m = Mock(topic="telemetry/" + VIN + "/v/BatteryLevel", payload=b"55")
    m.retain = False
    t.mqttMessage(None, None, m)
    v = reg.get(VIN)
    assert v is not None and t.vehicles[VIN] is v
    assert v.value("batteryLevel") == 55 and v.value("state") == "online"


def test_a_live_report_lists_the_source_on_the_car_but_retained_does_not():
    t, reg = make()
    t.vinFor = lambda vid: VIN
    t.applyDataToVehicle("1", "display_name", "TestCar", retained=True)
    t.applyDataToVehicle("1", "charge_limit_soc", "80", retained=True)
    assert reg.get(VIN) is None  # a retained message never creates a car
    t.applyDataToVehicle("1", "display_name", "TestCar")
    claim = reg.get(VIN).claims["Probe"]
    assert (claim.status, claim.commands, claim.keepsAlive) == ("reports", False, False)
    # a silent telemetry-only car still ages out, and the claim buys it nothing
    assert reg.expire(now=reg.now() + reg.forgetAfter + 1) == [VIN]
    # a car with only retained data later reported live: claim made once
    t.applyDataToVehicle("1", "display_name", "TestCar")
    reg.get(VIN).claims["Probe"]
