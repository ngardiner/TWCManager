# tests/unit/test_teslaapi_providers.py
import re
from unittest.mock import Mock

import pytest
from requests_mock import ANY

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from tests.unit.test_teslaapi_registry import api_with_registry, VIN
from tests.unit.teslatokens import fleet_token, owner_token
from tests.unit.vehicle_helpers import commands

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
    assert owner.cost(C.READ_CHARGE) == C.CLOUD_FREE
    # Free, but paced as Fleet (#659)
    assert owner.cost(C.SET_AMPS) == C.OWNER_COMMAND and C.OWNER_COMMAND.price == 0.0
    assert fleet.cost(C.READ_CHARGE) == C.FLEET_DATA
    assert fleet.cost(C.SET_AMPS) == C.FLEET_COMMAND and fleet.cost(C.WAKE) == C.FLEET_WAKE
    assert fleet.declarations()[C.SET_AMPS] == C.Declaration(C.FLEET_COMMAND)   # pacing is the reconciler's MIN_SEND_GAP
    # no tokens yet
    assert fleet.available(C.SET_AMPS) is False and owner.available(C.READ_CHARGE) is True
    assert owner.available(C.SET_AMPS) is False
    assert owner.FIELDS == fleet.FIELDS == C.CHARGE_FIELDS | C.LOCATION_FIELDS
    assert (owner.module, fleet.module) == (api, api)


def test_fleet_reads_only_when_owner_cannot():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    owner, fleet = api.providers()
    api.storeToken(fleet_token())
    assert fleet.ready(v, C.READ_CHARGE) and not owner.ready(v, C.READ_CHARGE)
    api.storeToken(owner_token())
    assert owner.ready(v, C.READ_CHARGE) and not fleet.ready(v, C.READ_CHARGE)
    assert fleet.ready(v, C.SET_AMPS) and fleet.ready(v, C.WAKE) and fleet.available(C.WAKE)


def test_claims_are_live_and_follow_the_slots():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})  # both slots empty
    v = reg.get(VIN)
    assert set(v.claims) == {"OwnerAPI", "FleetAPI"} and not commands(v)
    assert v.claims["OwnerAPI"].status == "listed"
    assert v.claims["OwnerAPI"].commands is False
    api.storeToken(fleet_token())
    assert commands(v)  # pasted after the listing: no re-list needed
    api.fleetSlot.clear()
    assert not commands(v)
    api.storeToken(owner_token())
    assert not commands(v)


def test_fleet_commands_resolve_the_car_by_vin():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    owner, fleet = api.providers()
    api.command = Mock(return_value=O.Outcome(O.DONE))
    api.wakeOutcome = Mock(return_value=O.Outcome(O.DONE))
    fleet.clock = lambda: 1000.0
    assert fleet.setAmps(VIN, 10) == O.Outcome(O.DONE)
    api.command.assert_called_once_with(
        v, "set_charging_amps", {"charging_amps": 10}, slot=api.fleetSlot
    )
    assert fleet.wake(VIN) == O.Outcome(O.DONE)
    api.wakeOutcome.assert_called_once_with(v, now=1000.0, slot=api.fleetSlot)
    assert fleet.setAmps("5YJ3E1EA1KF000009", 10) == O.Outcome(O.UNSUPPORTED, "not listed")
    assert fleet.wake("5YJ3E1EA1KF000009") == O.Outcome(O.UNSUPPORTED, "not listed")
    # Each provider sends with its own slot only (#659)
    api.command.reset_mock()
    assert owner.setAmps(VIN, 10) == O.Outcome(O.DONE)
    api.command.assert_called_once_with(
        v, "set_charging_amps", {"charging_amps": 10}, slot=api.ownerSlot
    )


@pytest.mark.parametrize("which", [0, 1])
def test_each_provider_reads_through_its_own_slot(which):
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.storeToken(owner_token())
    v = reg.get(VIN)
    v.attachment("TeslaAPI").statusDeferral = 0
    api.is_awake = Mock(return_value=True)
    api.update_vehicle_data = Mock(return_value=True)
    slot = (api.ownerSlot, api.fleetSlot)[which]
    assert api.providers()[which].fetch(v, "chargeLimit", allowWake=True) is True
    api.is_awake.assert_called_once_with(v, slot=slot)
    api.update_vehicle_data.assert_called_once_with(v, slot=slot)
    api.is_awake.return_value = False  # asleep: never woken, no paid read
    assert api.providers()[which].fetch(v, "chargeLimit", allowWake=True) is False
    assert api.update_vehicle_data.call_count == 1


def test_a_paused_slot_sends_nothing(requests_mock):
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    api.storeToken(owner_token())
    api.ownerSlot.pause()
    anything = requests_mock.register_uri(
        ANY, ANY, json={"response": {"state": "online"}}
    )
    assert api.get_car_api(reg.get(VIN), VIN, checkReady=False, slot=api.ownerSlot) == (
        False,
        None,
    )
    assert anything.call_count == 0


def test_a_data_only_fleet_token_reads_but_never_commands():
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    v = reg.get(VIN)
    owner, fleet = api.providers()
    api.storeToken(fleet_token(scp=["openid", "vehicle_device_data"]))
    assert fleet.available(C.READ_CHARGE) is True and fleet.ready(v, C.READ_CHARGE)
    assert fleet.available(C.SET_AMPS) is False and fleet.available(C.WAKE) is False
    assert fleet.ready(v, C.SET_AMPS) is False and not commands(v)


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
    car = reg.get(VIN).attachment("TeslaAPI")
    api.storeToken(fleet_token())
    api.storeToken(owner_token())
    car.statusDeferral = 0
    car.onlineCheckedAt = 0
    car.lastAPIAccessTime = 0
    api.is_location_home = Mock(return_value=True)
    api.is_far_from_home = Mock(return_value=False)
    fleet = requests_mock.register_uri(ANY, FLEET_ANY, json=VEHICLE_DATA)
    online = requests_mock.get(
        OWNER_VEHICLE, json={"response": {"state": state, "vin": VIN}}
    )
    data = requests_mock.get(OWNER_VEHICLE + "/vehicle_data", json=payload)
    ok = api.update_charge(reg.get(VIN))
    assert fleet.call_count == 0
    assert online.call_count == 1
    if case == "lands":
        assert ok is True and reg.get(VIN).value("batteryLevel") == 61 and data.call_count == 1
        assert reg.get(VIN).latest("batteryLevel").source == "OwnerAPI"
    else:
        assert ok is False and data.call_count == (0 if case == "asleep" else 1)


def test_fleet_reads_when_owner_holds_no_token(requests_mock):
    # Today's slotFor("read") rule: Fleet's vehicle_data is the only read.
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = reg.get(VIN).attachment("TeslaAPI")
    api.storeToken(fleet_token())
    car.statusDeferral = 0
    car.onlineCheckedAt = 0
    api.is_location_home = Mock(return_value=True)
    api.is_far_from_home = Mock(return_value=False)
    base = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/" + VIN
    online = requests_mock.get(base, json={"response": {"state": "online"}})
    data = requests_mock.get(base + "/vehicle_data", json=VEHICLE_DATA)
    assert api.update_charge(reg.get(VIN)) is True and reg.get(VIN).value("batteryLevel") == 61
    assert (online.call_count, data.call_count) == (1, 1)


def test_a_refused_owner_read_hands_reads_to_fleet(requests_mock):
    # Owner vehicle_data refused (403): the slot pauses for an hour and the
    # Fleet source reads in the same cascade (one poll earlier than
    # slotFor("read") would have). Fleet reuses the Owner's online check from
    # this cascade (the per-car 2-minute awake cache), so its vehicle_data is
    # not preceded by a Fleet vehicles/{vin}. While paused, the Owner slot is
    # not tried again.
    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    car = reg.get(VIN).attachment("TeslaAPI")
    api.storeToken(fleet_token())
    api.storeToken(owner_token())
    car.statusDeferral = 0
    car.onlineCheckedAt = 0
    car.lastAPIAccessTime = 0
    api.is_location_home = Mock(return_value=True)
    api.is_far_from_home = Mock(return_value=False)
    api.master.queue_background_task = Mock()
    base = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/" + VIN
    fleetOnline = requests_mock.get(base, json={"response": {"state": "online"}})
    fleetData = requests_mock.get(base + "/vehicle_data", json=VEHICLE_DATA)
    ownerOnline = requests_mock.get(
        OWNER_VEHICLE, json={"response": {"state": "online", "vin": VIN}}
    )
    ownerData = requests_mock.get(
        OWNER_VEHICLE + "/vehicle_data", status_code=403, text="refused"
    )
    assert api.update_charge(reg.get(VIN)) is True and reg.get(VIN).value("batteryLevel") == 61
    assert (ownerOnline.call_count, ownerData.call_count) == (1, 1)
    assert (fleetOnline.call_count, fleetData.call_count) == (0, 1)
    assert not api.ownerSlot.usable("read")
    # Next poll that must read (maxAge 0): Fleet alone reads
    car.statusDeferral = 0
    assert api.update_location(reg.get(VIN), 0) is True
    assert (ownerOnline.call_count, ownerData.call_count) == (1, 1)
    assert fleetData.call_count == 2
