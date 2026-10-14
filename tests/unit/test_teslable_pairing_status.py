# tests/unit/test_teslable_pairing_status.py
from unittest.mock import Mock, patch
import threading
import pytest
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaBLE import TeslaBLE

VIN = "5YJ3E1EA1KF000001"


def make():
    reg = VehicleRegistry({})
    master = Mock()
    master.vehicleRegistry = reg
    master.settings = {"Vehicles": {VIN: {"privKey": "x"}}}
    master.settingsLock = threading.RLock()
    master.config = {
        "config": {"teslaControlPath": "/bin/true"},
        "vehicle": {"teslaBLE": {"enabled": True}},
    }
    with patch.object(TeslaBLE, "_start_dbus_session"), patch.object(
        TeslaBLE, "_cleanup_stale_pipe"
    ):
        ble = TeslaBLE(master)
    ble.sendPrivateKey = Mock(return_value=True)
    ble._ensure_pipe_closed = Mock()
    return ble, reg, master


def test_pairing_then_proof_makes_paired_and_managed():
    ble, reg, master = make()
    ble.sendPublicKey = Mock(return_value=True)
    ble.parseCommandOutput = Mock(return_value=True)
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ok", 0))
    assert ble.peerWithVehicle(VIN) is True
    assert ble.bleStatus(VIN)["status"] == "pairing"
    assert reg.get(VIN) is None or reg.get(VIN).tier == "observed"
    ble._run_command_with_timeout = Mock(return_value=(b"", b"done", 0))
    assert ble._sendCommand_internal(VIN, "ping") == "done"
    assert ble.bleStatus(VIN)["status"] == "paired" and reg.get(VIN).tier == "managed"
    master.queue_background_task.assert_any_call({"cmd": "saveSettings"})


def test_refusal_drops_claim():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble._run_command_with_timeout = Mock(
        return_value=(
            b"",
            b"Error: vehicle rejected request: your public key has not been paired with the vehicle",
            1,
        )
    )
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "refused" and reg.get(VIN).tier == "observed"


def test_state_read_success_proves_key():
    ble, reg, master = make()
    ble._run_command_with_timeout = Mock(
        return_value=(b'{"chargeState": {"batteryLevel": 5}}', b"", 0)
    )
    assert (
        ble.get_charge_state(VIN)["batteryLevel"] == 5
        and ble.bleStatus(VIN)["status"] == "paired"
    )


def test_update_settings_reclaims_paired_vins():
    ble, reg, master = make()
    master.settings["Vehicles"][VIN]["ble"] = {"status": "paired", "since": 1.0}
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert reg.get(VIN).tier == "managed"


def test_update_settings_seeds_keyed_entries_as_unproven_and_paired_as_managed():  # ruling 6 / P9
    ble, reg, master = make()
    master.settings["Vehicles"] = {
        VIN: {"privKey": "x", "chargeSessions": 4},
        "5YJ3E1EA1KF000002": {
            "privKey": "y",
            "ble": {"status": "paired", "since": 1.0},
        },
        "5YJ3E1EA1KF000003": {
            "privKey": "z",
            "ble": {"status": "refused", "since": 1.0},
        },
    }
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert (
        reg.get(VIN).tier == "observed"
        and reg.get(VIN).claims["TeslaBLE"].status == "unproven"
    )
    assert reg.get("5YJ3E1EA1KF000002").tier == "managed"
    assert "TeslaBLE" not in reg.get("5YJ3E1EA1KF000003").claims
    assert "ble" not in master.settings["Vehicles"][VIN]  # seeding writes nothing


def test_update_settings_ignores_entries_without_a_key():  # ruling 6 / P9
    ble, reg, master = make()
    master.settings["Vehicles"] = {VIN: {"chargeSessions": 4, "totalkWh": 12}}
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert reg.vehicles() == []


def test_pairing_failure_status():
    ble, reg, master = make()
    ble.sendPublicKey = Mock(return_value=False)
    assert (
        ble.peerWithVehicle(VIN) is False
        and ble.bleStatus(VIN)["status"] == "pairing_failed"
    )


REFUSED = b"Error: vehicle rejected request: your public key has not been paired with the vehicle"


def fast_retries(ble):
    ble.maxRetries = 2
    ble._calculate_backoff = Mock(return_value=0)


def test_key_refusal_stops_retrying_and_records_once():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    master.queue_background_task.reset_mock()
    fast_retries(ble)
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    assert ble.sendCommand(VIN, "ping") is None
    assert ble._run_command_with_timeout.call_count == 1
    assert master.queue_background_task.call_count == 1
    assert ble.bleStatus(VIN)["status"] == "refused"


def test_other_failures_are_still_retried():
    ble, reg, master = make()
    fast_retries(ble)
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ble: timeout", 1))
    assert ble.sendCommand(VIN, "ping") is None
    assert ble._run_command_with_timeout.call_count == 3


def test_other_failures_leave_status_alone():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ble: timeout", 1))
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert reg.get(VIN).tier == "managed"


def test_state_read_refusal_drops_claim():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    assert ble.get_charge_state(VIN) is None
    assert ble.bleStatus(VIN)["status"] == "refused"
    assert "TeslaBLE" not in reg.get(VIN).claims


def test_already_satisfied_counts_as_proof():
    ble, reg, master = make()
    ble._run_command_with_timeout = Mock(
        return_value=(b"", b"car could not execute command: is_charging", 1)
    )
    assert ble._sendCommand_internal(VIN, "charging-start") is not None
    assert ble.bleStatus(VIN)["status"] == "paired"


def test_pairing_exception_records_failure():
    ble, reg, master = make()
    ble.sendPublicKey = Mock(side_effect=RuntimeError("boom"))
    assert ble.peerWithVehicle(VIN) is False
    assert ble.bleStatus(VIN)["status"] == "pairing_failed"


def test_malformed_vin_is_rejected_without_writing():
    ble, reg, master = make()
    ble.sendPublicKey = Mock(return_value=True)
    before = dict(master.settings["Vehicles"])
    for bad in (None, "", "not-a-vin", "bad\u00e9vin", "5YJ3E1EA1KF00000I"):
        assert ble.peerWithVehicle(bad) is False
        ble._setBleStatus(bad, "paired")
        ble._setBleStatus(bad, "pairing")
    assert master.settings["Vehicles"] == before
    assert ble.sendPublicKey.call_count == 0
    assert reg.vehicles() == []
    master.queue_background_task.assert_not_called()


def test_bad_registry_key_skipped_when_seeding():
    ble, reg, master = make()
    master.settings["Vehicles"]["badévin"] = {"privKey": "q"}
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert reg.get(VIN).claims["TeslaBLE"].status == "unproven"


def test_status_default_and_copy():
    ble, reg, master = make()
    assert ble.bleStatus(VIN) == {"status": "unpaired", "since": None}
    ble._setBleStatus(VIN, "pairing")
    st = ble.bleStatus(VIN)
    st["status"] = "x"
    assert ble.bleStatus(VIN)["status"] == "pairing"


def test_new_record_created_for_unknown_vin():
    ble, reg, master = make()
    other = "5YJ3E1EA1KF000009"
    ble._setBleStatus(other, "paired")
    rec = master.settings["Vehicles"][other]
    assert rec["chargeSessions"] == 0 and rec["ble"]["status"] == "paired"
    assert reg.get(other).tier == "managed"


def test_claimed_vin_reads_from_ble_first():
    ble, reg, master = make()
    master.getModuleByName = Mock(return_value=None)
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    ble._run_command_with_timeout = Mock(
        return_value=(b'{"chargeState": {"batteryLevel": 42}}', b"", 0)
    )
    obs = reg.get(VIN).read("batteryLevel", maxAge=0)
    assert obs is not None and obs.value == 42 and obs.source == "TeslaBLE"
    assert ble.bleStatus(VIN)["status"] == "paired"


def pair(ble):
    ble.sendPublicKey = Mock(return_value=True)
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ok", 0))
    assert ble.peerWithVehicle(VIN) is True


def test_pairing_is_an_unproven_claim():
    ble, reg, master = make()
    pair(ble)
    v = reg.get(VIN)
    assert v.claims["TeslaBLE"].status == "unproven" and v.tier == "observed"


def test_refusal_inside_pairing_window_keeps_pairing():
    ble, reg, master = make()
    pair(ble)
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "pairing"
    assert "TeslaBLE" in reg.get(VIN).claims


def test_refusal_after_pairing_window_is_refused():
    ble, reg, master = make()
    pair(ble)
    master.settings["Vehicles"][VIN]["ble"]["pairingSent"] -= (
        TeslaBLE.PAIRING_WINDOW + 1
    )
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "refused"
    assert "TeslaBLE" not in reg.get(VIN).claims


def test_failed_repair_of_paired_car_keeps_paired():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble.sendPublicKey = Mock(return_value=True)
    ble._run_command_with_timeout = Mock(return_value=(None, None, -9))
    assert ble.peerWithVehicle(VIN) is False
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert reg.get(VIN).tier == "managed"
    attempt = ble.blePairingAttempt(VIN)
    assert attempt["ok"] is False and attempt["message"] and attempt["at"]


def test_failed_repair_without_binary_keeps_paired():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble.binaryPath = None
    assert ble.peerWithVehicle(VIN) is False
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert ble.blePairingAttempt(VIN)["ok"] is False


def test_repair_of_paired_car_keeps_paired_and_refusal_in_window_is_ignored():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    pair(ble)
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert ble.blePairingAttempt(VIN)["ok"] is True
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert reg.get(VIN).tier == "managed"


def test_paired_car_refused_outside_window_is_refused():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "refused"


def test_no_pairing_attempt_by_default():
    ble, reg, master = make()
    assert ble.blePairingAttempt(VIN) is None
