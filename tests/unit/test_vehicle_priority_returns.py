# tests/unit/test_vehicle_priority_returns.py
#
# The facade ends its walk on any truthy return other than "error". These
# tests drive the real TeslaAPI methods (never a mock that returns
# "success") so a success read as failure cannot send a second, billed
# command through Home Assistant.
from unittest.mock import Mock

import pytest
import requests_mock as rm

from TWCManager.Vehicle.Providers import Capability as C
from tests.unit.test_vehicle_priority import P, make, mod


@pytest.fixture
def no_http(requests_mock):
    return requests_mock.register_uri(rm.ANY, rm.ANY, json={})


def _fleetThenHa(ha):
    # A real TeslaAPI with a Fleet token and its real providers, with Home
    # Assistant ranked after it
    from tests.unit.test_carapivehicle_registry import api_with_registry
    from tests.unit.teslatokens import fleet_token

    api, reg = api_with_registry()
    api.storeToken(fleet_token())
    m, vp = make()
    m.vehicleRegistry = reg
    for p in api.providers():
        m.providerOrder.register(p)
    m.providerOrder.register(
        P("HomeAssistant", 6, False, ha, caps={c: C.Declaration(C.HA_BILLED) for c in C.COMMANDS})
    )
    return api, vp


def test_a_none_return_is_a_failure_and_the_walk_continues():
    first = mod(wakeVehicle=None)
    second = mod(wakeVehicle="success")
    m, vp = make({}, P("FleetAPI", 5, False, first), P("HomeAssistant", 6, False, second))
    assert vp.wakeVehicle(None) == "success"
    assert (first.wakeVehicle.call_count, second.wakeVehicle.call_count) == (2, 1)


def test_update_charge_at_home_ends_at_teslaapi_when_it_holds_cars(no_http):
    from tests.unit.teslatokens import add_home_vehicle

    ha = mod(updateChargeAtHome=True)
    api, vp = _fleetThenHa(ha)
    car = add_home_vehicle(api)
    car.update_charge = Mock(return_value=True)
    assert vp.updateChargeAtHome() is True
    assert car.update_charge.call_count == 1 and ha.updateChargeAtHome.call_count == 0


def test_update_charge_at_home_reaches_ha_when_teslaapi_holds_no_car(no_http):
    ha = mod(updateChargeAtHome=True)
    api, vp = _fleetThenHa(ha)
    assert api.updateChargeAtHome() is False
    assert vp.updateChargeAtHome() is True and ha.updateChargeAtHome.call_count == 1


def test_home_assistant_update_charge_at_home_returns_true():
    from tests.unit.test_homeassistant_registry import make_ha

    ha, _ = make_ha()
    car = Mock()
    ha.carApiVehicles = [car]
    assert ha.updateChargeAtHome() is True and car.refresh_from_home_assistant.call_count == 1


# Concern rulings, item 1: Home Assistant reached only because a billed
# provider failed is held to Fleet's cadence, per car and command

VIN = "5YJ3E1EA1KF000001"
OTHER = "5YJ3E1EA1KF000009"
FLEET_CMDS = {c: C.Declaration(C.FLEET_COMMAND) for c in C.COMMANDS}
HA_CMDS = {c: C.Declaration(C.HA_BILLED) for c in C.COMMANDS}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _backoff(fleet=True):
    api = mod(setChargeRate="error", wakeVehicle="error")
    ha = mod(setChargeRate="success", wakeVehicle="success")
    providers = [P("HomeAssistant", 6, False, ha, caps=HA_CMDS)]
    if fleet:
        providers.append(P("FleetAPI", 5, False, api, caps=FLEET_CMDS))
    m, vp = make({}, *providers)
    for vin in (VIN, OTHER):
        m.vehicleRegistry.getOrCreate(vin, "t")
    vp._clock = clock = Clock()
    return vp, ha, api, clock


def test_ha_after_a_fleet_failure_is_held_to_fleets_cadence():
    vp, ha, api, clock = _backoff()
    car = Mock(VIN=VIN)
    assert vp.setChargeRate(10, car) == "success"
    clock.t += 10
    assert vp.setChargeRate(10, car) is False                   # Fleet tried, HA held
    assert (api.setChargeRate.call_count, ha.setChargeRate.call_count) == (4, 1)
    clock.t += 51
    assert vp.setChargeRate(10, car) == "success" and ha.setChargeRate.call_count == 2


def test_the_hold_is_per_car_and_per_command():
    vp, ha, api, clock = _backoff()
    assert vp.setChargeRate(10, Mock(VIN=VIN)) == "success"
    assert vp.setChargeRate(10, Mock(VIN=OTHER)) == "success"
    assert vp.wakeVehicle(VIN) == "success"
    assert (ha.setChargeRate.call_count, ha.wakeVehicle.call_count) == (2, 1)
    clock.t += 10
    assert vp.wakeVehicle(VIN) is False and ha.wakeVehicle.call_count == 1


def test_ha_only_install_keeps_its_own_cadence():
    vp, ha, api, clock = _backoff(fleet=False)
    for _ in range(3):
        assert vp.setChargeRate(10, Mock(VIN=VIN)) == "success"
        clock.t += 10
    assert ha.setChargeRate.call_count == 3
