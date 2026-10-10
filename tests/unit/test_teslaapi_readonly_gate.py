"""
Read-only command gate.

While TeslaAPI holds an Owner API token (readOnly), no vehicle command may
reach Tesla: VehiclePriority skips TeslaAPI for command methods, and
TeslaAPI's own command entry points refuse without an HTTP call and without
recording an API error (which would back off reads too). Reads still run.
"""

import logging
import re
import time
from unittest.mock import Mock

import pytest
import requests_mock as rm

from tests.unit.teslatokens import fleet_token, new_api, owner_token

OWNER_URL = "https://owner-api.teslamotors.com/api/1/vehicles"


class _NoThread:
    daemon = False

    def start(self):
        pass


@pytest.fixture
def no_threads(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi, "Thread", lambda *a, **k: _NoThread())


@pytest.fixture
def owner_api(no_threads):
    api = new_api()
    api.setCarApiBearerToken(owner_token())
    api.addVehicle({"id": 1, "vin": "VIN1", "display_name": "Car"})
    vehicle = api.getCarApiVehicles()[0]
    vehicle.atHome = True
    # Recently contacted, so ready() is True without an HTTP call.
    vehicle.lastAPIAccessTime = time.time()
    return api


@pytest.fixture
def any_http(requests_mock):
    return requests_mock.register_uri(rm.ANY, rm.ANY, json={})


def no_error_recorded(api):
    vehicle = api.getCarApiVehicles()[0]
    return api.getCarApiLastErrorTime() == 0 and vehicle.lastErrorTime == 0


class TestTeslaAPIRefusesCommands:
    def test_set_charge_rate(self, owner_api, any_http):
        vehicle = owner_api.getCarApiVehicles()[0]
        before = vehicle.lastAPIAccessTime
        assert owner_api.setChargeRate(10, vehicle) is False
        assert any_http.call_count == 0
        assert no_error_recorded(owner_api)
        assert vehicle.lastAPIAccessTime == before

    def test_start_and_stop_charge(self, owner_api, any_http):
        assert owner_api.car_api_charge({"charge": True}) == "error"
        assert owner_api.car_api_charge({"charge": False}) == "error"
        assert any_http.call_count == 0
        assert no_error_recorded(owner_api)

    def test_apply_charge_limit(self, owner_api, any_http):
        assert owner_api.applyChargeLimit(80) == "error"
        assert owner_api.applyChargeLimit(80, checkArrival=True) == "error"
        vehicle = owner_api.getCarApiVehicles()[0]
        assert vehicle.apply_charge_limit(80) is False
        assert any_http.call_count == 0
        assert no_error_recorded(owner_api)

    def test_wake(self, owner_api, any_http):
        vehicle = owner_api.getCarApiVehicles()[0]
        assert owner_api.wakeVehicle(vehicle) is False
        assert any_http.call_count == 0
        assert no_error_recorded(owner_api)

    def test_car_api_available_skips_wake_loop(self, owner_api, any_http):
        vehicle = owner_api.getCarApiVehicles()[0]
        vehicle.lastAPIAccessTime = 0  # asleep as far as we know
        owner_api.car_api_available(charge=True)
        owner_api.car_api_available(applyLimit=True)
        assert any_http.call_count == 0
        assert no_error_recorded(owner_api)

    def test_fleet_token_is_not_refused(self, owner_api, requests_mock):
        owner_api.setCarApiBearerToken(fleet_token())
        cmd = requests_mock.post(
            re.compile(r".*/VIN1/command/set_charging_amps"),
            json={"response": {"result": True, "reason": ""}},
        )
        vehicle = owner_api.getCarApiVehicles()[0]
        assert owner_api.setChargeRate(10, vehicle) == {
            "response": {"result": True, "reason": ""}
        }
        assert cmd.call_count == 1


class TestRefusalLogging:
    def refusals(self, caplog):
        return [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "read-only" in r.getMessage()
            and "not sending" in r.getMessage()
        ]

    def test_logged_once_per_state_change(self, owner_api, any_http, caplog):
        caplog.set_level(logging.INFO)
        vehicle = owner_api.getCarApiVehicles()[0]
        owner_api.setChargeRate(10, vehicle)
        owner_api.setChargeRate(12, vehicle)
        owner_api.car_api_charge({"charge": True})
        assert len(self.refusals(caplog)) == 1

        # Fleet token: commands allowed. Back to Owner: logs again, once.
        owner_api.setCarApiBearerToken(fleet_token())
        owner_api.setCarApiBearerToken(owner_token())
        owner_api.setChargeRate(10, vehicle)
        owner_api.setChargeRate(10, vehicle)
        assert len(self.refusals(caplog)) == 2

    def test_resync_of_owner_token_does_not_log_again(
        self, owner_api, any_http, caplog
    ):
        caplog.set_level(logging.INFO)
        vehicle = owner_api.getCarApiVehicles()[0]
        owner_api.setChargeRate(10, vehicle)
        owner_api.setCarApiBearerToken(owner_token(jti="rotated"))
        owner_api.setChargeRate(10, vehicle)
        assert len(self.refusals(caplog)) == 1


class TestReadsStillRun:
    def test_vehicle_data_read_uses_owner_api(self, owner_api, requests_mock):
        data = requests_mock.get(
            OWNER_URL + "/VIN1/vehicle_data",
            json={
                "response": {
                    "drive_state": {"latitude": 37.0, "longitude": -122.0},
                    "charge_state": {"battery_level": 55, "charge_limit_soc": 80},
                    "vehicle_state": {"vehicle_name": "Car"},
                }
            },
        )
        vehicle = owner_api.getCarApiVehicles()[0]
        assert vehicle.update_vehicle_data() is True
        assert data.call_count == 1
        assert vehicle.batteryLevel == 55

    def test_vehicle_list_read_uses_owner_api(self, no_threads, requests_mock):
        api = new_api()
        api.setCarApiBearerToken(owner_token())
        listing = requests_mock.get(
            OWNER_URL,
            json={
                "count": 1,
                "response": [{"id": 1, "vin": "VIN1", "display_name": "Car"}],
            },
        )
        assert api.car_api_available() is True
        assert listing.call_count == 1
        assert api.getVehicleCount() == 1


class TestVehiclePrioritySkipsReadOnly:
    def make(self, api_read_only):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}}
        master.config = {}
        master.settings = {}
        ble = Mock()
        ble.setChargeRate = Mock(return_value=False)
        ble.updateChargeAtHome = Mock(return_value=False)
        api = Mock()
        api.readOnly = api_read_only
        api.setChargeRate = Mock(return_value=True)
        api.updateChargeAtHome = Mock(return_value=True)
        master.getModuleByPriority = Mock(
            side_effect=[("TeslaBLE", ble, 20), ("TeslaAPI", api, 10), ("", None, 0)]
        )
        return VehiclePriority(master), master, ble, api

    def test_read_only_api_skipped_for_commands(self):
        priority, master, ble, api = self.make(api_read_only=True)
        assert priority.setChargeRate(10) is False
        assert ble.setChargeRate.called
        assert not api.setChargeRate.called
        assert "TeslaAPI" not in master.stats["moduleFailures"]

    def test_read_only_api_used_for_reads(self):
        priority, master, ble, api = self.make(api_read_only=True)
        assert priority.updateChargeAtHome() is True
        assert api.updateChargeAtHome.called

    def test_writable_api_used_for_commands(self):
        priority, master, ble, api = self.make(api_read_only=False)
        assert priority.setChargeRate(10) is True
        assert api.setChargeRate.called

    @pytest.mark.parametrize(
        "method", ["car_api_charge", "applyChargeLimit", "wakeVehicle"]
    )
    def test_every_command_method_skips(self, method):
        priority, master, ble, api = self.make(api_read_only=True)
        setattr(ble, method, Mock(return_value=False))
        setattr(api, method, Mock(return_value=True))
        getattr(priority, method)()
        assert not getattr(api, method).called


class TestTeslaAPIEVSE:
    def make_evse(self, carapi):
        from TWCManager.EVSEInstance.TeslaAPIEVSE import TeslaAPIEVSE

        master = Mock()
        master.config = {"config": {}}
        master.getModuleByName = Mock(return_value=carapi)
        return TeslaAPIEVSE(Mock(VIN="VIN1"), Mock(), master)

    def test_read_only_follows_tesla_api(self, owner_api):
        evse = self.make_evse(owner_api)
        assert evse.isReadOnly is True
        owner_api.setCarApiBearerToken(fleet_token())
        assert evse.isReadOnly is False

    def test_not_read_only_without_tesla_api(self):
        assert self.make_evse(None).isReadOnly is False
