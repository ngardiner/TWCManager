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
    st = ble.bleStatus(VIN)
    assert (st["status"], st["since"]) == ("unpaired", None)
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


# Task 17: range scan and page labels


def scanner(**kw):
    ble, reg, master = make()
    ble.isDockerCached = False
    return ble, reg, master


def test_local_name_matches_tesla_control():
    assert TeslaBLE.bleLocalName("5YJ3E1EA1KF000001") == "Sa3b631dfdbc5cf49C"
    assert TeslaBLE.bleLocalName("5yj3e1ea1kf000001") == "Sa3b631dfdbc5cf49C"


def test_scan_parses_bluetoothctl_output_under_ble_lock():
    ble, reg, master = scanner()

    def run(cmd, timeout, use_process_group=True):
        assert cmd[:4] == ["bluetoothctl", "--timeout", "8", "scan"]
        assert ble._bleLock.locked()
        return (b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n", b"", 0)

    ble._run_command_with_timeout = run
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is True
        assert ble.rangeStatus(VIN)["inRange"] is True
        assert ble.bleStatus(VIN)["label"] == "In range (not paired)"
        ble._run_command_with_timeout = lambda *a, **k: (
            b"[NEW] Device 11:22:33:44:55:66 Other\n",
            b"",
            0,
        )
        assert ble.scanForVehicleInRange(VIN) is False
        assert ble.bleStatus(VIN)["label"] == "Not in range"


def test_scan_unavailable_without_bluetoothctl():
    ble, reg, master = scanner()
    ble._run_command_with_timeout = Mock()
    with patch("TWCManager.Vehicle.TeslaBLE.shutil.which", return_value=None):
        assert ble.scanForVehicleInRange(VIN) is None
        st = ble.bleStatus(VIN)
        assert st["canScan"] is False and st["label"] == "Range unknown"
    ble._run_command_with_timeout.assert_not_called()


def test_scan_unavailable_in_docker():  # ruling 7
    ble, reg, master = scanner()
    ble.isDockerCached = True
    ble._run_command_with_timeout = Mock()
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is None
        assert ble.bleStatus(VIN)["canScan"] is False
    ble._run_command_with_timeout.assert_not_called()


def test_never_scanned_is_range_unknown():
    ble, reg, master = scanner()
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        st = ble.bleStatus(VIN)
    assert st["label"] == "Range unknown" and st["canScan"] is True
    assert st["lastScanAge"] is None and st["scannedAt"] is None


def test_request_scan_queues_one_task_and_runs_every_pending_vin():  # P17
    ble, reg, master = scanner()
    ble.requestRangeScan(VIN)
    ble.requestRangeScan("5YJ3E1EA1KF000002")
    master.queue_background_task.assert_called_with({"cmd": "bleScan"})
    assert ble.bleStatus(VIN)["scanPending"] is True
    ble.scanForVehicleInRange = Mock(return_value=False)
    ble.runPendingScans()
    assert sorted(c.args[0] for c in ble.scanForVehicleInRange.call_args_list) == [
        VIN,
        "5YJ3E1EA1KF000002",
    ]
    assert ble.bleStatus(VIN)["scanPending"] is False


def test_request_during_a_scan_is_served_by_the_same_task():
    ble, reg, master = scanner()
    ble.requestRangeScan(VIN)
    seen = []

    def scan(vin):
        seen.append(vin)
        if len(seen) == 1:
            # Arrives while the task runs; the queue coalesces it away.
            ble.requestRangeScan("5YJ3E1EA1KF000002")

    ble.scanForVehicleInRange = scan
    ble.runPendingScans()
    assert seen == [VIN, "5YJ3E1EA1KF000002"]


def test_request_rejects_malformed_vin():
    ble, reg, master = scanner()
    master.queue_background_task.reset_mock()
    ble.requestRangeScan("not-a-vin")
    ble.requestRangeScan(None)
    master.queue_background_task.assert_not_called()


def test_scan_observes_ble_reach_on_registry_vehicle():
    ble, reg, master = scanner()
    reg.getOrCreate(VIN, "TeslaBLE")
    ble._run_command_with_timeout = lambda *a, **k: (b"", b"", 0)
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        ble.scanForVehicleInRange(VIN)
        st = ble.bleStatus(VIN)
    obs = reg.get(VIN).latest("bleReach")
    assert obs.value is False and obs.source == "TeslaBLE"
    assert st["lastScanAge"] < 1.0


def test_scan_does_not_create_a_registry_vehicle():
    ble, reg, master = scanner()
    ble._run_command_with_timeout = lambda *a, **k: (
        b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n",
        b"",
        0,
    )
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is True
    assert reg.get(VIN) is None


@pytest.mark.parametrize(
    "status,label",
    [
        ("pairing", "Pairing (tap your key card in the car)"),
        ("paired", "Paired"),
        ("pairing_failed", "Pairing failed"),
        ("refused", "Key refused (re-pair)"),
    ],
)
def test_stored_status_labels(status, label):
    ble, reg, master = scanner()
    ble._setBleStatus(VIN, status)
    assert ble.bleStatus(VIN)["label"] == label


def test_status_carries_since_age_and_last_attempt():
    ble, reg, master = scanner()
    pair(ble)
    st = ble.bleStatus(VIN)
    assert st["sinceAge"] is not None and st["sinceAge"] < 1.0
    assert st["attempt"]["ok"] is True and st["attempt"]["message"]


def test_scan_finds_a_car_bluez_already_knows_by_address():
    # Live box: a cached car appears in scan output only by address.
    ble, reg, master = scanner()
    calls = []

    def run(cmd, timeout, use_process_group=True):
        calls.append(cmd)
        assert ble._bleLock.locked()
        if cmd[1:] == ["devices"]:
            return (
                b"Device 11:22:33:44:55:66 Other\n"
                b"Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n",
                b"",
                0,
            )
        return (b"[CHG] Device AA:BB:CC:DD:EE:FF RSSI: 0xffffffb2 (-78)\n", b"", 0)

    ble._run_command_with_timeout = run
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is True
        assert [c[1] for c in calls] == ["--timeout", "devices"]
        # Known to BlueZ but not heard in this scan: not in range.
        run_scan = run
        ble._run_command_with_timeout = lambda cmd, timeout, use_process_group=True: (
            run_scan(cmd, timeout)
            if cmd[1:] == ["devices"]
            else (b"[CHG] Device 11:22:33:44:55:66 RSSI: -60\n", b"", 0)
        )
        assert ble.scanForVehicleInRange(VIN) is False


def test_scan_ignores_bluez_forgetting_the_car():
    ble, reg, master = scanner()

    def run(cmd, timeout, use_process_group=True):
        if cmd[1:] == ["devices"]:
            return (b"Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n", b"", 0)
        return (
            b"[\x1b[0;91mDEL\x1b[0m] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n"
            b"[DEL] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n",
            b"",
            0,
        )

    ble._run_command_with_timeout = run
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is False
        # Colour codes around a [NEW] tag still match.
        ble._run_command_with_timeout = lambda *a, **k: (
            b"[\x1b[0;92mNEW\x1b[0m] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n",
            b"",
            0,
        )
        assert ble.scanForVehicleInRange(VIN) is True


# Task 17 fix round 1


class Runaway(BaseException):
    """Escapes runPendingScans' per-VIN except Exception, so a drain that
    never empties fails the test instead of looping."""


def scan_with(ble, scan_out, devices_out=b""):
    def run(cmd, timeout, use_process_group=True):
        if cmd[1:] == ["devices"]:
            return (devices_out, b"", 0)
        return scan_out

    ble._run_command_with_timeout = run


WHICH = "TWCManager.Vehicle.TeslaBLE.shutil.which"
CAR = b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n"


def test_timed_out_scan_is_range_unknown(caplog):
    ble, reg, master = scanner()
    reg.getOrCreate(VIN, "TeslaBLE")
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        scan_with(ble, (CAR, b"", 0))
        assert ble.scanForVehicleInRange(VIN) is True
        before = reg.get(VIN).latest("bleReach")
        scan_with(ble, (None, None, None))
        with caplog.at_level("WARNING"):
            assert ble.scanForVehicleInRange(VIN) is None
        st = ble.bleStatus(VIN)
    # The unknown result replaces the earlier "in range".
    assert st["inRange"] is None and st["label"] == "Range unknown"
    assert st["lastScanAge"] is not None
    assert reg.get(VIN).latest("bleReach") is before
    assert any(
        r.levelname == "WARNING" and VIN in r.getMessage() for r in caplog.records
    )


def test_timed_out_scan_records_no_ble_reach():
    ble, reg, master = scanner()
    reg.getOrCreate(VIN, "TeslaBLE")
    scan_with(ble, (None, None, None))
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is None
    assert reg.get(VIN).latest("bleReach") is None


def test_failed_scan_without_a_match_is_range_unknown():
    ble, reg, master = scanner()
    reg.getOrCreate(VIN, "TeslaBLE")
    scan_with(ble, (b"[NEW] Device 11:22:33:44:55:66 Other\n", b"", 1))
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is None
        assert ble.bleStatus(VIN)["label"] == "Range unknown"
    assert reg.get(VIN).latest("bleReach") is None


def test_match_wins_over_a_nonzero_exit():
    ble, reg, master = scanner()
    scan_with(ble, (CAR, b"", 1))
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is True


@pytest.mark.parametrize(
    "out",
    [
        (b"No default controller available\n", b"", 0),
        (b"", b"No default controller available\n", 0),
        (b"Waiting to connect to bluetoothd...", b"", 0),
        (b"Failed to start discovery: org.bluez.Error.NotReady\n", b"", 0),
    ],
)
def test_bluez_error_text_is_range_unknown(out):
    ble, reg, master = scanner()
    scan_with(ble, out)
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is None
        assert ble.bleStatus(VIN)["label"] == "Range unknown"


def test_scan_is_pending_while_it_runs():
    ble, reg, master = scanner()
    seen = []

    def run(cmd, timeout, use_process_group=True):
        seen.append(ble.rangeStatus(VIN)["pending"])
        return (CAR, b"", 0)

    ble._run_command_with_timeout = run
    ble.requestRangeScan(VIN)
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        ble.runPendingScans()
        assert seen == [True]
        assert ble.rangeStatus(VIN)["pending"] is False


def test_pending_cleared_when_a_scan_raises():
    ble, reg, master = scanner()
    ble.scanForVehicleInRange = Mock(side_effect=RuntimeError("boom"))
    ble.requestRangeScan(VIN)
    ble.runPendingScans()
    assert ble.rangeStatus(VIN)["pending"] is False


def test_request_within_ten_seconds_of_a_scan_is_ignored():
    ble, reg, master = scanner()
    scan_with(ble, (CAR, b"", 0))
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        ble.scanForVehicleInRange(VIN)
    master.queue_background_task.reset_mock()
    ble.requestRangeScan(VIN)
    master.queue_background_task.assert_not_called()
    assert ble.rangeStatus(VIN)["pending"] is False
    ble._rangeScan[VIN]["at"] -= 11
    ble.requestRangeScan(VIN)
    master.queue_background_task.assert_called_with({"cmd": "bleScan"})
    assert ble.rangeStatus(VIN)["pending"] is True


def test_request_while_the_vin_is_being_scanned_is_ignored():
    ble, reg, master = scanner()
    queued = []

    def scan(vin):
        # Hard cap: a broken debounce must fail, not loop forever.
        if len(queued) >= 3:
            raise Runaway("drain kept rescanning the same VIN")
        master.queue_background_task.reset_mock()
        ble.requestRangeScan(VIN)
        queued.append(master.queue_background_task.called)

    ble.scanForVehicleInRange = scan
    ble.requestRangeScan(VIN)
    ble.runPendingScans()
    assert queued == [False]
    assert ble.rangeStatus(VIN)["pending"] is False


def test_drain_is_bounded_and_leaves_one_follow_up():
    # A request arriving during every scan: the drain stops after
    # SCAN_DRAIN_PASSES and the post-task check queues exactly one follow-up.
    ble, reg, master = scanner()
    vins = ["5YJ3E1EA1KF%06d" % n for n in range(1, 50)]
    seen = []

    def scan(vin):
        if len(seen) >= 40:
            raise Runaway("drain did not stop")
        seen.append(vin)
        ble.requestRangeScan(vins[len(seen)])

    ble.scanForVehicleInRange = scan
    ble.requestRangeScan(vins[0])
    master.queue_background_task.reset_mock()
    ble.runPendingScans()
    assert len(seen) == ble.SCAN_DRAIN_PASSES
    master.queue_background_task.reset_mock()
    ble.requeueIfRequested()
    master.queue_background_task.assert_called_once_with({"cmd": "bleScan"})


def test_mid_scan_request_is_served_without_a_follow_up():
    ble, reg, master = scanner()
    seen = []

    def scan(vin):
        if len(seen) >= 5:
            raise Runaway("drain did not stop")
        seen.append(vin)
        if len(seen) == 1:
            ble.requestRangeScan("5YJ3E1EA1KF000002")

    ble.scanForVehicleInRange = scan
    ble.requestRangeScan(VIN)
    ble.runPendingScans()
    assert seen == [VIN, "5YJ3E1EA1KF000002"]
    master.queue_background_task.reset_mock()
    ble.requeueIfRequested()
    master.queue_background_task.assert_not_called()


def test_requeue_after_the_task_when_requests_remain():
    ble, reg, master = scanner()
    master.queue_background_task.reset_mock()
    ble.requeueIfRequested()
    master.queue_background_task.assert_not_called()
    with ble._scanLock:
        ble._scanRequests[VIN] = __import__("time").time()
    ble.requeueIfRequested()
    master.queue_background_task.assert_called_once_with({"cmd": "bleScan"})


@pytest.mark.parametrize(
    "line",
    [
        b"[CHG] Device AA:BB:CC:DD:EE:FF RSSI is nil\n",
        b"[CHG] Device AA:BB:CC:DD:EE:FF Connected: no\n",
        b"[CHG] Device AA:BB:CC:DD:EE:FF ServicesResolved: no\n",
        b"[CHG] Device AA:BB:CC:DD:EE:FF TxPower: -4\n",
        b"Device AA:BB:CC:DD:EE:FF\n",
    ],
)
def test_mac_fallback_ignores_lines_that_are_not_a_sighting(line):
    ble, reg, master = scanner()
    scan_with(ble, (line, b"", 0), b"Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n")
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is False


@pytest.mark.parametrize(
    "line",
    [
        b"[NEW] Device aa:bb:cc:dd:ee:ff aa-bb-cc-dd-ee-ff\n",
        b"[CHG] Device AA:BB:CC:DD:EE:FF RSSI: 0xffffffb2 (-78)\n",
        b"[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -60\n",
    ],
)
def test_mac_fallback_accepts_new_and_numeric_rssi_lines(line):
    ble, reg, master = scanner()
    scan_with(ble, (line, b"", 0), b"Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n")
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is True


def test_mac_fallback_lookup_timeout_is_range_unknown():
    ble, reg, master = scanner()

    def run(cmd, timeout, use_process_group=True):
        if cmd[1:] == ["devices"]:
            return (None, None, None)
        return (b"[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -60\n", b"", 0)

    ble._run_command_with_timeout = run
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is None


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
    ble._scheduleBlocksStart = Mock(return_value=False)
    ble.wakeVehicle = Mock(return_value=True)
    ble.sendCommand = Mock(return_value="ok")
    ble.parseCommandOutput = Mock(return_value=True)
    return ble, master


def commanded(ble):
    vins = [c.args[0] for c in ble.sendCommand.call_args_list]
    vins += [c.args[0] for c in ble.wakeVehicle.call_args_list]
    return set(vins)


def test_all_vehicle_commands_skip_a_refused_key():  # I2
    ble, master = all_vehicle_ble({VIN: "refused", VIN2: "paired", VIN3: None})
    assert ble.setChargeRate(16) is True
    assert commanded(ble) == {VIN2, VIN3}


def test_all_vehicle_commands_skip_a_vin_without_a_key():  # I2
    ble, master = all_vehicle_ble({VIN2: None})
    master.settings["Vehicles"][VIN] = {"chargeSessions": 3}  # history only
    assert ble.setChargeRate(16) is True
    assert commanded(ble) == {VIN2}


def test_unproven_and_pairing_keys_are_still_commanded():  # I2: upgrade users
    ble, master = all_vehicle_ble({VIN: None, VIN2: "pairing", VIN3: "pairing_failed"})
    ble.updateSettings = Mock()
    assert ble.setChargeRate(16) is True
    assert commanded(ble) == {VIN, VIN2, VIN3}


def test_no_usable_key_fails_the_all_vehicle_command():  # I2
    ble, master = all_vehicle_ble({VIN: "refused"})
    assert ble.setChargeRate(16) is False
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


def test_failed_device_lookup_is_range_unknown():  # M3
    ble, reg, master = scanner()

    def run(cmd, timeout, use_process_group=True):
        if cmd[1:] == ["devices"]:
            return (b"Waiting to connect to bluetoothd...\n", b"", 1)
        return (b"[CHG] Device AA:BB:CC:DD:EE:FF RSSI: -60\n", b"", 0)

    ble._run_command_with_timeout = run
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is None
    assert ble.rangeStatus(VIN)["inRange"] is None
