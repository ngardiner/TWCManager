from unittest.mock import Mock
import pytest
from TWCManager.TWCMaster import TWCMaster

VIN = "5YJ3E1EA1KF000001"
CONFIG = {
    "config": {
        "debugOutputToFile": False,
        "subtractChargerLoad": False,
        "treatGenerationAsGridDelivery": False,
        "wiringMaxAmpsAllTWCs": 80,
        "maxAmpsAllowedFromGrid": None,
    }
}


@pytest.fixture
def master():
    m = TWCMaster("AB", CONFIG)
    m.getModuleByName = Mock(return_value=None)
    m.queue_background_task = Mock()
    m.getModulesByType = Mock(return_value=[])
    return m


def slave(vin):
    s = Mock()
    s.TWCID = bytearray(b"\x82\x44")
    s.currentVIN = vin
    s.lifetimekWh = 100
    return s


def test_first_sighting_requeries_and_records_nothing(master):
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(slave(VIN))
    master.queue_background_task.assert_called_once_with(
        {"cmd": "getVehicleVIN", "slaveTWC": bytearray(b"\x82\x44"), "vinPart": 0},
        delay=5,
    )
    assert "Vehicles" not in master.settings or VIN not in master.settings["Vehicles"]


def test_second_matching_sighting_records_one_session(master):
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(slave(VIN))
    master.recordVehicleVIN(slave(VIN))
    assert master.settings["Vehicles"][VIN]["chargeSessions"] == 1
    assert master.settings["SlaveTWCs"]["8244"]["supportsVINQuery"] == 1
    assert master.vehicleRegistry.get(VIN).latest("twcVin").value == "8244"


def test_known_vehicle_records_on_first_sighting(master):
    master.vehicleRegistry.getOrCreate(VIN, "TeslaAPI")
    master.vehicleRegistry.beginPlugSession("8244")
    master.recordVehicleVIN(slave(VIN))
    assert master.settings["Vehicles"][VIN]["chargeSessions"] == 1
    assert not any(
        c.args[0]["cmd"] == "getVehicleVIN"
        for c in master.queue_background_task.call_args_list
    )


def test_charging_session_begins_and_ends_anonymous_vehicle(master):
    s = slave("")
    s.reportedAmpsActual = 5.0
    s.isCharging = 0
    s.chargingDroppedBelowThresholdTime = 0
    master.getSlaveTWCs = Mock(return_value=[s])
    master.num_cars_charging_now()
    assert master.vehicleRegistry.get("anon:8244") is not None
    s.reportedAmpsActual = 0.0
    s.chargingDroppedBelowThresholdTime = 1  # long ago
    master.num_cars_charging_now()
    assert master.vehicleRegistry.get("anon:8244") is None
