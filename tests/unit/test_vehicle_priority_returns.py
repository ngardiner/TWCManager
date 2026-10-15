# tests/unit/test_vehicle_priority_returns.py
#
# The facade ends its walk on any truthy return other than "error". These
# tests drive the real TeslaAPI methods (never a mock that returns
# "success") so a success read as failure cannot send a second, billed
# command through Home Assistant.
import time
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
    first = mod(applyChargeLimit=None)
    second = mod(applyChargeLimit="success")
    m, vp = make({}, P("FleetAPI", 5, False, first), P("HomeAssistant", 6, False, second))
    assert vp.applyChargeLimit(limit=80) == "success"
    assert (first.applyChargeLimit.call_count, second.applyChargeLimit.call_count) == (2, 1)


def test_teslaapi_apply_charge_limit_returns_success(no_http):
    api, _ = _fleetThenHa(mod())
    api.car_api_available = Mock(return_value=True)
    assert api.applyChargeLimit(80, checkArrival=True) == "success"
    assert api.applyChargeLimit(-1, checkDeparture=True) == "success"
    assert no_http.call_count == 0


def test_ha_is_not_reached_after_a_fleet_apply_charge_limit(no_http):     # C1
    ha = mod(applyChargeLimit="success")
    api, vp = _fleetThenHa(ha)
    api.car_api_available = Mock(return_value=True)
    assert vp.applyChargeLimit(limit=80, checkArrival=True) == "success"
    assert ha.applyChargeLimit.call_count == 0


def test_throttled_apply_charge_limit_is_not_a_failure(no_http):
    # Policy re-queues the limit every evaluation; TeslaAPI applies it at
    # most once a minute. Its skip must not hand the limit to HA.
    ha = mod(applyChargeLimit="success")
    api, vp = _fleetThenHa(ha)
    api.car_api_available = Mock(return_value=True)
    api.carApiLastChargeLimitApplyTime = time.time()
    assert api.applyChargeLimit(80) not in (None, False, "error")
    assert vp.applyChargeLimit(limit=80) and ha.applyChargeLimit.call_count == 0


def test_throttled_start_is_not_a_failure(no_http):
    # TWCSlave asks to start every heartbeat while the car draws nothing;
    # TeslaAPI sends at most once a minute. Its skip must not hand the
    # start to HA.
    ha = mod(car_api_charge="success")
    api, vp = _fleetThenHa(ha)
    api.carApiLastStartOrStopChargeTime = time.time()
    assert api.car_api_charge({"charge": True}) not in (None, False, "error")
    assert vp.car_api_charge({"charge": True}) and ha.car_api_charge.call_count == 0
    assert no_http.call_count == 0


def test_a_failed_fleet_start_still_falls_to_ha(no_http):
    # A real failure (Fleet unavailable) keeps the sanctioned fall-through
    ha = mod(car_api_charge="success")
    api, vp = _fleetThenHa(ha)
    api.car_api_available = Mock(return_value=False)
    assert vp.car_api_charge({"charge": True}) == "success"
    assert ha.car_api_charge.call_count == 1


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
