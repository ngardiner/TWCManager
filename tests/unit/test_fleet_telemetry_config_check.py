"""The daily Fleet Telemetry config check: each listed car's config is read
(free, never paid, never a wake) and a car lacking a field TWCManager needs
is warned of once per distinct problem."""

import logging
from unittest.mock import Mock

import pytest
import requests

from tests.unit.teslatokens import FLEET_NA_URL, fleet_token
from tests.unit.test_twcmanager import _launcher_function
import TWCManager.Vehicle.FleetTelemetryMQTT as FleetTelemetryMQTTModule
from TWCManager.Vehicle.FleetTelemetryMQTT import FleetTelemetryMQTT
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaAPI import TeslaAPI

VIN = "5YJ3E1EA0KF000001"
URL = FLEET_NA_URL + "/" + VIN + "/fleet_telemetry_config"


def answer(fields=None, synced=True, config=True):
    fields = sorted(FleetTelemetryMQTT.REQUIRED) if fields is None else fields
    cfg = {"hostname": "t.example.com", "fields": {f: {} for f in fields}}
    return {
        "response": {
            "synced": synced,
            "config": cfg if config else None,
            "limit_reached": False,
            "key_paired": True,
        }
    }


@pytest.fixture
def setup():
    master = Mock()
    master.vehicleRegistry = VehicleRegistry({})
    master.tokenSyncEnabled.return_value = False
    master.config = {
        "config": {"teslaApiClientID": "id"},
        "vehicle": {
            "teslaAPI": {"enabled": True},
            "teslaFleetTelemetryMQTT": {"enabled": True},
        },
    }
    api = TeslaAPI(master)
    api.storeToken(fleet_token(), "refresh")
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    return api, FleetTelemetryMQTT(master), master.vehicleRegistry.get(VIN)


def warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def check(setup, requests_mock, caplog, nextDay=False, **kw):
    api, tlm, _ = setup
    requests_mock.get(URL, json=answer(**kw))
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        if nextDay:
            tlm.configCheckDue = 0.0
        tlm.checkConfigs(api)
    return warnings(caplog)


def test_missing_required_field_warns_once_naming_it(setup, requests_mock, caplog):
    fields = sorted(FleetTelemetryMQTT.REQUIRED - {"Gear"})
    got = check(setup, requests_mock, caplog, fields=fields)
    assert len(got) == 1
    msg = got[0].getMessage()
    assert VIN in msg and "Gear" in msg and "Vehicle_FleetTelemetryMQTT.md" in msg
    assert "lacks Gear" in setup[2].claims["FleetTelemetryMQTT"].status
    # Unchanged a day later: read again, not warned again
    assert check(setup, requests_mock, caplog, nextDay=True, fields=fields) == []
    assert requests_mock.call_count == 2


def test_a_new_problem_warns_again(setup, requests_mock, caplog):
    check(setup, requests_mock, caplog, fields=["Gear"])
    assert len(check(setup, requests_mock, caplog, nextDay=True, config=False)) == 1


def test_all_present_no_warning_and_no_claim(setup, requests_mock, caplog):
    assert check(setup, requests_mock, caplog) == []
    assert "FleetTelemetryMQTT" not in setup[2].claims


def test_fixed_config_clears_the_shown_problem(setup, requests_mock, caplog):
    check(setup, requests_mock, caplog, fields=["Gear"])
    assert check(setup, requests_mock, caplog, nextDay=True) == []
    assert setup[2].claims["FleetTelemetryMQTT"].status == "reports"


def test_optional_fields_missing_no_warning(setup, requests_mock, caplog):
    # Every required field and none of the optional ones
    fields = sorted(FleetTelemetryMQTT.REQUIRED)
    assert FleetTelemetryMQTT.OPTIONAL and not FleetTelemetryMQTT.OPTIONAL & set(fields)
    assert check(setup, requests_mock, caplog, fields=fields) == []


def test_not_synced_warns(setup, requests_mock, caplog):
    got = check(setup, requests_mock, caplog, synced=False)
    assert len(got) == 1 and "not applied" in got[0].getMessage()


def test_no_config_warns(setup, requests_mock, caplog):
    got = check(setup, requests_mock, caplog, config=False)
    assert len(got) == 1 and "no Fleet Telemetry config" in got[0].getMessage()


def test_only_once_a_day(setup, requests_mock, caplog, monkeypatch):
    clock = Mock(return_value=1000.0)
    monkeypatch.setattr(FleetTelemetryMQTTModule, "time", Mock(time=clock))
    check(setup, requests_mock, caplog)
    clock.return_value += 24 * 60 * 60 - 1
    check(setup, requests_mock, caplog)
    assert requests_mock.call_count == 1
    clock.return_value += 1
    check(setup, requests_mock, caplog)
    assert requests_mock.call_count == 2


@pytest.mark.parametrize("status", [401, 403, 404, 412, 500])
def test_refused_read_is_quiet_and_keeps_the_token(
    setup, requests_mock, caplog, status
):
    api, tlm, vehicle = setup
    requests_mock.get(URL, status_code=status, json={"error": "x"})
    with caplog.at_level(logging.DEBUG):
        tlm.checkConfigs(api)
    assert warnings(caplog) == [] and not any(
        r.levelno >= logging.ERROR for r in caplog.records
    )
    assert api.fleetSlot.usable("read") and "FleetTelemetryMQTT" not in vehicle.claims


def test_network_failure_is_quiet(setup, requests_mock, caplog):
    api, tlm, _ = setup
    requests_mock.get(URL, exc=requests.exceptions.ConnectionError)
    with caplog.at_level(logging.DEBUG):
        tlm.checkConfigs(api)
    assert warnings(caplog) == []


def test_never_counted_as_paid(setup, requests_mock, caplog):
    check(setup, requests_mock, caplog, fields=["Gear"])
    assert setup[2].paidRequests == 0
    assert not any("paid request" in r.getMessage() for r in caplog.records)


def test_no_fleet_token_no_request(setup, requests_mock):
    api, tlm, _ = setup
    api.fleetSlot.clear()
    tlm.checkConfigs(api)
    assert requests_mock.call_count == 0


def _refreshTokens(master, api):
    master.getModuleByName.side_effect = {"TeslaAPI": api}.get
    api.refreshTokenIfNeeded = Mock()
    _launcher_function("refreshTokens")(master)


def test_module_inactive_no_request(setup, requests_mock):
    api, _, _ = setup
    _refreshTokens(Mock(), api)
    assert requests_mock.call_count == 0


def test_the_token_refresh_task_runs_the_check(setup, requests_mock):
    api, tlm, _ = setup
    requests_mock.get(URL, json=answer())
    master = Mock()
    master.getModuleByName.side_effect = {
        "TeslaAPI": api,
        "FleetTelemetryMQTT": tlm,
    }.get
    api.refreshTokenIfNeeded = Mock()
    _launcher_function("refreshTokens")(master)
    assert requests_mock.call_count == 1
