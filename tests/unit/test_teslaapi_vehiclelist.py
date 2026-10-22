"""TeslaAPI.loadVehicleList: idempotent, thread-safe vehicle list loading."""

from unittest.mock import Mock

from tests.unit.teslatokens import (
    FLEET_NA_URL,
    OWNER_API_URL,
    fleet_token,
    no_threads,
    owner_token,
)
from TWCManager.Vehicle.TeslaAPI import TeslaAPI


def new_api():
    master = Mock()
    master.config = {"config": {}, "vehicle": {"teslaAPI": {"enabled": True}}}
    api = TeslaAPI(master)
    api.fleetSlot.setTokens(fleet_token())
    api.carApiLastErrorTime = 0
    return api


VEHICLES = '{"count": 1, "response": [{"id": 1, "vin": "5YJ3E1EA1KF000001", "display_name": "Car"}]}'


def test_loads_once(no_threads, requests_mock):
    api = new_api()
    get = requests_mock.get(FLEET_NA_URL, text=VEHICLES)
    assert api.loadVehicleList() is True
    assert api.loadVehicleList() is True
    assert get.call_count == 1
    assert [v.vin for v, _ in api.registry.attached("TeslaAPI")] == [
        "5YJ3E1EA1KF000001"
    ]


def test_failure_returns_false_and_records_error(requests_mock):
    api = new_api()
    requests_mock.get(FLEET_NA_URL, text='{"error": "boom"}')
    assert api.loadVehicleList() is False
    assert len(api.registry.attached("TeslaAPI")) == 0
    assert api.carApiLastErrorTime > 0


def test_no_token_makes_no_call(requests_mock):
    api = new_api()
    api.fleetSlot.clear()
    assert api.loadVehicleList() is False
    assert requests_mock.request_history == []


def test_lists_from_owner_products_when_both_slots_present(no_threads, requests_mock):
    # Telemetry calls loadVehicleList() directly.
    api = new_api()
    api.ownerSlot.setTokens(owner_token())
    products = requests_mock.get(OWNER_API_URL[:-8] + "products", text=VEHICLES)
    fleet = requests_mock.get(FLEET_NA_URL, text=VEHICLES)
    assert api.loadVehicleList() is True
    assert products.call_count == 1
    assert fleet.call_count == 0
    assert products.last_request.headers["Authorization"] == (
        "Bearer " + api.ownerSlot.accessToken
    )


def test_refused_list_pauses_slot_without_error_or_retry(requests_mock):
    api = new_api()
    vehicles = requests_mock.get(FLEET_NA_URL, status_code=412)
    assert api.loadVehicleList() is False
    assert vehicles.call_count == 1
    assert api.fleetSlot.accessToken != ""
    assert api.slotFor("read") is None
    assert api.carApiLastErrorTime == 0
