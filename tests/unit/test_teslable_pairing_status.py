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


def test_update_settings_ignores_hand_edited_entries_without_a_key():  # P9
    # scanForVehicles is mocked: only a hand-edited file has no key here.
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


# Fix round 1: seeding scope, expiry, VIN form and case


def later_boot(master, vins=()):
    # A saved registry blob means this is not the upgrade boot.
    master.settings["vehicleRegistry"] = {
        "version": 1,
        "vehicles": {v: {"lastSeen": 1.0} for v in vins},
    }


def test_upgrade_boot_real_scan_keys_and_seeds_every_entry():
    ble, reg, master = make()
    master.settings["Vehicles"] = {VIN: {"chargeSessions": 4, "totalkWh": 12}}
    ble.updateSettings()
    assert "privKey" in master.settings["Vehicles"][VIN]
    assert reg.get(VIN).claims["TeslaBLE"].status == "unproven"


def test_unproven_only_car_ages_out_but_paired_does_not():
    ble, reg, master = make()
    master.settings["Vehicles"]["5YJ3E1EA1KF000002"] = {
        "privKey": "y",
        "ble": {"status": "paired", "since": 1.0},
    }
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    for v in reg.vehicles():
        v.lastSeen -= 365 * 86400
    assert reg.expire() == [VIN]
    assert reg.get("5YJ3E1EA1KF000002").tier == "managed"


def test_later_boot_does_not_recreate_expired_or_forgotten_car():
    ble, reg, master = make()
    later_boot(master)
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert reg.get(VIN) is None


def test_later_boot_claims_held_and_paired_vins():
    ble, reg, master = make()
    held, paired, refused = VIN, "5YJ3E1EA1KF000002", "5YJ3E1EA1KF000003"
    master.settings["Vehicles"] = {
        held: {"privKey": "x"},
        paired: {"privKey": "y", "ble": {"status": "paired", "since": 1.0}},
        refused: {"privKey": "z", "ble": {"status": "refused", "since": 1.0}},
    }
    later_boot(master, [held, refused])
    reg.load(master.settings["vehicleRegistry"])
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert reg.get(held).claims["TeslaBLE"].status == "unproven"
    assert reg.get(paired).tier == "managed"
    assert "TeslaBLE" not in reg.get(refused).claims


def test_seed_skips_malformed_vins():
    ble, reg, master = make()
    master.settings["Vehicles"]["NOT-A-VIN"] = {"privKey": "q"}
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert reg.get("NOT-A-VIN") is None and reg.get(VIN) is not None


def test_lowercase_form_vin_uses_the_stored_record():
    ble, reg, master = make()
    master.settings["Vehicles"][VIN]["pubKeyPEM"] = "cA=="
    ble.sendPublicKey = Mock(return_value=True)
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ok", 0))
    assert ble.peerWithVehicle(VIN.lower()) is True
    assert list(master.settings["Vehicles"]) == [VIN]
    assert ble.bleStatus(VIN.lower())["status"] == "pairing"
    assert ble.bleStatus(VIN)["status"] == "pairing"
    assert ble.blePairingAttempt(VIN.lower())["ok"] is True


def test_legacy_lowercase_record_is_found_by_upper_vin():
    ble, reg, master = make()
    master.settings["Vehicles"] = {VIN.lower(): {"privKey": "x"}}
    ble._run_command_with_timeout = Mock(
        return_value=(b'{"chargeState": {"batteryLevel": 7}}', b"", 0)
    )
    assert ble.get_charge_state(VIN)["batteryLevel"] == 7
    assert list(master.settings["Vehicles"]) == [VIN.lower()]
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert reg.get(VIN).tier == "managed"


def test_pairing_failure_keeps_an_unproven_claim():
    ble, reg, master = make()
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    ble.sendPublicKey = Mock(return_value=False)
    assert ble.peerWithVehicle(VIN) is False
    assert ble.bleStatus(VIN)["status"] == "pairing_failed"
    assert reg.get(VIN).claims["TeslaBLE"].status == "unproven"
    # Restart: the registry holds the car, so it is claimed again.
    later_boot(master, [VIN])
    ble.updateSettings()
    assert reg.get(VIN).claims["TeslaBLE"].status == "unproven"


VIN2 = "5YJ3E1EA1KF000002"
VIN3 = "5YJ3E1EA1KF000003"


def all_vehicle_ble(statuses):
    # One keyed car per entry: status None is a key with no recorded pairing
    # state (as every pre-1.4.0 key, seeded unproven on the upgrade boot).
    ble, reg, master = make()
    master.settings["Vehicles"] = {}
    for vin, status in statuses.items():
        rec = {"privKey": "x"}
        if status:
            rec["ble"] = {"status": status, "since": 1.0}
        master.settings["Vehicles"][vin] = rec
    master.getNormalChargeLimit = Mock(return_value=(False, None, None))
    ble._scheduleBlocksStart = Mock(return_value=False)
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    ble.startCharging = Mock(return_value=True)
    ble.stopCharging = Mock(return_value=True)
    return ble, master


def commanded(ble):
    vins = [c.args[0] for c in ble.sendCommand.call_args_list]
    vins += [c.args[0] for c in ble.wakeVehicle.call_args_list]
    vins += [c.args[0] for c in ble.startCharging.call_args_list]
    vins += [c.args[0] for c in ble.stopCharging.call_args_list]
    return set(vins)


def test_all_vehicle_commands_skip_a_refused_key():  # I2
    ble, master = all_vehicle_ble({VIN: "refused", VIN2: "paired", VIN3: None})
    assert ble.car_api_charge({"cmd": "charge", "charge": True}) is True
    assert commanded(ble) == {VIN2, VIN3}
    ble, master = all_vehicle_ble({VIN: "refused", VIN2: "paired", VIN3: None})
    assert ble.setChargeRate(16) is True
    assert commanded(ble) == {VIN2, VIN3}
    ble, master = all_vehicle_ble({VIN: "refused", VIN2: "paired", VIN3: None})
    assert ble.applyChargeLimit(80) is True
    assert commanded(ble) == {VIN2, VIN3}


def test_all_vehicle_commands_skip_a_vin_without_a_key():  # I2
    ble, master = all_vehicle_ble({VIN2: None})
    master.settings["Vehicles"][VIN] = {"chargeSessions": 3}  # history only
    assert ble.setChargeRate(16) is True
    assert commanded(ble) == {VIN2}


def test_unproven_and_pairing_keys_are_still_commanded():  # I2: upgrade users
    ble, master = all_vehicle_ble({VIN: None, VIN2: "pairing", VIN3: "pairing_failed"})
    ble.updateSettings = Mock()
    assert ble.car_api_charge({"cmd": "charge", "charge": False}) is True
    assert commanded(ble) == {VIN, VIN2, VIN3}


def test_no_usable_key_fails_the_all_vehicle_command():  # I2
    ble, master = all_vehicle_ble({VIN: "refused"})
    assert ble.car_api_charge({"cmd": "charge", "charge": True}) is False
    assert ble.setChargeRate(16) is False
    assert ble.applyChargeLimit(80) is False
    assert commanded(ble) == set()


def test_pairing_that_could_not_start_creates_no_vehicle_record():  # M7
    ble, reg, master = make()
    ble.sendPublicKey = Mock(return_value=False)  # key preparation failed
    assert ble.peerWithVehicle(VIN2) is False
    assert VIN2 not in master.settings["Vehicles"] and reg.get(VIN2) is None
    assert {"cmd": "saveSettings"} not in [
        c.args[0] for c in master.queue_background_task.call_args_list
    ]
    assert "public key" in ble.blePairingAttempt(VIN2)["message"]
    # A known car still records the failure.
    assert ble.peerWithVehicle(VIN) is False
    assert ble.bleStatus(VIN)["status"] == "pairing_failed"
