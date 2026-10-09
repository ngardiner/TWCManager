"""
TeslaAPIController lists no vehicle a local TWC serves or might be serving,
so a car on a TWC never gets its own power share (and paid Fleet API
set_charging_amps commands) alongside the TWC.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.EVSEController.TeslaAPIController import TeslaAPIController
from TWCManager.TWCSlave import TWCSlave

VIN1 = "5YJ3E1EA1KF000001"
VIN2 = "5YJ3E1EA1KF000002"

# TWCSlave's plug-session sentinel for a car that isn't a Tesla. TWCSlave on
# main has no plug sessions (plugVIN), so stand in for it here.
NON_TESLA = getattr(TWCSlave, "NON_TESLA", "non-tesla")


def slave(currentVIN="", plugVIN=None, amps=0.0, state=0x00):
    return SimpleNamespace(
        currentVIN=currentVIN,
        plugVIN=plugVIN,
        NON_TESLA=NON_TESLA,
        reportedAmpsActual=amps,
        reportedState=state,
    )


def vehicle(vin, atHome=True, syncSource="TeslaAPI", pluggedIn=None):
    return SimpleNamespace(
        VIN=vin, atHome=atHome, syncSource=syncSource, pluggedIn=pluggedIn
    )


def make_controller(slaves, vehicles):
    carapi = Mock()
    carapi.enabled.return_value = True
    carapi.getCarApiVehicles.return_value = vehicles
    master = Mock()
    master.config = {"config": {}}
    master.getModuleByName.return_value = carapi
    master.getSlaveTWCs.return_value = slaves
    return TeslaAPIController(master)


def vins(controller):
    return {e.currentVIN for e in controller.allEVSEs}


def test_vinless_twc_drawing_power_excludes_every_vehicle():
    # The incident: a TWC with no VIN yet drawing 47 A, car at home.
    c = make_controller([slave(amps=47.0)], [vehicle(VIN1)])
    assert c.allEVSEs == []


def test_vinless_twc_idle_does_not_exclude():
    c = make_controller([slave(amps=0.5)], [vehicle(VIN1)])
    assert vins(c) == {VIN1}


def test_vehicle_on_twc_by_current_vin_is_excluded():
    c = make_controller(
        [slave(currentVIN=VIN1, amps=32.0)], [vehicle(VIN1), vehicle(VIN2)]
    )
    assert vins(c) == {VIN2}


def test_vehicle_on_twc_by_plug_vin_is_excluded():
    c = make_controller([slave(plugVIN=VIN1)], [vehicle(VIN1), vehicle(VIN2)])
    assert vins(c) == {VIN2}


def test_non_tesla_plug_vin_is_not_a_served_vin():
    c = make_controller([slave(plugVIN=NON_TESLA)], [vehicle(VIN1)])
    assert vins(c) == {VIN1}


def test_non_tesla_plug_vin_drawing_power_counts_as_unknown():
    # The sentinel is not a VIN: a TWC with it that draws power can't name
    # its car, so it might be any of them.
    c = make_controller([slave(plugVIN=NON_TESLA, amps=16.0)], [vehicle(VIN1)])
    assert c.allEVSEs == []


def test_vehicle_on_third_party_charger_is_included():
    c = make_controller([slave()], [vehicle(VIN1), vehicle(VIN2, atHome=False)])
    assert vins(c) == {VIN1}


def test_slave_without_plug_vin_attribute():
    # TWCSlave on main has no plugVIN.
    s = SimpleNamespace(currentVIN=VIN1, reportedAmpsActual=10.0, reportedState=1)
    c = make_controller([s], [vehicle(VIN1), vehicle(VIN2)])
    assert vins(c) == {VIN2}


def test_cached_evse_dropped_once_twc_serves_vehicle():
    s = slave()
    c = make_controller([s], [vehicle(VIN1)])
    assert vins(c) == {VIN1}
    s.currentVIN = VIN1
    assert c.allEVSEs == []
    assert c._evse_cache == {}


def test_vinless_twc_plugged_idle_excludes_every_vehicle():
    # Plugged in but not drawing (Stopped on a schedule): no VIN query yet.
    c = make_controller([slave(state=0x03)], [vehicle(VIN1), vehicle(VIN2)])
    assert c.allEVSEs == []


def test_vinless_twc_ramping_excludes_every_vehicle():
    # 1-3 A at charge start, before the VIN query answers.
    c = make_controller([slave(amps=1.5)], [vehicle(VIN1)])
    assert c.allEVSEs == []


def test_vinless_twc_just_below_charging_threshold_is_not_blind():
    c = make_controller([slave(amps=0.9)], [vehicle(VIN1)])
    assert vins(c) == {VIN1}


def test_telemetry_plugged_car_excluded_while_a_twc_is_unnamed():
    # Gen2 reports a steady 00 for an idle plugged car; only telemetry
    # says this car is plugged in, maybe into that TWC.
    c = make_controller(
        [slave()],
        [vehicle(VIN1, syncSource="TeslaMate", pluggedIn=True), vehicle(VIN2)],
    )
    assert vins(c) == {VIN2}


def test_telemetry_plugged_car_included_when_every_twc_is_named():
    c = make_controller(
        [slave(currentVIN=VIN2)],
        [vehicle(VIN1, syncSource="TeslaMate", pluggedIn=True)],
    )
    assert vins(c) == {VIN1}


def test_stale_telemetry_plug_state_is_ignored():
    # syncSource reverts to TeslaAPI once the feed goes stale.
    c = make_controller([slave()], [vehicle(VIN1, pluggedIn=True)])
    assert vins(c) == {VIN1}


def test_failed_slave_scan_fails_closed():
    c = make_controller([], [vehicle(VIN1)])
    c.master.getSlaveTWCs.side_effect = RuntimeError("boom")
    assert c.allEVSEs == []
