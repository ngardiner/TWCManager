"""Fleet Telemetry values arrive typed (numbers, bools, prefixed enum names,
a Location object) and must convert."""

import json
from unittest.mock import Mock

import pytest

from tests.unit.test_telemetry_registry import make
from TWCManager.Vehicle.FleetTelemetryMQTT import FleetTelemetryMQTT

VIN = "5YJ3E1EA1KF000001"

# topic, payload, field, expected
CASES = [
    ("BatteryLevel", 80.5, "batteryLevel", 80),
    ("ChargeLimitSoc", 90, "chargeLimit", 90),
    ("TimeToFullCharge", 1.5, "timeToFullCharge", 1.5),
    ("ChargeCurrentRequest", 16, "chargeCurrentRequest", 16),
    ("ChargeCurrentRequestMax", 32, "chargeCurrentRequestMax", 32),
    ("ChargeAmps", 15.5, "actualCurrent", 15.5),
    ("ChargerPhases", 3, "phases", 3),
    ("ChargerVoltage", 231.2, "voltage", 231.2),
    ("ACChargingPower", 7.1, "chargerPower", 7.1),
    ("FastChargerPresent", True, "fastChargerPresent", True),
    ("FastChargerPresent", False, "fastChargerPresent", False),
    ("FastChargerType", "FastChargerSupercharger", "fastChargerType", "Supercharger"),
    ("FastChargerType", "FastChargerUnknown", "fastChargerType", ""),
    ("ChargingCableType", "CableTypeIEC", "connChargeCable", "IEC"),
    ("DetailedChargeState", "DetailedChargeStateStopped", "chargingState", "Stopped"),
    ("VehicleName", "TestCar", "name", "TestCar"),
]


def send(t, topic, payload, retain=False):
    msg = Mock(topic=f"telemetry/{VIN}/{topic}", payload=json.dumps(payload).encode())
    msg.retain = retain
    t.mqttMessage(None, None, msg)


@pytest.mark.parametrize("topic,payload,field,expected", CASES)
def test_each_mapped_field_converts(topic, payload, field, expected):
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(t, f"v/{topic}", payload)
    assert reg.get(VIN).latest(field).value == expected


def test_charging_state_marks_the_car_charging():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(t, "v/DetailedChargeState", "DetailedChargeStateCharging")
    assert reg.get(VIN).latest("state").value == "charging"


@pytest.mark.parametrize(
    "gear,state", [("ShiftStateD", "driving"), ("ShiftStateP", "online")]
)
def test_gear(gear, state):
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(t, "v/Gear", gear)
    assert reg.get(VIN).latest("state").value == state


def test_location_object_sets_lat_lon():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(t, "v/Location", {"latitude": 47.6, "longitude": -122.3})
    v = reg.get(VIN)
    assert (v.latest("lat").value, v.latest("lon").value) == (47.6, -122.3)


def test_location_that_is_not_an_object_is_ignored():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(t, "v/Location", "47.6,-122.3")
    assert reg.get(VIN) is None or reg.get(VIN).latest("lat") is None


@pytest.mark.parametrize(
    "status,state", [("CONNECTED", "online"), ("DISCONNECTED", "offline")]
)
def test_connectivity_record(status, state):
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(
        t,
        "connectivity",
        {"ConnectionId": "x", "Status": status, "CreatedAt": "2025-01-01T00:00:00Z"},
    )
    assert reg.get(VIN).latest("state").value == state


def test_invalid_value_publishes_null_and_is_no_reading():
    t, reg = make(FleetTelemetryMQTT)
    t.mqtt_prefix = "telemetry"
    send(t, "v/DetailedChargeState", None)
    send(t, "v/ChargerVoltage", None)
    v = reg.get(VIN)
    assert v is None or v.latest("voltage") is None
