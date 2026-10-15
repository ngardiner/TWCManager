"""TeslaAPI.loadVehicleList: idempotent, thread-safe vehicle list loading."""

from unittest.mock import Mock, patch

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
    api.carApiVehicles = []  # class-level default is shared
    api.fleetSlot.setTokens(fleet_token())
    api.carApiLastErrorTime = 0
    return api


def resp(text):
    r = Mock()
    r.text = text
    return r


VEHICLES = '{"count": 1, "response": [{"id": 1, "vin": "5YJ3E1EA1KF000001", "display_name": "Car"}]}'


def test_loads_once(no_threads):
    api = new_api()
    with patch("requests.get", return_value=resp(VEHICLES)) as get:
        assert api.loadVehicleList() is True
        assert api.loadVehicleList() is True
    assert get.call_count == 1
    assert [v.VIN for v in api.getCarApiVehicles()] == ["5YJ3E1EA1KF000001"]


def test_failure_returns_false_and_records_error():
    api = new_api()
    with patch("requests.get", return_value=resp('{"error": "boom"}')):
        assert api.loadVehicleList() is False
    assert api.getVehicleCount() == 0
    assert api.carApiLastErrorTime > 0


def test_no_token_makes_no_call():
    api = new_api()
    api.fleetSlot.clear()
    with patch("requests.get") as get:
        assert api.loadVehicleList() is False
    get.assert_not_called()


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
