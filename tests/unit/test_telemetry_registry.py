# tests/unit/test_telemetry_registry.py
import logging
import time
from unittest.mock import Mock

import pytest

from tests.unit.teslatokens import FakeMaster, new_api
from TWCManager.EVSEController.TeslaAPIController import TeslaAPIController
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
    t.applyDataToVehicle("1", "display_name", "Callandor")  # maps and replays at once
    v = reg.get(VIN)
    assert v.name == "Callandor" and t.vehicles["1"] is v
    assert (v.latest("chargeLimit").value, v.latest("chargeLimit").source) == (
        80,
        "Probe",
    )


def test_name_match_against_registry_without_cloud_list():
    t, reg = make()
    reg.getOrCreate(VIN, "TeslaBLE").observe("name", "Callandor", "TeslaAPI")
    assert t.updateVehicles("1", "Callandor", loadList=False) is True
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
    from TWCManager.Vehicle.TeslaAPI import CarApiVehicle

    assert not any(
        hasattr(CarApiVehicle, n)
        for n in ("syncState", "syncLat", "syncLon", "_legacySyncSource")
    )


def test_connection_state_is_the_feed_liveness():
    t, reg = make()
    t.vinFor = lambda vid: VIN
    t.applyDataToVehicle("1", "display_name", "Callandor")
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
    api.carApiVehicles = []
    master.modules["TeslaAPI"] = api
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    # A cloud call returns nothing usable; tests assert it is never made.
    car.get_car_api = Mock(return_value=(False, None))
    master.config["vehicle"]["TeslaMate"] = {"enabled": True}
    tm = TeslaMateVehicle(master)
    tm.mqttConnect(Mock(), None, None, 0)
    tm.applyDataToVehicle("7", "display_name", "C")
    return tm, car, master.vehicleRegistry


def feed(tm, **topics):
    for topic, payload in topics.items():
        tm.applyDataToVehicle("7", topic, payload)


def test_healthy_teslamate_car_reads_without_api_calls(teslamate):  # ruling 1
    tm, car, reg = teslamate
    feed(tm, state="online", latitude="37.0", longitude="-122.0", battery_level="61")
    assert car.update_location(0) is True  # maxAge 0: the feed vouches for now
    assert car.update_charge() is True
    assert not car.get_car_api.called
    assert reg.get(VIN).latest("lat").source == "TeslaMateVehicle"


def test_feed_vouches_for_a_value_the_api_reobserved(teslamate):  # ruling 3
    tm, car, reg = teslamate
    feed(tm, state="online", latitude="37.0", longitude="-122.0")
    car.lat = 37.0  # the cloud saw the same position after the feed
    car.lon = -122.0
    assert reg.get(VIN).latest("lat").source == "TeslaAPI"
    assert car.update_location(0) is True and not car.get_car_api.called
    car.lat = 38.0  # a value the feed never reported is not vouched for
    car.update_location(0)
    assert car.get_car_api.called


def test_sleeping_car_on_healthy_feed_stays_telemetry_sourced(teslamate):  # ruling 2
    tm, car, reg = teslamate
    v = reg.get(VIN)
    # Reported hours ago; TeslaMate has had nothing to say since.
    v.observe("state", "asleep", "TeslaMateVehicle", at=0.0)
    v.observe("pluggedIn", True, "TeslaMateVehicle", at=0.0)
    assert car.syncSource == "TeslaMateVehicle"
    assert TeslaAPIController._telemetryPluggedIn(car) is True
    assert car.is_awake() is False and not car.get_car_api.called
    tm.noteHealthy("7", "false")  # TeslaMate stopped polling
    assert car.syncSource == "TeslaAPI"
    assert TeslaAPIController._telemetryPluggedIn(car) is False
    tm.noteHealthy("7", "true")
    tm.mqttDisconnect(None, None, None, 7)  # broker gone
    assert car.syncSource == "TeslaAPI"


def test_telemetry_writes_carry_the_feed_source(teslamate):  # ruling 4
    tm, car, reg = teslamate
    feed(tm, charge_limit_soc="80", plugged_in="true", charging_state="Charging")
    v = reg.get(VIN)
    for field in ("chargeLimit", "pluggedIn", "chargingState", "name"):
        assert v.latest(field).source == "TeslaMateVehicle", field
    feed(tm, display_name="Renamed")  # the rename path
    assert (v.name, v.latest("name").source) == ("Renamed", "TeslaMateVehicle")
    assert car.name == "Renamed"


def test_resumed_line_only_on_reconnect(teslamate, caplog):  # ruling 5
    tm, car, reg = teslamate
    caplog.set_level(logging.INFO)
    for i in range(5):
        feed(tm, battery_level=str(60 + i), state="online")
    assert "resumed" not in caplog.text
    tm.mqttDisconnect(None, None, None, 7)
    tm.mqttConnect(Mock(), None, None, 0)
    tm.mqttConnect(Mock(), None, None, 0)
    assert caplog.text.count("resumed") == 1


def test_healthy_topic_is_routed(teslamate):
    tm, car, reg = teslamate
    msg = Mock(topic="teslamate/cars/7/healthy", payload=b"false")
    tm.mqttMessage(None, None, msg)
    assert tm.healthy["7"] is False
    assert reg.sourceLiveAt("TeslaMateVehicle", reg.get(VIN)) is None


def test_prefixed_teslamate_topic_is_applied(teslamate):
    tm, car, reg = teslamate
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

    msg("/v/Gear", b'"D"')
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
