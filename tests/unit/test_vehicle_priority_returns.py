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
    # A real failure (the API in its error backoff) keeps the sanctioned
    # fall-through
    from tests.unit.teslatokens import add_home_vehicle

    ha = mod(car_api_charge="success")
    api, vp = _fleetThenHa(ha)
    add_home_vehicle(api)
    api.updateCarApiLastErrorTime()
    assert api.car_api_charge({"charge": True}) == "error"
    assert vp.car_api_charge({"charge": True}) == "success"
    assert ha.car_api_charge.call_count == 1


def _asleep(api):
    from tests.unit.teslatokens import add_home_vehicle

    car = add_home_vehicle(api)
    car.lastAPIAccessTime = 0
    car.is_awake = Mock(return_value=False)
    return car


def test_the_pre_wake_hold_is_not_a_failure(no_http):
    # A sleeping car just plugged in: Fleet holds off waking it on purpose
    # (wakeDelayMins). HA must not start it instead, which would wake it
    # and bill through HA's Tesla integration
    ha = mod(car_api_charge="success", applyChargeLimit="success")
    api, vp = _fleetThenHa(ha)
    car = _asleep(api)
    assert api.car_api_charge({"charge": True}) not in (None, False, "error")
    assert car.firstChargeNeededTime > 0                       # the hold began
    api.carApiLastStartOrStopChargeTime = 0
    assert vp.car_api_charge({"charge": True}) and ha.car_api_charge.call_count == 0
    api.master.getNormalChargeLimit = Mock(return_value=(True, 70, 70))
    assert vp.applyChargeLimit(limit=80) and ha.applyChargeLimit.call_count == 0
    assert no_http.call_count == 0


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
    api = mod(car_api_charge="error", applyChargeLimit="error")
    ha = mod(car_api_charge="success", applyChargeLimit="success")
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
    start = {"charge": True, "vin": VIN}
    assert vp.car_api_charge(start) == "success"
    clock.t += 10
    assert vp.car_api_charge(start) is False                    # Fleet tried, HA held
    assert (api.car_api_charge.call_count, ha.car_api_charge.call_count) == (4, 1)
    clock.t += 51
    assert vp.car_api_charge(start) == "success" and ha.car_api_charge.call_count == 2


def test_the_hold_is_per_car_and_per_command():
    vp, ha, api, clock = _backoff()
    assert vp.car_api_charge({"charge": True, "vin": VIN}) == "success"
    assert vp.car_api_charge({"charge": True, "vin": OTHER}) == "success"
    assert vp.car_api_charge({"charge": False, "vin": VIN}) == "success"
    assert vp.applyChargeLimit(limit=80, vin=VIN) == "success"
    assert (ha.car_api_charge.call_count, ha.applyChargeLimit.call_count) == (3, 1)
    clock.t += 10
    assert vp.applyChargeLimit(limit=80, vin=VIN) is False and ha.applyChargeLimit.call_count == 1


def test_ha_only_install_keeps_its_own_cadence():
    vp, ha, api, clock = _backoff(fleet=False)
    start = {"charge": True, "vin": VIN}
    for _ in range(3):
        assert vp.car_api_charge(start) == "success"
        clock.t += 10
    assert ha.car_api_charge.call_count == 3
