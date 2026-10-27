# tests/unit/test_teslable_pairing_status.py
from unittest.mock import Mock, patch
import threading
import time
import pytest
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
from TWCManager.Vehicle.Reconcile.Run import entryKey
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from tests.unit.reconcile_helpers import entries, runPending
from tests.unit.vehicle_helpers import commands

VIN = "5YJ3E1EA1KF000001"
# What tesla-control prints once the car has taken an add-key request
SENT = (
    b"Sent add-key request to %s. Confirm by tapping NFC card on center console.\n"
    % VIN.encode()
)


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
    ble._run_command_with_timeout = Mock(return_value=(b"", SENT, 0))
    assert ble.peerWithVehicle(VIN) is True
    assert ble.bleStatus(VIN)["status"] == "pairing"
    assert reg.get(VIN) is None or not commands(reg.get(VIN))
    ble._run_command_with_timeout = Mock(return_value=(b"", b"done", 0))
    assert ble._sendCommand_internal(VIN, "ping") == "done"
    assert ble.bleStatus(VIN)["status"] == "paired" and commands(reg.get(VIN))
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
    assert ble.bleStatus(VIN)["status"] == "refused" and not commands(reg.get(VIN))


def test_state_read_success_proves_key():
    ble, reg, master = make()
    ble._run_command_with_timeout = Mock(
        return_value=(b'{"chargeState": {"batteryLevel": 5}}', b"", 0)
    )
    assert (
        ble.get_charge_state(VIN)["batteryLevel"] == 5
        and ble.bleStatus(VIN)["status"] == "paired"
    )


def test_charge_read_reports_the_cars_own_ac_maximum():
    ble, reg, master = make()
    ble._run_command_with_timeout = Mock(
        return_value=(
            b'{"chargeState": {"chargerPilotCurrent": 6, "chargeCurrentRequestMax": 48}}',
            b"",
            0,
        )
    )
    state = ble.get_charge_state(VIN)
    assert state["availableCurrent"] == 6 and state["chargeCurrentRequestMax"] == 48
    (p,) = ble.providers()
    v = reg.getOrCreate(VIN, "t")
    p.fetch(v, "chargingState")
    assert v.value("chargeCurrentRequestMax") == 48


def test_update_settings_reclaims_paired_vins():
    ble, reg, master = make()
    master.settings["Vehicles"][VIN]["ble"] = {"status": "paired", "since": 1.0}
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    assert commands(reg.get(VIN))


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
        not commands(reg.get(VIN))
        and reg.get(VIN).claims["TeslaBLE"].status == "unproven"
    )
    assert commands(reg.get("5YJ3E1EA1KF000002"))
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


def test_key_refusal_stops_retrying_and_records_once():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    master.queue_background_task.reset_mock()
    ble._run_command_with_timeout = Mock(return_value=(b"", REFUSED, 1))
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep") as sleep:
        assert ble.sendCommand(VIN, "ping") is None
    assert ble._run_command_with_timeout.call_count == 1 and sleep.call_count == 0
    assert master.queue_background_task.call_count == 1
    assert ble.bleStatus(VIN)["status"] == "refused"


@pytest.mark.parametrize(
    "failure",
    [
        (None, None, None),
        (b"", b"", -9),
        (b"", b"ble: Connection failed", 1),
        (b"", b"ble: timeout", 1),
    ],
    ids=["timeout", "killed", "connection", "timeout-output"],
)
def test_a_transient_failure_is_retried_once_then_reported(failure):
    from TWCManager.Vehicle.Reconcile import Outcome as O

    ble, reg, master = make()
    (p,) = ble.providers()
    ble._run_command_with_timeout = Mock(return_value=failure)
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep") as sleep:
        assert p.setAmps(VIN, 10) == O.Outcome(O.UNREACHABLE, "no reply")
    assert ble._run_command_with_timeout.call_count == 2
    sleep.assert_called_once_with(2.0)
    assert TeslaBLE.QUICK_RETRY_DELAY == 2.0


def test_a_transient_failure_then_success_is_done():
    from TWCManager.Vehicle.Reconcile import Outcome as O

    ble, reg, master = make()
    (p,) = ble.providers()
    ble._run_command_with_timeout = Mock(
        side_effect=[(None, None, None), (b"", b"Command executed successfully", 0)]
    )
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep"):
        assert p.setAmps(VIN, 10).kind == O.DONE
    assert ble._run_command_with_timeout.call_count == 2


@pytest.mark.parametrize(
    "stderr",
    [b"Error: car could not execute command: not_allowed", b""],
)
def test_a_non_transient_failure_is_not_retried(stderr):
    from TWCManager.Vehicle.Reconcile import Outcome as O

    ble, reg, master = make()
    (p,) = ble.providers()
    ble._run_command_with_timeout = Mock(return_value=(b"", stderr, 1))
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep") as sleep:
        assert p.setAmps(VIN, 10) == O.Outcome(O.UNREACHABLE, "no reply")
    assert ble._run_command_with_timeout.call_count == 1 and sleep.call_count == 0


def test_a_command_rejected_before_running_is_not_retried():
    ble, reg, master = make()
    ble._run_command_with_timeout = Mock()
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep") as sleep:
        assert ble.sendCommand("5YJ3E1EA1KF000009", "ping") is None  # not in settings
    assert ble._run_command_with_timeout.call_count == 0 and sleep.call_count == 0


def test_removed_retry_config_keys_are_ignored():
    master_config = {
        "maxRetries": 9,
        "retryDelay": 30,
        "circuitBreakerThreshold": 1,
        "circuitBreakerTimeout": 3600,
    }
    reg = VehicleRegistry({})
    master = Mock()
    master.vehicleRegistry = reg
    master.settings = {"Vehicles": {VIN: {"privKey": "x"}}}
    master.settingsLock = threading.RLock()
    master.config = {
        "config": {
            "teslaControlPath": "/bin/true",
            "moduleConfiguration": {"TeslaBLE": master_config},
        },
        "vehicle": {"teslaBLE": {"enabled": True}},
    }
    with patch.object(TeslaBLE, "_start_dbus_session"), patch.object(
        TeslaBLE, "_cleanup_stale_pipe"
    ):
        ble = TeslaBLE(master)
    ble.sendPrivateKey = Mock(return_value=True)
    ble._ensure_pipe_closed = Mock()
    for name in ("maxRetries", "retryDelay", "circuit_breaker", "retry_stats"):
        assert not hasattr(ble, name)
    ble._run_command_with_timeout = Mock(return_value=(None, None, None))
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep") as sleep:
        for _ in range(3):  # a breaker at threshold 1 would stop the later sends
            assert ble.sendCommand(VIN, "ping") is None
    assert ble._run_command_with_timeout.call_count == 6
    assert [c.args for c in sleep.call_args_list] == [(2.0,)] * 3


def test_other_failures_leave_status_alone():
    ble, reg, master = make()
    ble._setBleStatus(VIN, "paired")
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ble: timeout", 1))
    assert ble._sendCommand_internal(VIN, "ping") is None
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert commands(reg.get(VIN))


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
    assert commands(reg.get(other))


def test_claimed_vin_reads_from_ble_first():
    from tests.unit.teslatokens import wire_reads

    ble, reg, master = make()
    wire_reads(reg, *ble.providers())
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
    ble._run_command_with_timeout = Mock(return_value=(b"", SENT, 0))
    assert ble.peerWithVehicle(VIN) is True


def test_pairing_is_an_unproven_claim():
    ble, reg, master = make()
    pair(ble)
    v = reg.get(VIN)
    assert v.claims["TeslaBLE"].status == "unproven" and not commands(v)


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
    assert commands(reg.get(VIN))
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
    assert commands(reg.get(VIN))


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
    assert commands(reg.get("5YJ3E1EA1KF000002"))


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
    assert commands(reg.get(paired))
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
    ble._run_command_with_timeout = Mock(return_value=(b"", SENT, 0))
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
    assert commands(reg.get(VIN))


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
    # A BLE worker that runs only when a test drains or starts it
    master.reconciler = VehicleReconciler(master)
    # The readiness check found a powered adapter
    ble._scanBlocked, ble._scanCheckAt = None, time.time()
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


def test_one_scan_answers_for_several_cars():
    ble, reg, master = scanner()
    TWO = "5YJ3E1EA1KF000002"
    name = ble.bleLocalName(TWO).encode()
    ble._run_command_with_timeout = Mock(
        return_value=(b"[NEW] Device AA:BB:CC:DD:EE:FF " + name + b"\n", b"", 0)
    )
    ble._bluezAddresses = Mock(return_value={})
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange([VIN, TWO]) is True
    assert ble._run_command_with_timeout.call_count == 1
    assert ble.rangeStatus(TWO)["inRange"] is True
    assert ble.rangeStatus(VIN)["inRange"] is False


BT = "TWCManager.Vehicle.TeslaBLE.shutil.which"
SHOW_READY = (b"Controller 00:1A:7D:DA:71:13 (public)\n\tPowered: yes\n", b"", 0)


def checked(ble, show, which="/usr/bin/bluetoothctl", docker=False):
    # One readiness check on the BLE worker, as canScan queues it
    ble._scanCheckAt, ble.isDockerCached = None, docker
    ble._run_command_with_timeout = Mock(return_value=show)
    with patch(BT, return_value=which):
        ble.canScan()  # queues the check; never runs it on the caller
        ble._run_command_with_timeout.assert_not_called()
        runPending(ble.master.reconciler.ble)
        return ble.canScan()


def test_docker_scans_through_dbus_without_nsenter():
    ble, reg, master = scanner()
    ble._scanBlocked = ""
    assert checked(ble, SHOW_READY, docker=True) is True
    ble._run_command_with_timeout.assert_called_once_with(["bluetoothctl", "show"], 5)
    run = ble._run_command_with_timeout = Mock(return_value=(b"", b"", 0))
    with patch(BT, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is False
    assert run.call_args.args[0][0] == "bluetoothctl"


@pytest.mark.parametrize(
    "which, show, docker, reason",
    [
        (None, None, False, "bluetoothctl not installed (install the bluez package)"),
        (
            "/usr/bin/bluetoothctl",
            (b"No default controller available\n", b"", 0),
            False,
            "no Bluetooth controller visible to bluetoothd",
        ),
        (
            "/usr/bin/bluetoothctl",
            (b"Controller 00:1A:7D:DA:71:13\n\tPowered: no\n", b"", 0),
            True,
            "Bluetooth adapter is powered off or blocked (rfkill)",
        ),
        (
            "/usr/bin/bluetoothctl",
            (None, None, None),
            True,
            "bluetoothctl cannot reach the host's bluetoothd (mount /run/dbus"
            " and set DBUS_SYSTEM_BUS_ADDRESS)",
        ),
        (
            "/usr/bin/bluetoothctl",
            (b"Waiting to connect to bluetoothd...", b"", 1),
            False,
            "bluetoothd is not answering",
        ),
    ],
)
def test_each_reason_no_scan_is_possible(which, show, docker, reason):
    ble, reg, master = scanner()
    assert checked(ble, show, which, docker) is False
    st = ble.bleStatus(VIN)
    assert st["canScan"] is False and st["scanBlocked"] == reason
    ble._run_command_with_timeout.reset_mock()
    with patch(BT, return_value=which):
        assert ble.scanForVehicleInRange(VIN) is None
    ble._run_command_with_timeout.assert_not_called()


def test_warns_once_per_reason_and_recovers_once(caplog):
    ble, reg, master = scanner()
    ble._scanBlocked = ""
    off = (b"\tPowered: no\n", b"", 0)
    gone = (b"No default controller available\n", b"", 0)
    with caplog.at_level("INFO"):
        for show in (off, off, gone, gone, SHOW_READY, SHOW_READY, off):
            checked(ble, show)
    logs = [
        (r.levelname, r.getMessage().split(";")[0])
        for r in caplog.records
        if "range scans" in r.getMessage()
    ]
    assert logs == [
        ("WARNING", "BLE range scans unavailable: " + ble.SCAN_BLOCKERS[2][1]),
        ("WARNING", "BLE range scans unavailable: " + ble.SCAN_BLOCKERS[0][1]),
        ("INFO", "BLE range scans available again"),
        ("WARNING", "BLE range scans unavailable: " + ble.SCAN_BLOCKERS[2][1]),
    ]
    assert "cars watched by the Tesla API" in caplog.text


def test_a_failed_check_reruns_no_sooner_than_ten_minutes():
    ble, reg, master = scanner()
    checked(ble, (b"\tPowered: no\n", b"", 0))
    submit = master.reconciler.submitJob = Mock(return_value=True)
    start = ble._scanCheckAt
    with patch("TWCManager.Vehicle.TeslaBLE.time.time", return_value=start + 599):
        assert ble.canScan() is False
    submit.assert_not_called()
    with patch("TWCManager.Vehicle.TeslaBLE.time.time", return_value=start + 600):
        assert ble.canScan() is False
        assert ble.canScan() is False
    assert submit.call_count == 1  # once per due check, however often asked


@pytest.mark.parametrize(
    "scan",
    [(b"No default controller available\n", b"", 1), (None, None, None)],
    ids=["no adapter", "timed out"],
)
def test_a_passed_check_is_not_rerun_until_a_scan_fails(scan):
    ble, reg, master = scanner()
    submit = master.reconciler.submitJob = Mock(return_value=True)
    with patch(
        "TWCManager.Vehicle.TeslaBLE.time.time", return_value=time.time() + 9999
    ):
        assert ble.canScan() is True
    submit.assert_not_called()
    ble._run_command_with_timeout = Mock(return_value=scan)
    with patch(BT, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is None
    ble.canScan()
    submit.assert_called_once()


def test_startup_queues_the_check():
    ble, reg, master = make()
    master.reconciler.submitJob.assert_called_once_with(
        "radio", "check", ble._checkScan
    )


def test_a_scan_before_the_check_ran_checks_first():
    ble, reg, master = scanner()
    ble._scanBlocked = ""  # queued behind other work, not yet run
    out = (b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n", b"", 0)
    run = ble._run_command_with_timeout = Mock(side_effect=[SHOW_READY, out])
    with patch(BT, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is True
    assert run.call_args_list[0].args[0] == ["bluetoothctl", "show"]


def test_an_unchecked_result_is_retried_after_ten_minutes():
    # The queued check was dropped (worker stopped, queue overflow, raised)
    ble, reg, master = scanner()
    ble._scanBlocked = ""
    submit = master.reconciler.submitJob = Mock(return_value=True)
    start = ble._scanCheckAt
    with patch("TWCManager.Vehicle.TeslaBLE.time.time", return_value=start + 599):
        assert ble.canScan() is False
    submit.assert_not_called()
    with patch("TWCManager.Vehicle.TeslaBLE.time.time", return_value=start + 600):
        assert ble.canScan() is False
    submit.assert_called_once()


def test_docker_tesla_control_runs_under_nsenter_in_its_own_group():
    ble, reg, master = scanner()
    ble.isDockerCached = True
    with patch("TWCManager.Vehicle.TeslaBLE.subprocess.Popen") as popen:
        popen.return_value.communicate.return_value = (b"", b"")
        ble._run_command_with_timeout([ble.binaryPath, "-ble", "x"], 5)
        ble._run_command_with_timeout(["bluetoothctl", "show"], 5)
    (tc, _), (bt, _) = [(c.args[0], c) for c in popen.call_args_list]
    assert tc == ["nsenter", "--net=/rootns/net", ble.binaryPath, "-ble", "x"]
    assert bt == ["bluetoothctl", "show"]
    assert all(c.kwargs["preexec_fn"] is not None for c in popen.call_args_list)


def test_never_scanned_is_range_unknown():
    ble, reg, master = scanner()
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        st = ble.bleStatus(VIN)
    assert st["label"] == "Range unknown" and st["canScan"] is True
    assert st["lastScanAge"] is None and st["scannedAt"] is None


def jobs(master):
    # The scans waiting on the BLE worker, as (vin, kind)
    return [entryKey(j) for j in entries(master.reconciler.ble)]


def test_requests_share_one_scan_of_every_pending_vin():  # P17
    ble, reg, master = scanner()
    ble.requestRangeScan(VIN)
    ble.requestRangeScan("5YJ3E1EA1KF000002")
    assert jobs(master) == [("radio", "scan")]
    assert ble.bleStatus(VIN)["scanPending"] is True
    ble.scanForVehicleInRange = Mock(return_value=False)
    runPending(master.reconciler.ble)
    assert [c.args[0] for c in ble.scanForVehicleInRange.call_args_list] == [
        {VIN, "5YJ3E1EA1KF000002"}
    ]
    assert ble.bleStatus(VIN)["scanPending"] is False


def test_request_during_a_scan_queues_behind_it():
    ble, reg, master = scanner()
    ble.requestRangeScan(VIN)
    seen = []

    def scan(vin):
        seen.append(vin)
        if len(seen) == 1:
            # Arrives while the worker scans
            ble.requestRangeScan("5YJ3E1EA1KF000002")

    ble.scanForVehicleInRange = scan
    runPending(master.reconciler.ble)
    assert seen == [{VIN}, {"5YJ3E1EA1KF000002"}]


def test_request_rejects_malformed_vin():
    ble, reg, master = scanner()
    ble.requestRangeScan("not-a-vin")
    ble.requestRangeScan(None)
    assert jobs(master) == []


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


def test_a_scan_looks_addresses_up_only_for_an_asked_car_it_missed():
    # Another registry car out of range is the usual case, not a reason for
    # a second bluetoothctl run.
    ble, reg, master = scanner()
    reg.getOrCreate("5YJ3E1EA1KF000002", "t")
    calls = []

    def run(cmd, timeout, use_process_group=True):
        calls.append(cmd[1:])
        return (
            b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n"
            b"[CHG] Device 11:22:33:44:55:66 RSSI: -60\n",
            b"",
            0,
        )

    ble._run_command_with_timeout = run
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is True
    assert ["devices"] not in calls


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
    """Escapes the worker's except Exception, so a scan that keeps queueing
    itself fails the test instead of looping."""


def scan_with(ble, scan_out, devices_out=b""):
    def run(cmd, timeout, use_process_group=True):
        if cmd[1:] == ["devices"]:
            return (devices_out, b"", 0)
        return scan_out

    ble._run_command_with_timeout = run


WHICH = "TWCManager.Vehicle.TeslaBLE.shutil.which"
CAR = b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n"


def test_a_car_not_asked_for_is_recorded_only_when_heard():
    ble, reg, master = scanner()
    other = "5YJ3E1EA1KF000002"
    reg.getOrCreate(VIN, "t")
    reg.getOrCreate(other, "t")
    heard = "[NEW] Device AA:BB:CC:DD:EE:01 %s\n" % TeslaBLE.bleLocalName(other)
    scan_with(ble, (heard.encode(), b"", 0))
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        assert ble.scanForVehicleInRange(VIN) is False
        assert reg.get(other).latest("bleReach").value is True  # a sighting
        assert reg.get(VIN).latest("bleReach").value is False
        scan_with(ble, (CAR, b"", 0))  # hears VIN only
        assert ble.scanForVehicleInRange(VIN) is True
    assert reg.get(other).latest("bleReach").value is True  # never a miss


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
        runPending(master.reconciler.ble)
        assert seen == [True]
        assert ble.rangeStatus(VIN)["pending"] is False


def test_pending_cleared_when_a_scan_raises():
    ble, reg, master = scanner()
    ble.scanForVehicleInRange = Mock(side_effect=RuntimeError("boom"))
    ble.requestRangeScan(VIN)
    runPending(master.reconciler.ble)
    assert ble.rangeStatus(VIN)["pending"] is False


def test_request_within_ten_seconds_of_a_scan_is_ignored():
    ble, reg, master = scanner()
    scan_with(ble, (CAR, b"", 0))
    with patch(WHICH, return_value="/usr/bin/bluetoothctl"):
        ble.scanForVehicleInRange(VIN)
    ble.requestRangeScan(VIN)
    assert jobs(master) == []
    assert ble.rangeStatus(VIN)["pending"] is False
    ble._rangeScan[VIN]["at"] -= 11
    ble.requestRangeScan(VIN)
    assert jobs(master) == [("radio", "scan")]
    assert ble.rangeStatus(VIN)["pending"] is True


def test_request_while_the_vin_is_being_scanned_is_ignored():
    ble, reg, master = scanner()
    queued = []

    def scan(vin):
        # Hard cap: a broken debounce must fail, not loop forever.
        if len(queued) >= 3:
            raise Runaway("drain kept rescanning the same VIN")
        ble.requestRangeScan(VIN)
        queued.append(bool(jobs(master)))

    ble.scanForVehicleInRange = scan
    ble.requestRangeScan(VIN)
    runPending(master.reconciler.ble)
    assert queued == [False]
    assert ble.rangeStatus(VIN)["pending"] is False


def test_mid_scan_request_is_served_by_the_worker_once():
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
    runPending(master.reconciler.ble)
    assert seen == [{VIN}, {"5YJ3E1EA1KF000002"}]
    assert jobs(master) == []


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


def test_a_scan_hit_is_a_sighting_and_a_miss_is_not():
    ble, reg, master = scanner()
    v = reg.getOrCreate(VIN, "TeslaBLE")
    seen = v.lastSeen

    def scan(out):
        ble._run_command_with_timeout = lambda *a, **k: (out, b"", 0)
        with patch(
            "TWCManager.Vehicle.TeslaBLE.shutil.which",
            return_value="/usr/bin/bluetoothctl",
        ):
            return ble.scanForVehicleInRange(VIN)

    assert scan(b"[NEW] Device 11:22:33:44:55:66 Other\n") is False
    assert v.lastSeen == seen
    assert scan(b"[NEW] Device AA:BB:CC:DD:EE:FF Sa3b631dfdbc5cf49C\n") is True
    assert v.lastSeen > seen and v.lastSeenSource == "TeslaBLE"


def test_the_last_scan_survives_a_restart():
    ble, reg, master = scanner()
    ble._run_command_with_timeout = lambda *a, **k: (b"", b"", 0)
    with patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which",
        return_value="/usr/bin/bluetoothctl",
    ):
        assert ble.scanForVehicleInRange(VIN) is False
    master.queue_background_task.assert_any_call({"cmd": "saveSettings"})
    saved = master.settings["Vehicles"][VIN]["ble"]["scan"]
    assert saved["inRange"] is False

    # A new module over the same settings
    with patch.object(TeslaBLE, "_start_dbus_session"), patch.object(
        TeslaBLE, "_cleanup_stale_pipe"
    ):
        again = TeslaBLE(master)
    assert again.rangeStatus(VIN) == {**saved, "pending": False}
    # Pairing writes the status beside it without dropping it
    again._setBleStatus(VIN, "paired")
    assert master.settings["Vehicles"][VIN]["ble"]["scan"] == saved


def test_a_scan_of_a_car_with_no_record_is_kept_in_memory_only():
    ble, reg, master = scanner()
    master.settings["Vehicles"] = {}
    ble._noteScan(VIN, True)
    assert ble.rangeStatus(VIN)["inRange"] is True
    assert master.settings["Vehicles"] == {}


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


def test_pairing_debug_output_alone_is_not_a_sent_request():
    # -debug output mentions "OAuth token", which matched parseCommandOutput's
    # "ok" and made every attempt look sent
    ble, reg, master = make()
    ble.sendPublicKey = Mock(return_value=True)
    output = b"[debug] Set OAuth token name to ''\n[debug] Connecting over BLE...\n"
    ble._run_command_with_timeout = Mock(return_value=(b"", output, 1))
    assert ble.peerWithVehicle(VIN) is False
    assert ble.bleStatus(VIN)["status"] != "pairing"


def test_charge_state_maps_the_cars_own_current_request():  # slim Task 12b
    ble, reg, master = make()
    ble._fetch_state_json = Mock(
        return_value={
            "chargeState": {"chargeCurrentRequest": 16, "chargerPilotCurrent": 32}
        }
    )
    state = ble.get_charge_state(VIN)
    assert state["chargeCurrentRequest"] == 16 and state["availableCurrent"] == 32


def test_pairing_and_pairing_failed_keys_are_claimed_and_commandable():  # 12b deleted-test PARTIAL (upgrade users)
    from TWCManager.Vehicle.Providers import Capability as C

    ble, reg, master = make()
    vins = {"5YJ3E1EA1KF000002": "pairing", "5YJ3E1EA1KF000003": "pairing_failed"}
    master.settings["Vehicles"] = {
        vin: {"privKey": "x", "ble": {"status": status, "since": 1.0}}
        for vin, status in vins.items()
    }
    ble.scanForVehicles = Mock()
    ble.updateSettings()
    (p,) = ble.providers()
    for vin in vins:
        assert "TeslaBLE" in reg.get(vin).claims and p.ready(reg.get(vin), C.SET_AMPS)


# tesla-control -debug logs to stderr before its final error line
DEBUG_PREAMBLE = (
    b"2026-10-15T00:00:00Z [debug] Connecting to car...\n"
    b"2026-10-15T00:00:01Z [info ] Connecting to car...\n"
    b"2026-10-15T00:00:02Z [info ] Securing connection...\n"
    b"2026-10-15T00:00:03Z [warn ] timeout waiting for session info, retrying\n"
)


@pytest.mark.parametrize(
    "last_line, runs",
    [
        (b"Failed to execute command: car could not execute command: not_allowed", 1),
        (b"Error: failed to find BLE beacon: context deadline exceeded", 2),
        (b"Failed to execute command: ble: connection lost", 2),
        (b"Failed to execute command: request timeout", 2),
    ],
    ids=["not_allowed", "deadline", "connection", "timeout"],
)
def test_only_the_final_error_line_decides_a_quick_retry(last_line, runs):
    from TWCManager.Vehicle.Reconcile import Outcome as O

    ble, reg, master = make()
    (p,) = ble.providers()
    ble._run_command_with_timeout = Mock(
        return_value=(b"", DEBUG_PREAMBLE + last_line + b"\n", 1)
    )
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep") as sleep:
        assert p.setAmps(VIN, 10) == O.Outcome(O.UNREACHABLE, "no reply")
    assert ble._run_command_with_timeout.call_count == runs
    assert sleep.call_count == runs - 1


def test_a_stored_refused_key_is_refused_even_when_the_run_times_out():
    from TWCManager.Vehicle.Reconcile import Outcome as O

    ble, reg, master = make()
    (p,) = ble.providers()
    ble._setBleStatus(VIN, "refused")
    ble._run_command_with_timeout = Mock(return_value=(None, None, None))
    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep"):
        assert p.setAmps(VIN, 10) == O.Outcome(O.REFUSED, "key")


def test_the_quick_retry_is_logged_at_info_once(caplog):
    import logging

    ble, reg, master = make()
    ble._run_command_with_timeout = Mock(return_value=(None, None, None))
    from TWCManager.Vehicle import TeslaBLE as bleModule

    level = bleModule.logger.level
    bleModule.logger.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.INFO), patch(
            "TWCManager.Vehicle.TeslaBLE.time.sleep"
        ):
            assert ble.sendCommand(VIN, "wake") is None
    finally:
        bleModule.logger.setLevel(level)
    retries = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "retrying" in r.getMessage()
    ]
    assert len(retries) == 1 and "wake" in retries[0].getMessage()


AWAKE_OPEN = b'{"closureStatuses":{"chargePort":"CLOSURESTATE_OPEN"},"vehicleSleepStatus":"VEHICLE_SLEEP_STATUS_AWAKE","userPresence":"VEHICLE_USER_PRESENCE_NOT_PRESENT"}'
ASLEEP = b'{"vehicleSleepStatus":"VEHICLE_SLEEP_STATUS_ASLEEP","userPresence":"VEHICLE_USER_PRESENCE_NOT_PRESENT"}'


def test_ping_records_the_door_while_awake_and_nothing_asleep():
    ble, reg, master = scanner()
    v = reg.getOrCreate(VIN, "t")
    ble._tool = lambda vin, *args: (AWAKE_OPEN, b"", 0)
    assert ble.ping(VIN) is True
    assert v.value("state") == "online" and v.value("chargePortOpen") is True
    ble._tool = lambda vin, *args: (ASLEEP, b"", 0)
    assert ble.ping(VIN) is True
    assert v.value("state") == "asleep" and v.latest("chargePortOpen").value is True


def test_ping_runs_for_a_vin_with_no_key_and_no_settings_record():
    ble, reg, master = scanner()
    master.settings["Vehicles"] = {}
    v = reg.getOrCreate(VIN, "t")  # no TeslaBLE claim, no settings record
    assert not master.settings.get("Vehicles", {}).get(VIN)
    runs = []
    ble._run_command_with_timeout = lambda cmd, timeout, **k: runs.append(cmd) or (
        ASLEEP,
        b"",
        0,
    )
    assert ble.ping(VIN) is True and v.value("state") == "asleep"
    assert len(runs) == 1 and runs[0][-1] == "body-controller-state"
    assert "-key-file" not in runs[0]


def test_ping_with_scan_false_returns_none_and_observes_nothing_when_unanswered():
    ble, reg, master = scanner()
    v = reg.getOrCreate(VIN, "t")
    ble._tool = lambda vin, *args: (b"", b"timeout", 1)
    ble.scanForVehicleInRange = lambda vin: pytest.fail("no scan asked")
    assert ble.ping(VIN, scan=False) is None and v.latest("bleReach") is None
