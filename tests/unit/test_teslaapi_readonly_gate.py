"""
Read-only command gate.

Without a usable Fleet API token, TeslaAPI commands a vehicle with an Owner
API token only when Tesla's /products entry for that vehicle says
command_signing "off". Every other vehicle (signing "required", the field
missing, or a value never seen) is read-only, as is every vehicle when no
token can command: no vehicle command may reach Tesla. The provider order
keeps TeslaAPI out of a command walk for a read-only car (OwnerAPI is not
ready for it) and out of every broadcast while every listed car is
read-only (neither provider is available), and the providers' commands
refuse per vehicle without an HTTP call and without recording an API error
(which would back off reads too). Reads still run.
"""

import logging
import re
import time
from unittest.mock import Mock

import pytest
import requests_mock as rm

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile.Outcome import DONE, REFUSED, UNSUPPORTED, Outcome

from tests.unit.teslatokens import (
    FLEET_NA_URL as FLEET_URL,
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
    vehicle = api.carApiVehicles[0]
    return api.getCarApiLastErrorTime() == 0 and vehicle.lastErrorTime == 0


class TestFleetProviderRefusesCommands:
    VIN = "5YJ3E1EA1KF000001"

    @pytest.mark.parametrize("call", ["setAmps", "wake"])
    def test_no_command_token_sends_nothing(self, owner_api, any_http, call):
        fleet = [p for p in owner_api.providers() if p.name == "FleetAPI"][0]
        args = (self.VIN, 10) if call == "setAmps" else (self.VIN,)
        car = owner_api.carApiVehicles[0]
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
        vehicle = owner_api.carApiVehicles[0]
        assert vehicle.update_vehicle_data() is True
        assert data.call_count == 1
        assert vehicle.batteryLevel == 55

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
        assert len(api.carApiVehicles) == 1


class TestCheckChargeRefreshesEachModuleOnce:
    # checkCharge walks the provider order: each vehicle module with a
    # provider not suppressed for reads refreshes its cars once
    def make(self, settings=None, ha=None):
        from tests.unit.test_carapivehicle_registry import api_with_registry
        from TWCManager.TWCMaster import TWCMaster
        from TWCManager.Vehicle.Providers.Order import ProviderOrder
        from TWCManager.Vehicle.Providers.Provider import Provider

        api, reg = api_with_registry()
        api.storeToken(owner_token())
        add_home_vehicle(api, "required")
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
        # HA is refreshed even when TeslaAPI holds cars
        ha = Mock()
        ha.enabled.return_value = True
        ble, api = self.make(ha=ha)
        assert ha.updateChargeAtHome.call_count == 1
        assert api.updateChargeAtHome.call_count == 1


# What Tesla answers a REST command for a car that needs signed commands.
VCP_403 = {
    "response": None,
    "error": "Tesla Vehicle Command Protocol required, please refer to the "
    "documentation here: https://developer.tesla.com/docs/fleet-api",
    "error_description": "",
}
ACCEPTED = {"response": {"result": True, "reason": ""}}
ONLINE = {"response": {"state": "online"}}


def tesla_provider(api):
    """Either of TeslaAPI's providers: the command methods are shared and
    send through commandSlotFor, the slot that may command the car."""
    return api.providers()[0]


# Each owner-command path: the endpoint under /5YJ3E1EA1KF000001 and how to send it.
COMMANDS = {
    "set_charging_amps": (
        "/command/set_charging_amps",
        lambda api, vehicle: tesla_provider(api).setAmps(vehicle.VIN, 10),
    ),
    "set_charge_limit": (
        "/command/set_charge_limit",
        lambda api, vehicle: tesla_provider(api).setChargeLimit(vehicle.VIN, 80),
    ),
    "charge_start": (
        "/command/charge_start",
        lambda api, vehicle: tesla_provider(api).startCharging(vehicle.VIN),
    ),
    "charge_stop": (
        "/command/charge_stop",
        lambda api, vehicle: tesla_provider(api).stopCharging(vehicle.VIN),
    ),
    "wake_up": (
        "/wake_up",
        lambda api, vehicle: tesla_provider(api).wake(vehicle.VIN),
    ),
}


def prime(command, api, vehicle, requests_mock, state="asleep"):
    """A wake first asks the free online state and goes out only for a car
    not known to be online whose charge need is live and past its grace; a
    stop asks it too and goes out to an online car."""
    if command == "charge_stop":
        for base in (OWNER_URL, FLEET_URL):
            requests_mock.get(base + "/" + vehicle.VIN, json=ONLINE)
        return
    if command != "wake_up":
        return
    vehicle.lastAPIAccessTime = 0
    vehicle.firstChargeNeededTime = 1
    vehicle.lastChargeNeededTime = time.time()
    for base in (OWNER_URL, FLEET_URL):
        requests_mock.get(base + "/" + vehicle.VIN, json={"response": {"state": state}})


class Refusal:
    """Matches an Outcome whose kind is one of the given kinds."""

    def __init__(self, *kinds):
        self.kinds = kinds

    def __eq__(self, other):
        return getattr(other, "kind", None) in self.kinds


REFUSED_OUTCOME = Refusal(REFUSED)

# What each command path returns when Tesla refuses it (and latches the
# car), then when it is refused locally afterwards. The reconciler treats a
# REFUSED outcome as excluding the provider.
REFUSED_RETURNS = {command: (REFUSED_OUTCOME, REFUSED_OUTCOME) for command in COMMANDS}

# Owner API answers that mean the car needs signed commands: HTTP 403 with
# or without a JSON body, or the Vehicle Command Protocol error.
SIGNING_REFUSALS = {
    "403 VCP json": {"status_code": 403, "json": VCP_403},
    "403 empty": {"status_code": 403, "text": ""},
    "403 text": {"status_code": 403, "text": "Forbidden"},
    "403 html": {"status_code": 403, "text": "<html><body>403</body></html>"},
}


# What the Owner API answers once Tesla has stopped taking commands on it.
FLEETAPI_ONLY = {
    "response": None,
    "error": "Endpoint is only available on fleetapi. Visit "
    "https://developer.tesla.com/docs for more info",
    "error_description": "",
}

# Owner API answers that mean it no longer takes commands for the car:
# HTTP 410 or 412, or the "only available on fleetapi" error, whatever
# the status.
RETIRED_REFUSALS = {
    "410": {"status_code": 410, "text": ""},
    "412": {"status_code": 412, "text": ""},
    "412 fleetapi json": {"status_code": 412, "json": FLEETAPI_ONLY},
    "200 fleetapi json": {"status_code": 200, "json": FLEETAPI_ONLY},
    "200 fleetapi text": {
        "status_code": 200,
        "text": "Endpoint is only available on fleetapi.",
    },
}

ALL_REFUSALS = {**SIGNING_REFUSALS, **RETIRED_REFUSALS}

VEHICLE_DATA = {
    "response": {
        "drive_state": {"latitude": 37.0, "longitude": -122.0},
        "charge_state": {"battery_level": 55, "charge_limit_soc": 80},
        "vehicle_state": {"vehicle_name": "Car"},
    }
}


def reply_for(command):
    return ONLINE if command == "wake_up" else ACCEPTED


@pytest.fixture
def no_sleep(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi.time, "sleep", lambda secs: None)


def add_car(api, command_signing, vin="5YJ3E1EA1KF000001", name="Car", id=1):
    """A car at home whose charge and location reads succeed without HTTP,
    on a master that tracks charge limits."""
    vehicle = add_home_vehicle(api, command_signing, vin=vin, name=name, id=id)
    vehicle.update_charge = lambda *args, **kwargs: True
    vehicle.update_location = lambda *args, **kwargs: True
    api.master.getNormalChargeLimit = lambda vehicleID: (True, 70, -1)
    api.master.saveNormalChargeLimit = lambda *args: None
    api.master.removeNormalChargeLimit = lambda *args: None
    return vehicle


@pytest.fixture
def owner_car(no_threads, no_sleep):
    def make(command_signing, fleet=None):
        api = new_api()
        api.storeToken(owner_token())
        if fleet:
            api.storeToken(fleet)
        return api, add_car(api, command_signing)

    return make


def order_over(api):
    """The provider order the reconciler walks: TeslaAPI's providers, then a
    fallback provider ranked after them, as HomeAssistant is."""
    from TWCManager.Vehicle.Providers.Order import ProviderOrder
    from TWCManager.Vehicle.Providers.Provider import Provider

    class Fallback(Provider):
        name, rank, local = "Fallback", 6, False

        def declarations(self):
            return {c: C.Declaration(C.HA_BILLED) for c in C.COMMANDS}

        def ready(self, vehicle, capability):
            return True

    master = api.master
    master.vehicleRegistry = api.registry
    order = ProviderOrder(master)
    for p in api.providers() + [Fallback()]:
        order.register(p)
    return order


def amps_candidates(api, vehicle):
    """Who may set this car's amps, in order."""
    return [p.name for p in order_over(api).candidates(vehicle.vehicle, C.SET_AMPS)]


class TestOwnerCommandsForUnsignedVehicles:
    @pytest.mark.parametrize("command", list(COMMANDS))
    def test_sent_to_owner_api_with_owner_token(
        self, owner_car, requests_mock, command
    ):
        api, vehicle = owner_car("off")
        suffix, send = COMMANDS[command]
        prime(command, api, vehicle, requests_mock)
        cmd = requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001" + suffix, json=reply_for(command)
        )

        assert api.readOnly is False
        assert api.readOnlyFor(vehicle) is False
        send(api, vehicle)

        assert cmd.call_count == 1
        bearer = cmd.last_request.headers["Authorization"]
        assert bearer == "Bearer " + api.ownerSlot.accessToken
        assert no_error_recorded(api)

    def test_read_pause_does_not_block_owner_commands(self, owner_car, requests_mock):
        api, vehicle = owner_car("off")
        api.ownerSlot.pause()
        cmd = requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001/command/set_charging_amps", json=ACCEPTED
        )
        assert tesla_provider(api).setAmps(vehicle.VIN, 10) == Outcome(DONE)
        assert cmd.call_count == 1

    def test_cleared_owner_slot_blocks(self, owner_car, any_http):
        api, vehicle = owner_car("off")
        api.ownerSlot.clear()
        assert api.readOnlyFor(vehicle) is True
        assert tesla_provider(api).setAmps(vehicle.VIN, 10) == REFUSED_OUTCOME
        assert any_http.call_count == 0


class TestRefusedVehicles:
    @pytest.mark.parametrize(
        "signing", ["required", None, "weird"], ids=["required", "missing", "unknown"]
    )
    @pytest.mark.parametrize("command", list(COMMANDS))
    def test_refused_locally(self, owner_car, any_http, signing, command):
        api, vehicle = owner_car(signing)
        suffix, send = COMMANDS[command]

        assert api.readOnly is True
        assert api.readOnlyFor(vehicle) is True
        assert send(api, vehicle) in (False, REFUSED_OUTCOME)

        assert any_http.call_count == 0
        assert no_error_recorded(api)


@pytest.fixture
def mixed_fleet(no_threads, no_sleep):
    """Owner token only; 5YJ3E1EA1KF000001 takes Owner API commands, 5YJ3E1EA1KF000002 needs signing."""
    api = new_api()
    api.storeToken(owner_token())
    unsigned = add_car(api, "off")
    signed = add_car(api, "required", vin="5YJ3E1EA1KF000002", name="Signed", id=2)
    return api, unsigned, signed


class TestMixedFleet:
    def test_each_vehicle_decided_separately(self, mixed_fleet, requests_mock):
        api, unsigned, signed = mixed_fleet
        amps = requests_mock.post(
            re.compile(r".*/command/set_charging_amps"), json=ACCEPTED
        )
        provider = tesla_provider(api)

        assert api.readOnly is False
        assert api.readOnlyFor(unsigned) is False
        assert api.readOnlyFor(signed) is True
        assert provider.setAmps(signed.VIN, 10) == REFUSED_OUTCOME
        assert amps.call_count == 0
        assert provider.setAmps(unsigned.VIN, 10) == Outcome(DONE)
        assert amps.call_count == 1
        assert amps.last_request.url.startswith(OWNER_URL + "/5YJ3E1EA1KF000001/")
        assert no_error_recorded(api)
        assert signed.lastErrorTime == 0

    def test_stop_reaches_only_the_unsigned_car(self, mixed_fleet, requests_mock):
        api, unsigned, signed = mixed_fleet
        requests_mock.get(re.compile(r".*/vehicles/5YJ3E1EA1KF00000[12]$"), json=ONLINE)
        stop = requests_mock.post(re.compile(r".*/command/charge_stop"), json=ACCEPTED)
        provider = tesla_provider(api)

        # The signed car is refused without a request, so the reconciler
        # tries the next provider for it.
        assert provider.stopCharging(signed.VIN) == REFUSED_OUTCOME
        assert stop.call_count == 0
        assert provider.stopCharging(unsigned.VIN) == Outcome(DONE)
        assert stop.call_count == 1
        assert (
            stop.last_request.url
            == OWNER_URL + "/5YJ3E1EA1KF000001/command/charge_stop"
        )
        assert signed.lastErrorTime == 0
        assert no_error_recorded(api)

    def test_order_leaves_out_tesla_api_for_the_signed_car(self, mixed_fleet, any_http):
        api, unsigned, signed = mixed_fleet
        assert amps_candidates(api, signed) == ["Fallback"]
        assert any_http.call_count == 0
        assert no_error_recorded(api)
        assert signed.lastErrorTime == 0

    def test_order_puts_owner_api_first_for_the_unsigned_car(self, mixed_fleet):
        api, unsigned, signed = mixed_fleet
        assert amps_candidates(api, unsigned) == ["OwnerAPI", "Fallback"]

    def test_endpoint_read_only_per_vehicle(self, mixed_fleet):
        from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint
        from TWCManager.Vehicle.Providers.Order import ProviderOrder

        api, unsigned, signed = mixed_fleet
        order = ProviderOrder(api.master)
        for p in api.providers():
            order.register(p)
        assert VehicleEndpoint(unsigned.vehicle, order, api.master).isReadOnly is False
        assert VehicleEndpoint(signed.vehicle, order, api.master).isReadOnly is True


class TestOwnerCommandRefusedByTesla:
    """A car listed with command_signing "off" whose Owner API command Tesla
    refuses anyway: 403 or "Vehicle Command Protocol required" (needs signed
    commands), or 410, 412 or "only available on fleetapi" (the Owner API
    no longer takes commands). Either latches the car read-only."""

    @pytest.mark.parametrize("refusal", list(ALL_REFUSALS))
    @pytest.mark.parametrize("command", list(COMMANDS))
    def test_latches_without_error_backoff(
        self, owner_car, requests_mock, command, refusal
    ):
        api, vehicle = owner_car("off")
        suffix, send = COMMANDS[command]
        prime(command, api, vehicle, requests_mock)
        first, after = REFUSED_RETURNS[command]
        prime(command, api, vehicle, requests_mock)
        cmd = requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001" + suffix, **ALL_REFUSALS[refusal]
        )

        assert send(api, vehicle) == first

        assert cmd.call_count == 1
        assert vehicle.ownerCommandsRefused is True
        assert no_error_recorded(api)
        assert api.readOnlyFor(vehicle) is True
        assert api.readOnly is True

        assert send(api, vehicle) == after
        assert cmd.call_count == 1
        assert no_error_recorded(api)

        # Reads still run on the Owner token (of a car known awake: a read
        # of an asleep car is never attempted).
        assert api.ownerSlot.usable("read")
        vehicle.lastAPIAccessTime = time.time()
        vehicle.firstWakeAttemptTime = 0
        data = requests_mock.get(
            OWNER_URL + "/5YJ3E1EA1KF000001/vehicle_data", json=VEHICLE_DATA
        )
        assert vehicle.update_vehicle_data() is True
        assert data.call_count == 1

    @pytest.mark.parametrize(
        "refusal,cause",
        [(name, "signing") for name in SIGNING_REFUSALS]
        + [(name, "retired") for name in RETIRED_REFUSALS],
    )
    def test_warning_names_the_cause(
        self, owner_car, requests_mock, caplog, refusal, cause
    ):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car("off")
        requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001/command/set_charging_amps",
            **ALL_REFUSALS[refusal]
        )
        tesla_provider(api).setAmps(vehicle.VIN, 10)

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "Car" in warnings[0]
        assert "TeslaBLE" in warnings[0]
        if cause == "signing":
            assert "needs signed commands" in warnings[0]
            assert "no longer" not in warnings[0]
        else:
            assert "Owner API no longer takes commands" in warnings[0]
            assert "signed commands" not in warnings[0]

        # The per-car read-only line, logged again on a state change, gives
        # the same cause.
        vehicle.refusalLoggedAt = None
        tesla_provider(api).setAmps(vehicle.VIN, 12)
        lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "read-only for Car" in r.getMessage()
        ]
        assert len(lines) == 1
        if cause == "signing":
            assert "command_signing: off" in lines[0]
        else:
            assert "Owner API no longer takes commands" in lines[0]

    def test_wake_from_the_reconciler_records_no_error(self, owner_car, requests_mock):
        api, vehicle = owner_car("off")
        vehicle.lastAPIAccessTime = 0  # not known to be awake
        # A live charge need whose pre-wake hold is long over
        vehicle.firstChargeNeededTime = 1
        vehicle.lastChargeNeededTime = time.time()
        requests_mock.get(
            OWNER_URL + "/5YJ3E1EA1KF000001", json={"response": {"state": "asleep"}}
        )
        wake = requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001/wake_up", status_code=403, json=VCP_403
        )

        assert tesla_provider(api).wake(vehicle.VIN) == REFUSED_OUTCOME

        assert wake.call_count == 1
        assert vehicle.ownerCommandsRefused is True
        assert no_error_recorded(api)

    @pytest.mark.parametrize("refusal", list(ALL_REFUSALS))
    def test_latched_car_leaves_the_order_and_reads_still_work(
        self, owner_car, requests_mock, refusal
    ):
        api, vehicle = owner_car("off")
        cmd = requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001/command/set_charging_amps",
            **ALL_REFUSALS[refusal]
        )
        assert amps_candidates(api, vehicle) == ["OwnerAPI", "Fallback"]

        assert tesla_provider(api).setAmps(vehicle.VIN, 10) == REFUSED_OUTCOME
        assert cmd.call_count == 1

        # Latched: the next walk leaves TeslaAPI out without an HTTP call.
        assert amps_candidates(api, vehicle) == ["Fallback"]
        assert cmd.call_count == 1
        assert no_error_recorded(api)

        data = requests_mock.get(
            OWNER_URL + "/5YJ3E1EA1KF000001/vehicle_data", json=VEHICLE_DATA
        )
        assert vehicle.update_vehicle_data() is True
        assert data.call_count == 1

    # Fleet API answers to a command keep today's handling: Tesla is
    # refusing the Fleet token, not the Owner API, so nothing latches.
    FLEET_REFUSALS = {
        "403 VCP json": {"status_code": 403, "json": VCP_403},
        "410": {"status_code": 410, "json": {"response": None, "error": "gone"}},
        "412 fleetapi json": {"status_code": 412, "json": FLEETAPI_ONLY},
    }
    # A Fleet refusal is REFUSED (403, 412) or ERROR (410); a refused wake is
    # UNREACHABLE, so Fleet still carries commands that need no wake
    FLEET_REFUSED = Refusal("refused", "error", "unreachable")

    @pytest.mark.parametrize("refusal", list(FLEET_REFUSALS))
    @pytest.mark.parametrize("command", list(COMMANDS))
    def test_fleet_refusal_does_not_latch(
        self, owner_car, requests_mock, command, refusal
    ):
        api, vehicle = owner_car("off", fleet=fleet_token())
        suffix, send = COMMANDS[command]
        prime(command, api, vehicle, requests_mock)
        answer = self.FLEET_REFUSALS[refusal]
        # A stop first asks the free online state; an online car gets it
        requests_mock.get(OWNER_URL + "/5YJ3E1EA1KF000001", json=ONLINE)
        prime(command, api, vehicle, requests_mock)
        cmd = requests_mock.post(FLEET_URL + "/5YJ3E1EA1KF000001" + suffix, **answer)

        assert send(api, vehicle) == self.FLEET_REFUSED

        assert cmd.call_count >= 1
        assert vehicle.ownerCommandsRefused is False
        # The reconciler path retires a Fleet token Tesla refused (403, 412);
        # the Owner token then carries this car's commands
        if api.fleetSlot.usable("command"):
            assert api.commandSlotFor(vehicle) is api.fleetSlot
        else:
            assert api.commandSlotFor(vehicle) is api.ownerSlot
        assert api.readOnlyFor(vehicle) is False


class TestBothTokens:
    def test_fleet_carries_commands_for_unsigned_car(self, owner_car, requests_mock):
        api, vehicle = owner_car("off", fleet=fleet_token())
        amps = requests_mock.post(
            re.compile(r".*/command/set_charging_amps"), json=ACCEPTED
        )

        assert api.commandSlotFor(vehicle) is api.fleetSlot
        tesla_provider(api).setAmps(vehicle.VIN, 10)

        assert amps.call_count == 1
        assert amps.last_request.url.startswith(FLEET_URL + "/5YJ3E1EA1KF000001/")
        bearer = amps.last_request.headers["Authorization"]
        assert bearer == "Bearer " + api.fleetSlot.accessToken

    def test_owner_carries_commands_when_fleet_lacks_command_scope(
        self, owner_car, requests_mock
    ):
        api, vehicle = owner_car(
            "off", fleet=fleet_token(scp=["openid", "vehicle_device_data"])
        )
        amps = requests_mock.post(
            re.compile(r".*/command/set_charging_amps"), json=ACCEPTED
        )

        assert api.commandSlotFor(vehicle) is api.ownerSlot
        tesla_provider(api).setAmps(vehicle.VIN, 10)
        assert amps.last_request.url.startswith(OWNER_URL + "/5YJ3E1EA1KF000001/")

    def test_fleet_carries_commands_for_signed_car(self, owner_car):
        api, vehicle = owner_car("required", fleet=fleet_token())
        assert api.commandSlotFor(vehicle) is api.fleetSlot
        assert api.readOnly is False


class TestVehiclesNotYetListed:
    """Whether an Owner token can command is only known once the vehicles
    are listed, and only a read path lists them (unless telemetry does), so
    TeslaAPI is not read-only while the list is empty."""

    def test_owner_only_read_only_once_listed(self, no_threads):
        api = new_api()
        api.storeToken(owner_token())
        assert api.readOnly is False
        add_home_vehicle(api, "required")
        assert api.readOnly is True

    def test_no_token_is_read_only_unlisted(self):
        assert new_api().readOnly is True

    def test_command_without_vehicles_logs_no_error(self, no_threads, any_http, caplog):
        # The reconciler asks every cycle while the list is empty; that is
        # expected, not an error, and lists nothing itself.
        caplog.set_level(logging.DEBUG)
        api = new_api()
        api.storeToken(owner_token())

        for _ in range(3):
            assert tesla_provider(api).setAmps("5YJ3E1EA1KF000001", 10) == Outcome(
                UNSUPPORTED, "not listed"
            )

        assert any_http.call_count == 0
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_listing_then_refuses_signed_car(self, no_threads, no_sleep, requests_mock):
        api = new_api()
        api.storeToken(owner_token())
        listing = requests_mock.get(
            OWNER_URL[: -len("vehicles")] + "products",
            json={
                "count": 1,
                "response": [
                    {
                        "id": 1,
                        "vin": "5YJ3E1EA1KF000001",
                        "display_name": "Car",
                        "command_signing": "required",
                    }
                ],
            },
        )
        posts = requests_mock.register_uri("POST", rm.ANY, json={})

        assert api.loadVehicleList() is True
        assert tesla_provider(api).stopCharging("5YJ3E1EA1KF000001") == REFUSED_OUTCOME

        assert listing.call_count == 1
        assert posts.call_count == 0
        assert api.readOnly is True
        assert no_error_recorded(api)


class TestPerVehicleRefusalLogging:
    def lines(self, caplog, text):
        return [
            r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.INFO and text in r.getMessage()
        ]

    def test_required_car_logged_once_with_the_fix(self, owner_car, any_http, caplog):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car("required")
        provider = tesla_provider(api)
        provider.setAmps(vehicle.VIN, 10)
        provider.setAmps(vehicle.VIN, 12)
        provider.wake(vehicle.VIN)
        provider.startCharging(vehicle.VIN)

        lines = self.lines(caplog, "command_signing")
        assert len(lines) == 1
        assert "Car" in lines[0]
        assert "command_signing: required" in lines[0]
        assert "teslaProxy" in lines[0]
        assert "TeslaBLE" in lines[0]

    @pytest.mark.parametrize("signing", [None, "weird"], ids=["missing", "unknown"])
    def test_unknown_value_logged_as_read_only(
        self, owner_car, any_http, caplog, signing
    ):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car(signing)
        provider = tesla_provider(api)
        provider.setAmps(vehicle.VIN, 10)
        provider.setAmps(vehicle.VIN, 12)

        lines = self.lines(caplog, "command_signing")
        assert len(lines) == 1
        assert "read-only" in lines[0]
        if signing:
            assert signing in lines[0]

    def test_mixed_fleet_logs_signed_car_only(self, mixed_fleet, requests_mock, caplog):
        caplog.set_level(logging.INFO)
        api, unsigned, signed = mixed_fleet
        requests_mock.post(re.compile(r".*/command/set_charging_amps"), json=ACCEPTED)
        provider = tesla_provider(api)
        provider.setAmps(signed.VIN, 10)
        provider.setAmps(unsigned.VIN, 10)
        provider.setAmps(signed.VIN, 12)

        lines = self.lines(caplog, "command_signing")
        assert len(lines) == 1
        assert "Signed" in lines[0]

    def test_latch_logged_once(self, owner_car, requests_mock, caplog):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car("off")
        requests_mock.post(
            OWNER_URL + "/5YJ3E1EA1KF000001/command/set_charging_amps",
            status_code=403,
            json=VCP_403,
        )
        provider = tesla_provider(api)
        provider.setAmps(vehicle.VIN, 10)
        provider.setAmps(vehicle.VIN, 12)
        provider.wake(vehicle.VIN)

        lines = self.lines(caplog, "Car")
        assert len(lines) == 1
        assert "teslaProxy" in lines[0]
        assert "TeslaBLE" in lines[0]


class TestOrderLogsWhy:
    """When every listed car is read-only, the provider order leaves TeslaAPI
    out without calling it; the walk still logs the per-car reason once."""

    def test_one_line_naming_the_car(self, owner_car, any_http, caplog):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car("required")
        assert api.readOnly is True

        for _ in range(5):
            assert amps_candidates(api, vehicle) == ["Fallback"]

        lines = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.INFO and "read-only" in r.getMessage()
        ]
        assert len(lines) == 1
        assert "read-only for Car" in lines[0]
        assert "command_signing: required" in lines[0]
        assert "teslaProxy" in lines[0]
        assert "TeslaBLE" in lines[0]
        assert any_http.call_count == 0
        assert no_error_recorded(api)
