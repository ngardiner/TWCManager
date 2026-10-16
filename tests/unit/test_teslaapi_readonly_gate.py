"""
Read-only command gate.

Without a usable Fleet API token, TeslaAPI commands a vehicle with an Owner
API token only when Tesla's /products entry for that vehicle says
command_signing "off". Every other vehicle (signing "required", the field
missing, or a value never seen) is read-only, as is every vehicle when no
token can command: no vehicle command may reach Tesla. VehiclePriority skips
TeslaAPI for command methods while every listed vehicle is read-only, and
TeslaAPI's own command entry points refuse per vehicle without an HTTP call
and without recording an API error (which would back off reads too). Reads
still run.
"""

import logging
import re
from unittest.mock import Mock

import pytest
import requests_mock as rm

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
        owner_api.storeToken(fleet_token())
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

        # Fleet token: commands allowed. Fleet slot cleared: logs again, once.
        owner_api.storeToken(fleet_token())
        owner_api.fleetSlot.clear()
        owner_api.setChargeRate(10, vehicle)
        owner_api.setChargeRate(10, vehicle)
        assert len(self.refusals(caplog)) == 2

    def test_resync_of_owner_token_does_not_log_again(
        self, owner_api, any_http, caplog
    ):
        caplog.set_level(logging.INFO)
        vehicle = owner_api.getCarApiVehicles()[0]
        owner_api.setChargeRate(10, vehicle)
        owner_api.storeToken(owner_token(jti="rotated"))
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
        api.storeToken(owner_token())
        # The Owner API lists vehicles under /products, not /vehicles.
        listing = requests_mock.get(
            OWNER_URL[: -len("vehicles")] + "products",
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
        assert not api.refuseCommandIfReadOnly.called
        assert "TeslaAPI" not in master.stats["moduleFailures"]

    def test_read_only_module_without_refusal_method_skipped(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        class ReadOnlyModule:
            readOnly = True

        master = Mock()
        assert (
            VehiclePriority(master)._skip_read_only_for_command(
                ReadOnlyModule(), "setChargeRate"
            )
            is True
        )

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


# What Tesla answers a REST command for a car that needs signed commands.
VCP_403 = {
    "response": None,
    "error": "Tesla Vehicle Command Protocol required, please refer to the "
    "documentation here: https://developer.tesla.com/docs/fleet-api",
    "error_description": "",
}
ACCEPTED = {"response": {"result": True, "reason": ""}}
ONLINE = {"response": {"state": "online"}}

# Each owner-command path: the endpoint under /VIN1 and how to send it.
COMMANDS = {
    "set_charging_amps": (
        "/command/set_charging_amps",
        lambda api, vehicle: api.setChargeRate(10, vehicle),
    ),
    "apply_charge_limit": (
        "/command/set_charge_limit",
        lambda api, vehicle: vehicle.apply_charge_limit(80),
    ),
    "applyChargeLimit": (
        "/command/set_charge_limit",
        lambda api, vehicle: api.applyChargeLimit(80),
    ),
    "charge_start": (
        "/command/charge_start",
        lambda api, vehicle: api.car_api_charge({"charge": True}),
    ),
    "charge_stop": (
        "/command/charge_stop",
        lambda api, vehicle: api.car_api_charge({"charge": False}),
    ),
    "wake_up": ("/wake_up", lambda api, vehicle: api.wakeVehicle(vehicle)),
}


# What each command path returns when Tesla refuses it (and latches the
# car), then when it is refused locally afterwards. applyChargeLimit returns
# None after any send it makes; once every car is read-only it refuses at the
# top with "error". VehiclePriority treats all of these as failure.
REFUSED_RETURNS = {
    "set_charging_amps": (False, False),
    "apply_charge_limit": (False, False),
    "applyChargeLimit": (None, "error"),
    "charge_start": ("error", "error"),
    "charge_stop": ("error", "error"),
    "wake_up": (False, False),
}

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


def allow_resend(api, vehicle):
    # Clears the once-a-minute and once-applied guards between two sends.
    api.carApiLastStartOrStopChargeTime = 0
    api.carApiLastChargeLimitApplyTime = 0
    vehicle.lastLimitAttemptTime = 0
    vehicle.stopTryingToApplyLimit = False
    vehicle.stopAskingToStartCharging = False
    vehicle.lastChargeRateSent = None


@pytest.fixture
def no_sleep(monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi.time, "sleep", lambda secs: None)


def add_car(api, command_signing, vin="VIN1", name="Car", id=1):
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


def priority_over(api, ble):
    """VehiclePriority trying TeslaAPI first, then a mock BLE module."""
    from TWCManager.Vehicle.VehiclePriority import VehiclePriority

    master = Mock()
    master.stats = {"moduleSuccess": {}, "moduleFailures": {}}
    master.config = {}
    master.settings = {}
    master.getModuleByPriority = Mock(
        side_effect=[("TeslaAPI", api, 10), ("TeslaBLE", ble, 5), ("", None, 0)]
    )
    return VehiclePriority(master)


class TestOwnerCommandsForUnsignedVehicles:
    @pytest.mark.parametrize("command", list(COMMANDS))
    def test_sent_to_owner_api_with_owner_token(
        self, owner_car, requests_mock, command
    ):
        api, vehicle = owner_car("off")
        suffix, send = COMMANDS[command]
        cmd = requests_mock.post(OWNER_URL + "/VIN1" + suffix, json=reply_for(command))

        assert api.readOnly is False
        assert api.readOnlyFor(vehicle) is False
        send(api, vehicle)

        assert cmd.call_count == 1
        assert requests_mock.call_count == 1
        bearer = cmd.last_request.headers["Authorization"]
        assert bearer == "Bearer " + api.ownerSlot.accessToken
        assert no_error_recorded(api)

    def test_read_pause_does_not_block_owner_commands(self, owner_car, requests_mock):
        api, vehicle = owner_car("off")
        api.ownerSlot.pause()
        cmd = requests_mock.post(
            OWNER_URL + "/VIN1/command/set_charging_amps", json=ACCEPTED
        )
        assert api.setChargeRate(10, vehicle) == ACCEPTED
        assert cmd.call_count == 1

    def test_cleared_owner_slot_blocks(self, owner_car, any_http):
        api, vehicle = owner_car("off")
        api.ownerSlot.clear()
        assert api.readOnlyFor(vehicle) is True
        assert api.setChargeRate(10, vehicle) is False
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
        assert send(api, vehicle) in (False, "error")

        assert any_http.call_count == 0
        assert no_error_recorded(api)


@pytest.fixture
def mixed_fleet(no_threads, no_sleep):
    """Owner token only; VIN1 takes Owner API commands, VIN2 needs signing."""
    api = new_api()
    api.storeToken(owner_token())
    unsigned = add_car(api, "off")
    signed = add_car(api, "required", vin="VIN2", name="Signed", id=2)
    return api, unsigned, signed


class TestMixedFleet:
    def test_each_vehicle_decided_separately(self, mixed_fleet, requests_mock):
        api, unsigned, signed = mixed_fleet
        amps = requests_mock.post(
            re.compile(r".*/command/set_charging_amps"), json=ACCEPTED
        )

        assert api.readOnly is False
        assert api.readOnlyFor(unsigned) is False
        assert api.readOnlyFor(signed) is True
        assert api.setChargeRate(10, signed) is False
        assert amps.call_count == 0
        assert api.setChargeRate(10, unsigned) == ACCEPTED
        assert amps.call_count == 1
        assert amps.last_request.url.startswith(OWNER_URL + "/VIN1/")
        assert no_error_recorded(api)
        assert signed.lastErrorTime == 0

    def test_charge_reaches_only_the_unsigned_car(self, mixed_fleet, requests_mock):
        api, unsigned, signed = mixed_fleet
        stop = requests_mock.post(re.compile(r".*/command/charge_stop"), json=ACCEPTED)

        # The signed car is refused, so the call as a whole fails and
        # VehiclePriority tries the next module.
        assert api.car_api_charge({"charge": False}) == "error"
        assert stop.call_count == 1
        assert stop.last_request.url == OWNER_URL + "/VIN1/command/charge_stop"
        assert signed.lastErrorTime == 0
        assert no_error_recorded(api)

    def test_vehicle_priority_falls_through_for_signed_car(self, mixed_fleet, any_http):
        api, unsigned, signed = mixed_fleet
        ble = Mock()
        ble.setChargeRate = Mock(return_value=True)
        priority = priority_over(api, ble)

        assert priority.setChargeRate(10, signed) is True
        ble.setChargeRate.assert_called_with(10, signed)
        assert any_http.call_count == 0
        assert no_error_recorded(api)
        assert signed.lastErrorTime == 0

    def test_vehicle_priority_uses_tesla_api_for_unsigned_car(
        self, mixed_fleet, requests_mock
    ):
        api, unsigned, signed = mixed_fleet
        amps = requests_mock.post(
            OWNER_URL + "/VIN1/command/set_charging_amps", json=ACCEPTED
        )
        ble = Mock()
        ble.setChargeRate = Mock(return_value=True)
        priority = priority_over(api, ble)

        assert priority.setChargeRate(10, unsigned) == ACCEPTED
        assert amps.call_count == 1
        assert not ble.setChargeRate.called


# Each owner-command path as VehiclePriority calls it: the endpoint under
# /VIN1 and how to call it.
PRIORITY_CALLS = {
    "setChargeRate": (
        "/command/set_charging_amps",
        lambda priority, vehicle: priority.setChargeRate(10, vehicle),
    ),
    "applyChargeLimit": (
        "/command/set_charge_limit",
        lambda priority, vehicle: priority.applyChargeLimit(80),
    ),
    "car_api_charge": (
        "/command/charge_stop",
        lambda priority, vehicle: priority.car_api_charge({"charge": False}),
    ),
    "wakeVehicle": (
        "/wake_up",
        lambda priority, vehicle: priority.wakeVehicle(vehicle),
    ),
}


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
        first, after = REFUSED_RETURNS[command]
        cmd = requests_mock.post(OWNER_URL + "/VIN1" + suffix, **ALL_REFUSALS[refusal])

        assert send(api, vehicle) == first

        assert cmd.call_count == 1
        assert requests_mock.call_count == 1
        assert vehicle.ownerCommandsRefused is True
        assert no_error_recorded(api)
        assert api.readOnlyFor(vehicle) is True
        assert api.readOnly is True

        allow_resend(api, vehicle)
        assert send(api, vehicle) == after
        assert requests_mock.call_count == 1
        assert no_error_recorded(api)

        # Reads still run on the Owner token.
        assert api.ownerSlot.usable("read")
        data = requests_mock.get(OWNER_URL + "/VIN1/vehicle_data", json=VEHICLE_DATA)
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
            OWNER_URL + "/VIN1/command/set_charging_amps", **ALL_REFUSALS[refusal]
        )
        api.setChargeRate(10, vehicle)

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
        api.setChargeRate(12, vehicle)
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

    def test_wake_from_car_api_available_records_no_error(
        self, owner_car, requests_mock
    ):
        api, vehicle = owner_car("off")
        vehicle.lastAPIAccessTime = 0  # not known to be awake
        vehicle.firstChargeNeededTime = 1  # pre-wake hold long over
        requests_mock.get(OWNER_URL + "/VIN1", json={"response": {"state": "asleep"}})
        wake = requests_mock.post(
            OWNER_URL + "/VIN1/wake_up", status_code=403, json=VCP_403
        )

        api.car_api_available(charge=True)

        assert wake.call_count == 1
        assert vehicle.ownerCommandsRefused is True
        assert no_error_recorded(api)

    @pytest.mark.parametrize("refusal", list(ALL_REFUSALS))
    @pytest.mark.parametrize("method", list(PRIORITY_CALLS))
    def test_vehicle_priority_falls_through_and_reads_still_work(
        self, owner_car, requests_mock, method, refusal
    ):
        api, vehicle = owner_car("off")
        suffix, call = PRIORITY_CALLS[method]
        cmd = requests_mock.post(OWNER_URL + "/VIN1" + suffix, **ALL_REFUSALS[refusal])
        ble = Mock()
        setattr(ble, method, Mock(return_value=True))

        assert call(priority_over(api, ble), vehicle) is True
        assert cmd.call_count == 1
        assert getattr(ble, method).call_count == 1

        # Latched: the next command skips TeslaAPI without an HTTP call.
        allow_resend(api, vehicle)
        assert call(priority_over(api, ble), vehicle) is True
        assert requests_mock.call_count == 1
        assert getattr(ble, method).call_count == 2
        assert no_error_recorded(api)

        data = requests_mock.get(OWNER_URL + "/VIN1/vehicle_data", json=VEHICLE_DATA)
        assert vehicle.update_vehicle_data() is True
        assert data.call_count == 1

    # Fleet API answers to a command keep today's handling: Tesla is
    # refusing the Fleet token, not the Owner API, so nothing latches.
    FLEET_REFUSALS = {
        "403 VCP json": {"status_code": 403, "json": VCP_403},
        "410": {"status_code": 410, "json": {"response": None, "error": "gone"}},
        "412 fleetapi json": {"status_code": 412, "json": FLEETAPI_ONLY},
    }
    # What each path returns for those answers, as before the latch.
    FLEET_RETURNS = {
        "set_charging_amps": lambda body: body,
        "apply_charge_limit": lambda body: False,
        "applyChargeLimit": lambda body: None,
        "charge_start": lambda body: "error",
        "charge_stop": lambda body: "error",
        "wake_up": lambda body: False,
    }

    @pytest.mark.parametrize("refusal", list(FLEET_REFUSALS))
    @pytest.mark.parametrize("command", list(COMMANDS))
    def test_fleet_refusal_does_not_latch(
        self, owner_car, requests_mock, command, refusal
    ):
        api, vehicle = owner_car("off", fleet=fleet_token())
        suffix, send = COMMANDS[command]
        answer = self.FLEET_REFUSALS[refusal]
        cmd = requests_mock.post(FLEET_URL + "/VIN1" + suffix, **answer)

        assert send(api, vehicle) == self.FLEET_RETURNS[command](answer["json"])

        assert cmd.call_count >= 1
        assert vehicle.ownerCommandsRefused is False
        assert api.commandSlotFor(vehicle) is api.fleetSlot
        assert api.readOnlyFor(vehicle) is False


class TestBothTokens:
    def test_fleet_carries_commands_for_unsigned_car(self, owner_car, requests_mock):
        api, vehicle = owner_car("off", fleet=fleet_token())
        amps = requests_mock.post(
            re.compile(r".*/command/set_charging_amps"), json=ACCEPTED
        )

        assert api.commandSlotFor(vehicle) is api.fleetSlot
        api.setChargeRate(10, vehicle)

        assert amps.call_count == 1
        assert amps.last_request.url.startswith(FLEET_URL + "/VIN1/")
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
        api.setChargeRate(10, vehicle)
        assert amps.last_request.url.startswith(OWNER_URL + "/VIN1/")

    def test_fleet_carries_commands_for_signed_car(self, owner_car):
        api, vehicle = owner_car("required", fleet=fleet_token())
        assert api.commandSlotFor(vehicle) is api.fleetSlot
        assert api.readOnly is False


class TestVehiclesNotYetListed:
    """Whether an Owner token can command is only known once the vehicles
    are listed, and only a command path lists them (unless telemetry does),
    so TeslaAPI is not read-only while the list is empty."""

    def test_owner_only_read_only_once_listed(self, no_threads):
        api = new_api()
        api.storeToken(owner_token())
        assert api.readOnly is False
        add_home_vehicle(api, "required")
        assert api.readOnly is True

    def test_no_token_is_read_only_unlisted(self):
        assert new_api().readOnly is True

    def test_set_charge_rate_without_vehicles_logs_no_error(
        self, no_threads, any_http, caplog
    ):
        # VehiclePriority calls setChargeRate every tick while the list is
        # empty; that is expected, not an error. setChargeRate does not list
        # the vehicles itself, so it makes no HTTP call.
        caplog.set_level(logging.DEBUG)
        api = new_api()
        api.storeToken(owner_token())
        ble = Mock()
        ble.setChargeRate = Mock(return_value=True)

        for _ in range(3):
            assert priority_over(api, ble).setChargeRate(10) is True

        assert ble.setChargeRate.call_count == 3
        assert any_http.call_count == 0
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        lines = [r for r in caplog.records if "No Tesla vehicles" in r.getMessage()]
        assert lines and all(r.levelno == logging.INFO2 for r in lines)

    def test_command_lists_vehicles_then_refuses_signed_car(
        self, no_threads, no_sleep, requests_mock
    ):
        api = new_api()
        api.storeToken(owner_token())
        listing = requests_mock.get(
            OWNER_URL[: -len("vehicles")] + "products",
            json={
                "count": 1,
                "response": [
                    {
                        "id": 1,
                        "vin": "VIN1",
                        "display_name": "Car",
                        "command_signing": "required",
                    }
                ],
            },
        )
        posts = requests_mock.register_uri("POST", rm.ANY, json={})

        assert api.car_api_charge({"charge": False}) == "error"

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
        api.setChargeRate(10, vehicle)
        api.setChargeRate(12, vehicle)
        api.wakeVehicle(vehicle)
        api.car_api_charge({"charge": True})

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
        api.setChargeRate(10, vehicle)
        api.setChargeRate(12, vehicle)

        lines = self.lines(caplog, "command_signing")
        assert len(lines) == 1
        assert "read-only" in lines[0]
        if signing:
            assert signing in lines[0]

    def test_mixed_fleet_logs_signed_car_only(self, mixed_fleet, requests_mock, caplog):
        caplog.set_level(logging.INFO)
        api, unsigned, signed = mixed_fleet
        requests_mock.post(re.compile(r".*/command/set_charging_amps"), json=ACCEPTED)
        api.setChargeRate(10, signed)
        api.setChargeRate(10, unsigned)
        api.setChargeRate(12, signed)

        lines = self.lines(caplog, "command_signing")
        assert len(lines) == 1
        assert "Signed" in lines[0]

    def test_latch_logged_once(self, owner_car, requests_mock, caplog):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car("off")
        requests_mock.post(
            OWNER_URL + "/VIN1/command/set_charging_amps",
            status_code=403,
            json=VCP_403,
        )
        api.setChargeRate(10, vehicle)
        api.setChargeRate(12, vehicle)
        api.wakeVehicle(vehicle)

        lines = self.lines(caplog, "Car")
        assert len(lines) == 1
        assert "teslaProxy" in lines[0]
        assert "TeslaBLE" in lines[0]


class TestVehiclePriorityLogsWhy:
    """When every listed car is read-only, VehiclePriority skips TeslaAPI
    without calling it; the skip still logs the per-car reason once."""

    @pytest.mark.parametrize(
        "method", ["setChargeRate", "car_api_charge", "applyChargeLimit"]
    )
    def test_one_line_naming_the_car(self, owner_car, any_http, caplog, method):
        caplog.set_level(logging.INFO)
        api, vehicle = owner_car("required")
        assert api.readOnly is True
        ble = Mock()
        setattr(ble, method, Mock(return_value=True))
        calls = {
            "setChargeRate": lambda priority: priority.setChargeRate(10, vehicle),
            "car_api_charge": lambda priority: priority.car_api_charge(
                {"charge": True}
            ),
            "applyChargeLimit": lambda priority: priority.applyChargeLimit(80),
        }

        for _ in range(5):
            api.carApiLastStartOrStopChargeTime = 0
            assert calls[method](priority_over(api, ble)) is True

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
        assert getattr(ble, method).call_count == 5
        assert no_error_recorded(api)
