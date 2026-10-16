"""
Read-only command gate.

While TeslaAPI holds no usable Fleet API token (readOnly), e.g. only an
Owner API token, no vehicle command may reach Tesla: the provider order keeps
FleetAPI out of every command walk, and
the Fleet provider's commands refuse without an HTTP call and without
recording an API error (which would back off reads too). Reads still run.
"""

import logging
import re
from unittest.mock import Mock

import pytest
import requests_mock as rm

from TWCManager.Vehicle.Reconcile.Outcome import DONE, REFUSED, Outcome

from tests.unit.teslatokens import (
    OWNER_API_URL as OWNER_URL,
    add_home_vehicle,
    fleet_token,
    new_api,
    no_threads,
    owner_token,
)


@pytest.fixture
def owner_api(no_threads):
    api = new_api()
    api.storeToken(owner_token())
    add_home_vehicle(api)
    return api


@pytest.fixture
def any_http(requests_mock):
    return requests_mock.register_uri(rm.ANY, rm.ANY, json={})


def no_error_recorded(api):
    car = api.registry.attached("TeslaAPI")[0][1]
    return api.getCarApiLastErrorTime() == 0 and car.lastErrorTime == 0


class TestFleetProviderRefusesCommands:
    VIN = "5YJ3E1EA1KF000001"

    @pytest.mark.parametrize("call", ["setAmps", "wake"])
    def test_no_command_token_sends_nothing(self, owner_api, any_http, call):
        fleet = [p for p in owner_api.providers() if p.name == "FleetAPI"][0]
        args = (self.VIN, 10) if call == "setAmps" else (self.VIN,)
        car = owner_api.registry.get(self.VIN).attachment("TeslaAPI")
        before = car.lastAPIAccessTime
        assert getattr(fleet, call)(*args) == Outcome(REFUSED, "no command token")
        assert car.lastAPIAccessTime == before
        assert any_http.call_count == 0
        assert no_error_recorded(owner_api)

    def test_fleet_token_is_not_refused(self, owner_api, requests_mock):
        owner_api.storeToken(fleet_token())
        cmd = requests_mock.post(
            re.compile(r".*/5YJ3E1EA1KF000001/command/set_charging_amps"),
            json={"response": {"result": True, "reason": ""}},
        )
        fleet = [p for p in owner_api.providers() if p.name == "FleetAPI"][0]
        assert fleet.setAmps(self.VIN, 10) == Outcome(DONE)
        assert cmd.call_count == 1


class TestRefusalHint:
    # The INFO hint an Owner-only install gets, once per Fleet slot change
    # (12b review I3); no HTTP call, no API error
    def refusals(self, caplog):
        return [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "read-only" in r.getMessage()
        ]

    def fleet(self, api):
        return [p for p in api.providers() if p.name == "FleetAPI"][0]

    def test_logged_once_per_state_change(self, owner_api, any_http, caplog):
        from TWCManager.Vehicle.Providers import Capability as C

        caplog.set_level(logging.INFO)
        f = self.fleet(owner_api)
        assert not f.available(C.SET_AMPS) and not f.available(C.WAKE)
        assert not f.available(C.STOP_CHARGING)
        assert len(self.refusals(caplog)) == 1
        owner_api.storeToken(fleet_token())
        assert f.available(C.SET_AMPS) and len(self.refusals(caplog)) == 1
        owner_api.fleetSlot.clear()
        f.available(C.SET_AMPS)
        f.available(C.SET_AMPS)
        assert len(self.refusals(caplog)) == 2
        assert any_http.call_count == 0 and no_error_recorded(owner_api)

    def test_resync_of_owner_token_does_not_log_again(
        self, owner_api, any_http, caplog
    ):
        from TWCManager.Vehicle.Providers import Capability as C

        caplog.set_level(logging.INFO)
        f = self.fleet(owner_api)
        f.available(C.SET_AMPS)
        owner_api.storeToken(owner_token(jti="rotated"))
        f.available(C.SET_AMPS)
        assert len(self.refusals(caplog)) == 1


class TestReadsStillRun:
    def test_vehicle_data_read_uses_owner_api(self, owner_api, requests_mock):
        data = requests_mock.get(
            OWNER_URL + "/5YJ3E1EA1KF000001/vehicle_data",
            json={
                "response": {
                    "drive_state": {"latitude": 37.0, "longitude": -122.0},
                    "charge_state": {"battery_level": 55, "charge_limit_soc": 80},
                    "vehicle_state": {"vehicle_name": "Car"},
                }
            },
        )
        vehicle = owner_api.registry.get("5YJ3E1EA1KF000001")
        assert owner_api.update_vehicle_data(vehicle) is True
        assert data.call_count == 1
        assert vehicle.value("batteryLevel") == 55

    def test_vehicle_list_read_uses_owner_api(self, no_threads, requests_mock):
        api = new_api()
        api.storeToken(owner_token())
        # The Owner API lists vehicles under /products, not /vehicles.
        listing = requests_mock.get(
            OWNER_URL[: -len("vehicles")] + "products",
            json={
                "count": 1,
                "response": [{"id": 1, "vin": "5YJ3E1EA1KF000001", "display_name": "Car"}],
            },
        )
        assert api.loadVehicleList() is True
        assert listing.call_count == 1
        assert len(api.registry.attached("TeslaAPI")) == 1


class TestCheckChargeRefreshesEachModuleOnce:
    # checkCharge walks the provider order: each vehicle module with a
    # provider not suppressed for reads refreshes its cars once
    def make(self, settings=None, ha=None):
        from tests.unit.test_teslaapi_registry import api_with_registry
        from TWCManager.TWCMaster import TWCMaster
        from TWCManager.Vehicle.Providers import Capability as C
        from TWCManager.Vehicle.Providers.Order import ProviderOrder
        from TWCManager.Vehicle.Providers.Provider import Provider

        api, reg = api_with_registry()
        api.storeToken(owner_token())
        assert api.readOnly is True
        api.updateChargeAtHome = Mock()
        ble = Mock()

        class BLE(Provider):
            name, rank, local, module = "TeslaBLE", 2, True, ble
            SUPPRESSED = {"api_only": C.ALL}

            def declarations(self):
                return {C.READ_CHARGE: C.Declaration(C.LOCAL)}

        master = Mock()
        master.config = {}
        master.settings = settings or {}
        master.vehicleRegistry = reg
        master.providerOrder = ProviderOrder(master)
        for p in [BLE()] + api.providers():
            master.providerOrder.register(p)
        if ha is not None:
            from TWCManager.Vehicle.Providers.HomeAssistantProvider import (
                HomeAssistantProvider,
            )

            master.providerOrder.register(HomeAssistantProvider(ha))
        TWCMaster.updateChargeAtHome(master)
        return ble, api

    def test_read_only_api_still_refreshes_once(self, no_threads):
        ble, api = self.make()
        assert api.updateChargeAtHome.call_count == 1
        assert ble.updateChargeAtHome.call_count == 1

    def test_api_only_keeps_ble_out(self, no_threads):
        ble, api = self.make({"commandPolicy": "api_only"})
        assert api.updateChargeAtHome.call_count == 1
        assert not ble.updateChargeAtHome.called

    def test_home_assistant_refreshes_once_alongside_teslaapi(self, no_threads):
        # 12b deleted-test GAP: HA is refreshed even when TeslaAPI holds cars
        ha = Mock()
        ha.enabled.return_value = True
        ble, api = self.make(ha=ha)
        assert ha.updateChargeAtHome.call_count == 1
        assert api.updateChargeAtHome.call_count == 1
