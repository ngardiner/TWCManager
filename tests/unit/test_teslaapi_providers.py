# tests/unit/test_teslaapi_providers.py
import re
from unittest.mock import Mock

import pytest
from requests_mock import ANY

from TWCManager.Vehicle.Providers import Capability as C
from tests.unit.test_carapivehicle_registry import api_with_registry, VIN
from tests.unit.teslatokens import fleet_token, owner_token

OWNER_VEHICLE = "https://owner-api.teslamotors.com/api/1/vehicles/" + VIN
FLEET_ANY = re.compile(r"https://fleet-api\.prd\..*")


def test_two_providers_with_honest_costs():
    api, reg = api_with_registry()
    owner, fleet = api.providers()
    assert (owner.name, owner.rank, fleet.name, fleet.rank, owner.local) == (
        "OwnerAPI",
        4,
        "FleetAPI",
        5,
        False,
    )
    assert owner.cost(C.READ_CHARGE) == C.CLOUD_FREE and owner.cost(C.SET_AMPS) is None
    assert (
        fleet.cost(C.READ_ONLINE) == C.CLOUD_FREE
        and fleet.cost(C.READ_CHARGE) == C.FLEET_DATA
    )
    assert fleet.cost(C.SET_AMPS) == C.FLEET_COMMAND and fleet.cost(C.WAKE) == C.FLEET_WAKE
    assert fleet.declarations()[C.SET_AMPS].rateLimit == C.RateLimit(50, 60)
    # no tokens yet
    assert fleet.available(C.SET_AMPS) is False and owner.available(C.READ_CHARGE) is True
    assert (owner.readSource.provider, fleet.readSource.provider) == (
        "OwnerAPI",
        "FleetAPI",
    )
    assert (owner.module, fleet.module) == (api, api)


def test_fleet_reads_only_when_owner_cannot():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    owner, fleet = api.providers()
    api.storeToken(fleet_token())
    assert (
        fleet.ready(v, C.READ_CHARGE)
        and fleet.readSource.canRead(v)
        and not owner.ready(v, C.READ_CHARGE)
    )
    api.storeToken(owner_token())
    assert (
        owner.ready(v, C.READ_CHARGE)
        and not fleet.ready(v, C.READ_CHARGE)
        and not fleet.readSource.canRead(v)
    )
    assert fleet.ready(v, C.SET_AMPS) and fleet.ready(v, C.WAKE) and fleet.available(C.WAKE)


def test_claims_are_live_and_follow_the_slots():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})  # both slots empty
    v = reg.get(VIN)
    assert set(v.claims) == {"OwnerAPI", "FleetAPI"} and v.tier == "observed"
    assert v.claims["OwnerAPI"].status == "listed"
    assert v.claims["OwnerAPI"].commands is False
    api.storeToken(fleet_token())
    assert v.tier == "managed"  # pasted after the listing: no re-list needed
    api.fleetSlot.clear()
    assert v.tier == "observed"
    api.storeToken(owner_token())
    assert v.tier == "observed"


def test_fleet_commands_resolve_the_car_by_vin():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    owner, fleet = api.providers()
    api.setChargeRate = Mock(return_value=True)
    api.wakeVehicle = Mock(return_value=True)
    assert fleet.setAmps(VIN, 10) is True
    api.setChargeRate.assert_called_once_with(10, vehicle=car)
    assert fleet.wake(VIN) is True
    api.wakeVehicle.assert_called_once_with(car)
    assert fleet.setAmps("5YJ3E1EA1KF000009", 10) is False
    assert fleet.wake("5YJ3E1EA1KF000009") is False


def test_read_source_passes_its_slot_to_the_car():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    api.storeToken(owner_token())
    v = reg.get(VIN)
    car.statusDeferral = 0
    car.is_awake = Mock(return_value=True)
    car.update_vehicle_data = Mock(return_value=True)
    assert api.ownerReadSource.fetch(v, "chargeLimit", False) == {}
    car.is_awake.assert_called_once_with(slot=api.ownerSlot)
    car.update_vehicle_data.assert_called_once_with(slot=api.ownerSlot)


def test_a_paused_slot_sends_nothing():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    api.storeToken(owner_token())
    api.ownerSlot.pause()
    assert car.get_car_api(VIN, checkReady=False, slot=api.ownerSlot) == (False, None)


VEHICLE_DATA = {
    "response": {
        "drive_state": {"latitude": 37.0, "longitude": -122.0, "shift_state": "P"},
        "charge_state": {"battery_level": 61, "charge_limit_soc": 80},
        "vehicle_state": {},
    }
}


# The Owner read lands; the car is asleep (no vehicle_data at all); the car
# is online but the Owner vehicle_data comes back unusable, so the cascade
# moves on to the Fleet source with the car known awake.
OWNER_READS = {
    "lands": ("online", VEHICLE_DATA),
    "asleep": ("asleep", VEHICLE_DATA),
    "unusable": ("online", {"response": {"charge_state": {}}}),
}


@pytest.mark.parametrize("case", sorted(OWNER_READS))
def test_owner_reads_cost_nothing_with_a_fleet_token_held(requests_mock, case):
    # Both tokens held, Owner can read: a read cascade makes no Fleet request
    # at all, whether or not the Owner read succeeds.
    state, payload = OWNER_READS[case]
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    api.storeToken(fleet_token())
    api.storeToken(owner_token())
    car.statusDeferral = 0
    car.lastVehicleAwakeTime = 0
    car.lastAPIAccessTime = 0
    api.is_location_home = Mock(return_value=True)
    api.is_far_from_home = Mock(return_value=False)
    fleet = requests_mock.register_uri(ANY, FLEET_ANY, json=VEHICLE_DATA)
    online = requests_mock.get(
        OWNER_VEHICLE, json={"response": {"state": state, "vin": VIN}}
    )
    data = requests_mock.get(OWNER_VEHICLE + "/vehicle_data", json=payload)
    ok = car.update_charge()
    assert fleet.call_count == 0
    assert online.call_count == 1
    if case == "lands":
        assert ok is True and car.batteryLevel == 61 and data.call_count == 1
        assert reg.get(VIN).latest("batteryLevel").source == "TeslaAPI"
    else:
        assert ok is False and data.call_count == (0 if case == "asleep" else 1)


def test_fleet_reads_when_owner_holds_no_token(requests_mock):
    # Today's slotFor("read") rule: Fleet's vehicle_data is the only read.
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = api.getCarApiVehicles()[0]
    api.storeToken(fleet_token())
    car.statusDeferral = 0
    car.lastVehicleAwakeTime = 0
    api.is_location_home = Mock(return_value=True)
    api.is_far_from_home = Mock(return_value=False)
    base = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/" + VIN
    online = requests_mock.get(base, json={"response": {"state": "online"}})
    data = requests_mock.get(base + "/vehicle_data", json=VEHICLE_DATA)
    assert car.update_charge() is True and car.batteryLevel == 61
    assert (online.call_count, data.call_count) == (1, 1)
