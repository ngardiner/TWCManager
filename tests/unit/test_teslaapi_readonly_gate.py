"""
Read-only command gate.

Commands follow the vehicle, not only the token. A Fleet API token commands
every listed car. An Owner API token commands only a car whose /products
entry says command_signing "off" (#659), until Tesla answers one of its
commands with a 403. A car marked "required", one with no field and one with
any other value is read-only on an Owner token: no command may reach Tesla
for it. The provider order keeps it out of every command walk, and a provider
asked anyway refuses without an HTTP call and without recording an API error
(which would back off reads too). Reads still run. With both tokens the
provider order decides: OwnerAPI ranks first, so an "off" car's commands go
to the Owner API until Tesla refuses them, and each provider sends with its
own token only. The owner_api fixture's car has no command_signing, so it is
the missing-field case.
"""

import logging
import re
import time
from unittest.mock import Mock

import pytest
import requests_mock as rm

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile.Outcome import DONE, ERROR, REFUSED, Outcome

from tests.unit.teslatokens import (
    FLEET_NA_URL as FLEET_URL,
    OWNER_API_URL as OWNER_URL,
    add_home_vehicle,
    fleet_token,
    new_api,
    no_threads,
    owner_token,
    wire_reads,
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
    # The module-wide INFO hint, once per Fleet slot change (12b review I3),
    # when no token can command any car; with an Owner token each car says
    # why instead. No HTTP call, no API error
    def refusals(self, caplog):
        return [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "read-only" in r.getMessage()
        ]

    def fleet(self, api):
        return [p for p in api.providers() if p.name == "FleetAPI"][0]

    def test_logged_once_per_state_change(self, no_threads, any_http, caplog):
        caplog.set_level(logging.INFO)
        api = new_api()
        add_home_vehicle(api)
        f = self.fleet(api)
        assert not f.available(C.SET_AMPS) and not f.available(C.WAKE)
        assert not f.available(C.STOP_CHARGING)
        assert len(self.refusals(caplog)) == 1
        api.storeToken(fleet_token())
        assert f.available(C.SET_AMPS) and len(self.refusals(caplog)) == 1
        api.fleetSlot.clear()
        f.available(C.SET_AMPS)
        f.available(C.SET_AMPS)
        assert len(self.refusals(caplog)) == 2
        assert any_http.call_count == 0 and no_error_recorded(api)

    def test_owner_token_names_the_car_not_the_module(
        self, owner_api, any_http, caplog
    ):
        caplog.set_level(logging.INFO)
        self.fleet(owner_api).available(C.SET_AMPS)
        owner = provider(owner_api, "OwnerAPI")
        owner.ready(owner_api.registry.get(VIN), C.SET_AMPS)
        said = [r.getMessage() for r in self.refusals(caplog)]
        assert len(said) == 1 and said[0].startswith("Tesla API is read-only for Car;")
        assert any_http.call_count == 0 and no_error_recorded(owner_api)

    def test_resync_of_owner_token_does_not_log_again(
        self, owner_api, any_http, caplog
    ):
        caplog.set_level(logging.INFO)
        owner = provider(owner_api, "OwnerAPI")
        vehicle = owner_api.registry.get(VIN)
        owner.ready(vehicle, C.SET_AMPS)
        owner_api.storeToken(owner_token(jti="rotated"))
        owner.ready(vehicle, C.SET_AMPS)
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
                "response": [
                    {"id": 1, "vin": "5YJ3E1EA1KF000001", "display_name": "Car"}
                ],
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
        # No Fleet token: no car is commanded through Fleet
        assert api.slotFor("command") is None
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


# Owner API commands per vehicle (#659)

VIN = "5YJ3E1EA1KF000001"
VIN2 = "5YJSA7E20JF000002"
VCP_403 = {
    "response": None,
    "error": "Tesla Vehicle Command Protocol required, please refer to the "
    "documentation here: https://developer.tesla.com/docs/fleet-api",
}


def provider(api, name):
    return [p for p in api.providers() if p.name == name][0]


def listed(api, i=0):
    # (registry vehicle, CarApiState) of the i-th listed car, VIN order
    return api.registry.attached("TeslaAPI")[i]


def owner_api_with(signing, vin=VIN, name="Car"):
    api = new_api()
    api.storeToken(owner_token())
    add_home_vehicle(api, command_signing=signing, vin=vin, name=name)
    return api


def bearer(request):
    return request.headers["Authorization"][len("Bearer ") :]


def mock_owner_car(requests_mock, vin=VIN, status=200, body=None, state="online"):
    # The free online check and every command answer for one car
    requests_mock.get(OWNER_URL + "/" + vin, json={"response": {"state": state}})
    return requests_mock.post(
        re.compile(re.escape(OWNER_URL + "/" + vin) + "/.*"),
        status_code=status,
        json=body or {"response": {"result": True, "reason": ""}},
    )


def posts(requests_mock):
    return [q for q in requests_mock.request_history if q.method == "POST"]


class TestOffCarCommandsThroughOwner:
    # command_signing "off": every command goes to the Owner API with the
    # Owner token, through the OwnerAPI provider
    @pytest.mark.parametrize(
        "call,args,command",
        [
            ("setAmps", (VIN, 10), "command/set_charging_amps"),
            ("setChargeLimit", (VIN, 80), "command/set_charge_limit"),
            ("startCharging", (VIN,), "command/charge_start"),
            ("stopCharging", (VIN,), "command/charge_stop"),
        ],
    )
    def test_command_goes_to_owner_api(
        self, no_threads, requests_mock, call, args, command
    ):
        api = owner_api_with("off")
        sent = mock_owner_car(requests_mock)
        owner = provider(api, "OwnerAPI")
        vehicle = api.registry.get(VIN)
        cap = {
            "setAmps": C.SET_AMPS,
            "setChargeLimit": C.SET_CHARGE_LIMIT,
            "startCharging": C.START_CHARGING,
            "stopCharging": C.STOP_CHARGING,
        }[call]
        assert owner.available(cap) and owner.ready(vehicle, cap)
        assert getattr(owner, call)(*args) == Outcome(DONE)
        assert [q.url for q in posts(requests_mock)] == [
            OWNER_URL + "/" + VIN + "/" + command
        ]
        assert bearer(sent.last_request) == api.ownerSlot.accessToken
        assert no_error_recorded(api)

    def test_wake_goes_to_owner_api(self, no_threads, requests_mock, monkeypatch):
        import TWCManager.Vehicle.TeslaAPI as teslaapi

        monkeypatch.setattr(teslaapi.time, "sleep", lambda s: None)
        api = owner_api_with("off")
        wake = mock_owner_car(
            requests_mock, state="asleep", body={"response": {"state": "online"}}
        )
        vehicle, car = listed(api)
        car.lastAPIAccessTime = 0
        # The wake hold is over
        car.firstChargeNeededTime = time.time() - 600
        car.lastChargeNeededTime = time.time()
        owner = provider(api, "OwnerAPI")
        assert owner.ready(vehicle, C.WAKE)
        assert owner.wake(VIN) == Outcome(DONE)
        assert wake.last_request.url == OWNER_URL + "/" + VIN + "/wake_up"
        assert bearer(wake.last_request) == api.ownerSlot.accessToken

    def test_fleet_provider_still_refuses(self, no_threads, requests_mock):
        # Fleet stays Fleet: it never sends with the Owner token
        api = owner_api_with("off")
        fleet = provider(api, "FleetAPI")
        assert not fleet.available(C.SET_AMPS)
        assert fleet.setAmps(VIN, 10) == Outcome(REFUSED, "no command token")
        assert requests_mock.call_count == 0

    def test_module_is_not_read_only(self, no_threads):
        api = owner_api_with("off")
        vehicle, car = listed(api)
        assert api.readOnly is False
        assert api.canCommand(vehicle, api.ownerSlot)

    def test_owner_claim_commands(self, no_threads):
        api = owner_api_with("off")
        vehicle = api.registry.get(VIN)
        assert vehicle.claims["OwnerAPI"].commands is True
        assert vehicle.tier == "managed"

    def test_read_pause_does_not_block_owner_commands(self, no_threads, requests_mock):
        # A paused read slot is paused for reads only
        api = owner_api_with("off")
        api.ownerSlot.pause()
        sent = mock_owner_car(requests_mock)
        assert provider(api, "OwnerAPI").setAmps(VIN, 10) == Outcome(DONE)
        assert sent.call_count == 1

    def test_cleared_owner_slot_blocks(self, no_threads, requests_mock):
        api = owner_api_with("off")
        api.ownerSlot.clear()
        vehicle = api.registry.get(VIN)
        assert not api.canCommand(vehicle, api.ownerSlot)
        assert provider(api, "OwnerAPI").setAmps(VIN, 10) == Outcome(
            REFUSED, "no command token"
        )
        assert requests_mock.call_count == 0


class TestOwnerTokenRefusesOtherCars:
    # "required", a missing field and an unknown value: refused locally
    @pytest.mark.parametrize("signing", ["required", None, "weird"])
    def test_refused_without_http(self, no_threads, requests_mock, signing):
        api = owner_api_with(signing)
        vehicle, car = listed(api)
        owner = provider(api, "OwnerAPI")
        assert api.readOnly is True
        assert not api.canCommand(vehicle, api.ownerSlot)
        for cap in C.COMMANDS:
            assert not owner.ready(vehicle, cap)
        assert owner.setAmps(VIN, 10) == Outcome(REFUSED, "no command token")
        assert owner.setChargeLimit(VIN, 80) == Outcome(REFUSED, "no command token")
        assert owner.wake(VIN) == Outcome(REFUSED, "no command token")
        assert requests_mock.call_count == 0
        assert no_error_recorded(api)
        assert vehicle.claims["OwnerAPI"].commands is False
        assert vehicle.tier == "observed"

    def test_off_must_match_exactly(self, no_threads):
        for signing in ("Off", "OFF", " off", "false", False, 0, ""):
            api = owner_api_with(signing)
            assert not api.canCommand(api.registry.get(VIN), api.ownerSlot)

    def infos(self, caplog):
        return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]

    def test_required_car_is_named_once(self, no_threads, requests_mock, caplog):
        caplog.set_level(logging.INFO)
        api = owner_api_with("required", name="Model 3")
        vehicle = api.registry.get(VIN)
        owner = provider(api, "OwnerAPI")
        for _ in range(3):
            owner.ready(vehicle, C.SET_AMPS)
            owner.ready(vehicle, C.WAKE)
        said = [m for m in self.infos(caplog) if "Model 3" in m]
        assert said == [
            "Tesla API is read-only for Model 3; not sending set_charging_amps "
            "or any other vehicle command to it. Tesla reports Model 3 requires "
            "signed commands (command_signing: required). Charge commands need "
            "a Fleet API token with the Vehicle Command proxy (teslaProxy) or "
            "the TeslaBLE module."
        ]
        assert requests_mock.call_count == 0 and no_error_recorded(api)

    @pytest.mark.parametrize("signing", [None, "weird"])
    def test_unknown_car_is_named_once(self, no_threads, caplog, signing):
        caplog.set_level(logging.INFO)
        api = owner_api_with(signing, name="Model S")
        vehicle = api.registry.get(VIN)
        owner = provider(api, "OwnerAPI")
        owner.ready(vehicle, C.SET_AMPS)
        owner.ready(vehicle, C.SET_AMPS)
        said = [m for m in self.infos(caplog) if "Model S" in m]
        assert len(said) == 1
        assert "treated as read-only" in said[0]
        assert "did not report command_signing: off" in said[0]

    def test_no_per_car_hint_while_fleet_commands(self, no_threads, caplog):
        caplog.set_level(logging.INFO)
        api = owner_api_with("required", name="Model 3")
        api.storeToken(fleet_token())
        provider(api, "OwnerAPI").ready(api.registry.get(VIN), C.SET_AMPS)
        assert not [m for m in self.infos(caplog) if "Model 3" in m]

    def test_owner_token_with_no_cars_listed_is_not_read_only(self, no_threads):
        # Whether it can command is known only once the cars are listed
        api = new_api()
        api.storeToken(owner_token())
        assert api.readOnly is False and api.ownerTokenOnly is True

    def test_module_hint_only_when_no_car_can_command(self, no_threads, caplog):
        caplog.set_level(logging.INFO)
        api = owner_api_with("off")
        provider(api, "FleetAPI").available(C.SET_AMPS)
        assert not [m for m in self.infos(caplog) if "Tesla API is read-only" in m]


class TestMixedFleet:
    # One "off" car and one "required" car on one Owner token: each decided
    # on its own
    def make(self):
        api = owner_api_with("off", name="Old X")
        add_home_vehicle(api, command_signing="required", vin=VIN2, name="New 3", id=2)
        return api

    def test_each_car_decided_separately(self, no_threads, requests_mock):
        api = self.make()
        (off, _), (required, requiredCar) = api.registry.attached("TeslaAPI")
        owner = provider(api, "OwnerAPI")
        assert api.readOnly is False
        assert api.canCommand(off, api.ownerSlot)
        assert not api.canCommand(required, api.ownerSlot)
        assert owner.ready(off, C.SET_AMPS)
        assert not owner.ready(required, C.SET_AMPS)
        mock_owner_car(requests_mock)
        assert owner.setAmps(VIN, 10) == Outcome(DONE)
        assert owner.setAmps(VIN2, 10) == Outcome(REFUSED, "no command token")
        assert [q.url for q in posts(requests_mock)] == [
            OWNER_URL + "/" + VIN + "/command/set_charging_amps"
        ]
        assert no_error_recorded(api) and requiredCar.lastErrorTime == 0


class TestBothTokens:
    def test_off_car_commands_through_owner(self, no_threads, requests_mock):
        # The provider order decides (OwnerAPI ranks before FleetAPI in the
        # same tier): each provider is ready for its own slot only, and
        # sends with its own token
        api = owner_api_with("off")
        api.storeToken(fleet_token())
        vehicle, car = listed(api)
        owner, fleet = provider(api, "OwnerAPI"), provider(api, "FleetAPI")
        assert owner.available(C.SET_AMPS) and owner.ready(vehicle, C.SET_AMPS)
        assert fleet.available(C.SET_AMPS) and fleet.ready(vehicle, C.SET_AMPS)
        order = wire_reads(api.registry, *api.providers())
        names = [p.name for p in order.candidates(vehicle, C.SET_AMPS)]
        assert names == ["OwnerAPI", "FleetAPI"]
        sent = mock_owner_car(requests_mock)
        assert owner.setAmps(VIN, 10) == Outcome(DONE)
        assert bearer(sent.last_request) == api.ownerSlot.accessToken
        cmd = requests_mock.post(
            re.compile(re.escape(FLEET_URL + "/" + VIN) + "/command/.*"),
            json={"response": {"result": True, "reason": ""}},
        )
        assert fleet.setAmps(VIN, 10) == Outcome(DONE)
        assert bearer(cmd.last_request) == api.fleetSlot.accessToken
        assert sent.call_count == 1 and cmd.call_count == 1

    @pytest.mark.parametrize("signing", ["required", None, "weird"])
    def test_other_cars_command_through_fleet_only(
        self, no_threads, requests_mock, caplog, signing
    ):
        caplog.set_level(logging.INFO)
        api = owner_api_with(signing, name="Model 3")
        api.storeToken(fleet_token())
        vehicle, car = listed(api)
        owner, fleet = provider(api, "OwnerAPI"), provider(api, "FleetAPI")
        assert not owner.ready(vehicle, C.SET_AMPS)
        assert not owner.ready(vehicle, C.WAKE)
        assert fleet.ready(vehicle, C.SET_AMPS)
        assert owner.setAmps(VIN, 10) == Outcome(REFUSED, "no command token")
        assert owner.wake(VIN) == Outcome(REFUSED, "no command token")
        assert requests_mock.call_count == 0
        # Fleet commands it: not read-only, so no per-car line
        assert api.readOnly is False
        assert not [r for r in caplog.records if "read-only" in r.getMessage()]

    def test_fleet_token_without_command_scope_leaves_owner(
        self, no_threads, requests_mock
    ):
        # A Fleet token that cannot command is no Fleet command slot: the
        # "off" car's commands go through the Owner token
        api = owner_api_with("off")
        api.storeToken(fleet_token(scp=["openid", "vehicle_device_data"]))
        vehicle, car = listed(api)
        assert not api.fleetSlot.usable("command")
        assert api.canCommand(vehicle, api.ownerSlot)
        assert not api.canCommand(vehicle, api.fleetSlot)
        assert provider(api, "OwnerAPI").ready(vehicle, C.SET_AMPS)
        sent = mock_owner_car(requests_mock)
        assert provider(api, "OwnerAPI").setAmps(VIN, 10) == Outcome(DONE)
        assert bearer(sent.last_request) == api.ownerSlot.accessToken


class TestOwner403Latch:
    # An "off" car whose Owner command Tesla answers 403 Vehicle Command
    # Protocol required: that car alone goes read-only on the Owner token,
    # with no API error backoff and no slot-wide refusal
    def test_latch(self, no_threads, requests_mock, caplog):
        caplog.set_level(logging.INFO)
        api = owner_api_with("off", name="Old X")
        add_home_vehicle(api, command_signing="off", vin=VIN2, name="Other", id=2)
        (vehicle, car), (other, _) = api.registry.attached("TeslaAPI")
        cmd = mock_owner_car(requests_mock, status=403, body=VCP_403)
        owner = provider(api, "OwnerAPI")
        assert owner.setAmps(VIN, 10).kind == REFUSED
        assert cmd.call_count == 1
        assert car.ownerCommandsRefused is True
        assert no_error_recorded(api)
        assert "command" not in api.ownerSlot.refusedPurposes
        assert api.ownerSlot.usable("read")
        # This car drops out of the Owner walk; the other "off" car does not
        assert not owner.ready(vehicle, C.SET_AMPS)
        assert not owner.ready(vehicle, C.WAKE)
        assert vehicle.claims["OwnerAPI"].commands is False
        assert owner.ready(other, C.SET_AMPS)
        # A second command makes no request
        assert owner.setAmps(VIN, 10) == Outcome(REFUSED, "no command token")
        assert owner.wake(VIN) == Outcome(REFUSED, "no command token")
        assert cmd.call_count == 1
        warned = [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.INFO and "Old X" in r.getMessage()
        ]
        assert len(warned) == 1
        assert "teslaProxy" in warned[0] and "TeslaBLE" in warned[0]

    def test_wake_403_latches(self, no_threads, requests_mock):
        api = owner_api_with("off")
        vehicle, car = listed(api)
        car.lastAPIAccessTime = 0
        car.firstChargeNeededTime = time.time() - 600
        car.lastChargeNeededTime = time.time()
        wake = mock_owner_car(requests_mock, state="asleep", status=403, body=VCP_403)
        assert provider(api, "OwnerAPI").wake(VIN).kind == REFUSED
        assert wake.call_count == 1 and car.ownerCommandsRefused is True
        assert no_error_recorded(api)

    def test_read_still_works(self, no_threads, requests_mock):
        api = owner_api_with("off")
        mock_owner_car(requests_mock, status=403, body=VCP_403)
        provider(api, "OwnerAPI").setChargeLimit(VIN, 80)
        data = requests_mock.get(
            OWNER_URL + "/" + VIN + "/vehicle_data",
            json={
                "response": {
                    "drive_state": {"latitude": 37.0, "longitude": -122.0},
                    "charge_state": {"battery_level": 55, "charge_limit_soc": 80},
                    "vehicle_state": {"vehicle_name": "Car"},
                }
            },
        )
        vehicle = api.registry.get(VIN)
        assert api.update_vehicle_data(vehicle) is True
        assert data.call_count == 1 and vehicle.value("batteryLevel") == 55

    @pytest.mark.parametrize("call", ["command", "wake"])
    def test_vcp_error_on_http_200_latches(self, no_threads, requests_mock, call):
        # Finding 3: the VCP error in a 200's body latches as a 403 does,
        # rather than coming back a retryable TypeError
        api = owner_api_with("off")
        vehicle, car = listed(api)
        car.lastAPIAccessTime = 0
        car.firstChargeNeededTime = time.time() - 600
        car.lastChargeNeededTime = time.time()
        state = "asleep" if call == "wake" else "online"
        sent = mock_owner_car(requests_mock, state=state, body=VCP_403)
        owner = provider(api, "OwnerAPI")
        first = owner.wake(VIN) if call == "wake" else owner.setChargeLimit(VIN, 80)
        assert first == Outcome(REFUSED, "signing required")
        assert sent.call_count == 1 and car.ownerCommandsRefused is True
        assert "command" not in api.ownerSlot.refusedPurposes
        assert no_error_recorded(api)
        assert owner.setChargeLimit(VIN, 80) == Outcome(REFUSED, "no command token")
        assert sent.call_count == 1

    def test_fleet_403_keeps_its_handling(self, no_threads, requests_mock):
        # Only the Owner slot latches a car; a Fleet 403 is still Fleet's
        api = owner_api_with("off")
        api.storeToken(fleet_token())
        requests_mock.post(
            re.compile(re.escape(FLEET_URL + "/" + VIN) + "/command/.*"),
            status_code=403,
            json=VCP_403,
        )
        vehicle, car = listed(api)
        assert provider(api, "FleetAPI").setAmps(VIN, 10) == Outcome(REFUSED, "403")
        assert car.ownerCommandsRefused is False


FLEETAPI_ONLY = {
    "response": None,
    "error": "Endpoint is only available on fleetapi. Visit "
    "https://developer.tesla.com/docs for more info",
}
RETIRED = Outcome(REFUSED, "owner commands retired")


class TestOwnerCommandsRetired:
    # Tesla stops taking Owner API commands (410 Gone, or "only available on
    # fleetapi"): a token-wide signal, so the Owner token sends no command or
    # wake again, for any car, until it changes. Reads keep the token; no API
    # error backoff; logged once
    def asleep(self, api):
        vehicle, car = listed(api)
        car.lastAPIAccessTime = 0
        car.firstChargeNeededTime = time.time() - 600
        car.lastChargeNeededTime = time.time()
        return vehicle, car

    @pytest.mark.parametrize(
        "status,body,call",
        [
            (410, {"response": None, "error": "gone"}, "command"),
            (410, {"response": None, "error": "gone"}, "wake"),
            (412, FLEETAPI_ONLY, "command"),
            (403, FLEETAPI_ONLY, "wake"),
        ],
    )
    def test_retired(self, no_threads, requests_mock, caplog, status, body, call):
        caplog.set_level(logging.INFO)
        api = owner_api_with("off", name="Old X")
        add_home_vehicle(api, command_signing="off", vin=VIN2, name="Other", id=2)
        vehicle, car = self.asleep(api)
        other = api.registry.get(VIN2)
        state = "asleep" if call == "wake" else "online"
        sent = mock_owner_car(requests_mock, state=state, status=status, body=body)
        owner = provider(api, "OwnerAPI")
        first = owner.wake(VIN) if call == "wake" else owner.setAmps(VIN, 10)
        assert first == RETIRED
        assert sent.call_count == 1
        assert "command" in api.ownerSlot.refusedPurposes
        assert car.ownerCommandsRefused is False
        assert no_error_recorded(api)
        assert car.delayNextWakeAttempt == 0
        # Every car leaves the Owner walk; nothing more is sent
        for each in (vehicle, other):
            assert not owner.ready(each, C.SET_AMPS)
            assert not owner.ready(each, C.WAKE)
        assert owner.setAmps(VIN2, 10) == Outcome(REFUSED, "no command token")
        assert owner.wake(VIN) == Outcome(REFUSED, "no command token")
        assert sent.call_count == 1
        assert api.readOnly is True
        # One line stands for every car's
        said = [
            r.getMessage()
            for r in caplog.records
            if "Owner API" in r.getMessage() or "read-only for" in r.getMessage()
        ]
        assert len(said) == 1
        assert "no longer accepts Owner API commands" in said[0]
        assert "replaced" not in said[0]
        # Reads keep the token
        assert api.ownerSlot.usable("read")
        data = requests_mock.get(
            OWNER_URL + "/" + VIN + "/vehicle_data",
            json={
                "response": {
                    "drive_state": {"latitude": 37.0, "longitude": -122.0},
                    "charge_state": {"battery_level": 55, "charge_limit_soc": 80},
                    "vehicle_state": {"vehicle_name": "Car"},
                }
            },
        )
        assert api.update_vehicle_data(vehicle) is True
        assert data.call_count == 1 and vehicle.value("batteryLevel") == 55

    def test_retired_with_a_fleet_token_says_fleet_carries(
        self, no_threads, requests_mock, caplog
    ):
        # Owner ranks ahead of Fleet for an "off" car; once Tesla retires
        # Owner commands the user is told Fleet carries them, not that a
        # Fleet token is needed
        caplog.set_level(logging.INFO)
        api = owner_api_with("off")
        api.storeToken(fleet_token())
        self.asleep(api)
        mock_owner_car(
            requests_mock, status=410, body={"response": None, "error": "gone"}
        )
        assert provider(api, "OwnerAPI").setAmps(VIN, 10) == RETIRED
        said = [r for r in caplog.records if "no longer accepts" in r.getMessage()]
        assert len(said) == 1 and said[0].levelno == logging.ERROR
        assert "Fleet API carries" in said[0].getMessage()
        assert "need a Fleet API token" not in said[0].getMessage()
        assert api.canCommand(api.registry.get(VIN), api.fleetSlot)

    def test_retired_on_http_200(self, no_threads, requests_mock):
        api = owner_api_with("off")
        sent = mock_owner_car(requests_mock, body=FLEETAPI_ONLY)
        assert provider(api, "OwnerAPI").setAmps(VIN, 10) == RETIRED
        assert sent.call_count == 1
        assert "command" in api.ownerSlot.refusedPurposes
        assert no_error_recorded(api)

    @pytest.mark.parametrize("body", [{"response": None}, FLEETAPI_ONLY])
    def test_fleet_410_keeps_its_handling(self, no_threads, requests_mock, body):
        # A Fleet 410 stays a plain, retryable error: nothing is retired
        api = owner_api_with("off")
        api.storeToken(fleet_token())
        requests_mock.post(
            re.compile(re.escape(FLEET_URL + "/" + VIN) + "/command/.*"),
            status_code=410,
            json=body,
        )
        assert provider(api, "FleetAPI").setAmps(VIN, 10) == Outcome(ERROR, "410")
        assert api.fleetSlot.refusedPurposes == set()
        assert api.ownerSlot.refusedPurposes == set()

    def test_per_car_line_names_the_token(self, no_threads, requests_mock, caplog):
        # Finding 2: the per-car line says the Owner token was refused, not
        # what the car's signing says; and it is logged again on the change
        caplog.set_level(logging.INFO)
        api = owner_api_with("required", name="Old X")
        owner = provider(api, "OwnerAPI")
        vehicle = api.registry.get(VIN)
        assert not owner.ready(vehicle, C.SET_AMPS)
        # As an expired token that could not be refreshed does
        api.ownerSlot.refuse("command")
        for _ in range(3):
            assert not owner.ready(vehicle, C.SET_AMPS)
        said = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "read-only for Old X" in r.getMessage()
        ]
        assert len(said) == 2
        assert "command_signing: required" in said[0]
        assert "Tesla refused commands with the Owner API token" in said[1]
        assert "command_signing" not in said[1]


class TestThroughTheWalk:
    # The reconciler's real walk over the real TeslaAPI providers, with a
    # lower-priority provider behind them
    def make(self, *signings, fleet=False):
        from tests.unit.reconcile_helpers import ScriptedProvider
        from tests.unit.test_limit_reconciler import setup

        m, r, v, c = setup(clock=False)
        api = new_api(master=m)
        api.storeToken(owner_token())
        if fleet:
            api.storeToken(fleet_token())
        for p in api.providers():
            m.providerOrder.register(p)
        last = ScriptedProvider("Fallback", 9, False, [Outcome(DONE)], tier=C.HA_BILLED)
        m.providerOrder.register(last)
        vins = (VIN, VIN2)
        for i, signing in enumerate(signings):
            add_home_vehicle(
                api, command_signing=signing, vin=vins[i], name="Car%d" % i, id=i + 1
            )
            m.vehicleRegistry.get(vins[i]).observe("chargeLimit", 90, "TeslaAPI")
        return m, r, api, last

    def limit(self, m, r, *vins, passes=3):
        from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT

        for vin in vins:
            m.desired.set(vin, CHARGE_LIMIT, 50, "policy")
        for _ in range(passes):
            r.pass_()

    def test_mixed_fleet(self, no_threads, requests_mock):
        m, r, api, last = self.make("off", "required")
        mock_owner_car(requests_mock)
        mock_owner_car(requests_mock, vin=VIN2)
        self.limit(m, r, VIN, VIN2)
        sent = [q.url for q in posts(requests_mock)]
        assert sent == [OWNER_URL + "/" + VIN + "/command/set_charge_limit"]
        # The "required" car got no request from TeslaAPI and fell through
        assert last.calls == [("setChargeLimit", VIN2, 50)]
        assert no_error_recorded(api)

    def test_403_falls_through(self, no_threads, requests_mock):
        from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT

        m, r, api, last = self.make("off")
        cmd = mock_owner_car(requests_mock, status=403, body=VCP_403)
        self.limit(m, r, VIN)
        assert cmd.call_count == 1
        assert listed(api)[1].ownerCommandsRefused is True
        assert no_error_recorded(api)
        # The Owner provider drops out for this car; the next one stays
        v = m.vehicleRegistry.get(VIN)
        able = [p.name for p in m.providerOrder.candidates(v, C.SET_CHARGE_LIMIT)]
        assert able == ["Fallback"]
        # The free refusal changed nothing and spent no paid send: the next
        # paid provider sends on the next pass, without waiting RETRY
        assert last.calls == [("setChargeLimit", VIN, 50)]
        rec = m.desired.record(VIN, CHARGE_LIMIT)
        assert rec.paidSends == 1 and rec.lastProvider == "Fallback"
        assert cmd.call_count == 1 and no_error_recorded(api)

    @pytest.mark.parametrize(
        "status,body", [(410, {"response": None}), (412, FLEETAPI_ONLY)]
    )
    def test_retired_falls_through(self, no_threads, requests_mock, status, body):
        from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT

        m, r, api, last = self.make("off")
        cmd = mock_owner_car(requests_mock, status=status, body=body)
        self.limit(m, r, VIN)
        assert cmd.call_count == 1
        assert "command" in api.ownerSlot.refusedPurposes
        assert no_error_recorded(api)
        assert last.calls == [("setChargeLimit", VIN, 50)]
        # A second desire, past the send gap, sends nothing to the Owner API
        m.vehicleRegistry.get(VIN).observe("chargeLimit", 50, "TeslaAPI")
        m.desired.set(VIN, CHARGE_LIMIT, 60, "policy")
        m.desired.record(VIN, CHARGE_LIMIT).lastSentAt -= 60
        for _ in range(3):
            r.pass_()
        assert cmd.call_count == 1
        assert last.calls[-1] == ("setChargeLimit", VIN, 60)
        assert no_error_recorded(api)

    def fleet_car(self, requests_mock, vin=VIN):
        requests_mock.get(FLEET_URL + "/" + vin, json={"response": {"state": "online"}})
        return requests_mock.post(
            re.compile(re.escape(FLEET_URL + "/" + vin) + "/command/.*"),
            json={"response": {"result": True, "reason": ""}},
        )

    def test_both_tokens_go_to_owner(self, no_threads, requests_mock):
        # The provider order decides: OwnerAPI ranks first, and nothing is
        # sent with the Fleet token
        m, r, api, last = self.make("off", fleet=True)
        cmd = self.fleet_car(requests_mock)
        sent = mock_owner_car(requests_mock)
        self.limit(m, r, VIN)
        assert [q.url for q in posts(requests_mock)] == [
            OWNER_URL + "/" + VIN + "/command/set_charge_limit"
        ]
        assert bearer(sent.last_request) == api.ownerSlot.accessToken
        assert cmd.call_count == 0 and last.calls == []

    @pytest.mark.parametrize(
        "status,body",
        [
            (403, VCP_403),
            (200, VCP_403),
            (410, {"response": None}),
            (412, FLEETAPI_ONLY),
            (200, FLEETAPI_ONLY),
        ],
    )
    def test_both_tokens_owner_refusal_hands_to_fleet(
        self, no_threads, requests_mock, status, body
    ):
        # A refused Owner command is not sent and excludes the provider, so
        # FleetAPI sends on the next pass without waiting RETRY
        from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT

        m, r, api, last = self.make("off", fleet=True)
        cmd = self.fleet_car(requests_mock)
        sent = mock_owner_car(requests_mock, status=status, body=body)
        m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
        r.pass_()
        assert sent.call_count == 1 and cmd.call_count == 0
        r.pass_()
        assert cmd.call_count == 1
        assert bearer(cmd.last_request) == api.fleetSlot.accessToken
        assert sent.call_count == 1 and last.calls == []
        rec = m.desired.record(VIN, CHARGE_LIMIT)
        assert rec.paidSends == 1 and rec.lastProvider == "FleetAPI"
        assert no_error_recorded(api)
        # Later desires go to Fleet only
        m.vehicleRegistry.get(VIN).observe("chargeLimit", 50, "TeslaAPI")
        m.desired.set(VIN, CHARGE_LIMIT, 60, "policy")
        m.desired.record(VIN, CHARGE_LIMIT).lastSentAt -= 60
        for _ in range(3):
            r.pass_()
        assert sent.call_count == 1 and cmd.call_count == 2

    @pytest.mark.parametrize("signing", ["required", None, "weird"])
    def test_both_tokens_other_cars_go_to_fleet(
        self, no_threads, requests_mock, signing
    ):
        m, r, api, last = self.make(signing, fleet=True)
        cmd = self.fleet_car(requests_mock)
        sent = mock_owner_car(requests_mock)
        self.limit(m, r, VIN)
        assert sent.call_count == 0
        assert [q.url for q in posts(requests_mock)] == [
            FLEET_URL + "/" + VIN + "/command/set_charge_limit"
        ]
        assert bearer(cmd.last_request) == api.fleetSlot.accessToken
        assert last.calls == []


class TestVehicleEndpointPerCar:
    # The distributor budgets power only for a car some provider can set
    # amps on: an "off" car on an Owner token is one, a "required" car not
    def test_is_read_only_per_car(self, no_threads):
        from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint

        api = owner_api_with("off")
        add_home_vehicle(api, command_signing="required", vin=VIN2, id=2)
        order = wire_reads(api.registry, *api.providers())
        master = Mock()
        master.config = {"config": {}}
        off, required = (v for v, _ in api.registry.attached("TeslaAPI"))
        assert VehicleEndpoint(off, order, master).isReadOnly is False
        assert VehicleEndpoint(off, order, master).provider == "OwnerAPI"
        assert VehicleEndpoint(required, order, master).isReadOnly is True
