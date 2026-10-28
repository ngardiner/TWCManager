import threading
from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.Control.HTTPControl import deleteVehicleGroup, vehicleGroupMgmt

VIN = "5YJ3E1EA1KF000001"


def master():
    return SimpleNamespace(
        settingsLock=threading.RLock(),
        queue_background_task=Mock(),
        settings={
            "VehicleGroups": {
                "Deny Charging": {"Built-in": 1, "Members": []},
                "Guests": {"Built-in": 0, "Members": []},
            }
        },
    )


def members(m, group="Deny Charging"):
    return m.settings["VehicleGroups"][group]["Members"]


def test_adding_twice_lists_the_car_once():
    m = master()
    for _ in range(2):
        assert vehicleGroupMgmt(m, "add", "Deny Charging", VIN) == (
            "/vehicleDetail/" + VIN
        )
    assert members(m) == [VIN]
    m.queue_background_task.assert_any_call({"cmd": "saveSettings"})
    m.queue_background_task.assert_any_call({"cmd": "checkVINEntitlement", "vin": VIN})


def test_one_remove_takes_the_car_out():
    m = master()
    # settings saved by earlier versions may list a car more than once
    members(m).extend([VIN, "OTHER", VIN])
    vehicleGroupMgmt(m, "remove", "Deny Charging", VIN)
    assert members(m) == ["OTHER"]
    # removing a car that isn't there changes nothing
    vehicleGroupMgmt(m, "remove", "Deny Charging", VIN)
    assert members(m) == ["OTHER"]


def test_a_numeric_group_name_arrives_from_the_form_as_an_int():
    m = master()
    m.settings["VehicleGroups"]["2024"] = {"Built-in": 0, "Members": []}
    vehicleGroupMgmt(m, "add", 2024, VIN)
    assert members(m, "2024") == [VIN]


def test_a_group_that_is_gone_changes_nothing():
    m = master()
    assert vehicleGroupMgmt(m, "add", "Deleted", VIN) == "/vehicleDetail/" + VIN
    assert "Deleted" not in m.settings["VehicleGroups"]
    m.queue_background_task.assert_not_called()


def test_built_in_groups_cannot_be_deleted():
    m = master()
    assert not deleteVehicleGroup(m, "Deny Charging")
    assert not deleteVehicleGroup(m, "Missing")
    assert "Deny Charging" in m.settings["VehicleGroups"]
    m.queue_background_task.assert_not_called()
    assert deleteVehicleGroup(m, "Guests")
    assert "Guests" not in m.settings["VehicleGroups"]
    m.queue_background_task.assert_called_once_with({"cmd": "saveSettings"})
