"""TeslaAPI.loadVehicleList: idempotent, thread-safe vehicle list loading."""

from unittest.mock import Mock, patch

from TWCManager.Vehicle.TeslaAPI import TeslaAPI


def new_api():
    master = Mock()
    master.config = {"config": {}, "vehicle": {"teslaAPI": {"enabled": True}}}
    api = TeslaAPI(master)
    api.carApiVehicles = []  # class-level default is shared
    api.carApiBearerToken = "tok"
    api.carApiLastErrorTime = 0
    return api


def resp(text):
    r = Mock()
    r.text = text
    return r


VEHICLES = '{"count": 1, "response": [{"id": 1, "vin": "VIN1", "display_name": "Car"}]}'


def test_loads_once():
    api = new_api()
    with patch("requests.get", return_value=resp(VEHICLES)) as get:
        assert api.loadVehicleList() is True
        assert api.loadVehicleList() is True
    assert get.call_count == 1
    assert [v.VIN for v in api.getCarApiVehicles()] == ["VIN1"]


def test_failure_returns_false_and_records_error():
    api = new_api()
    with patch("requests.get", return_value=resp('{"error": "boom"}')):
        assert api.loadVehicleList() is False
    assert api.getVehicleCount() == 0
    assert api.carApiLastErrorTime > 0


def test_no_token_makes_no_call():
    api = new_api()
    api.carApiBearerToken = ""
    with patch("requests.get") as get:
        assert api.loadVehicleList() is False
    get.assert_not_called()
