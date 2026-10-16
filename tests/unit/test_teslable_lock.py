"""
Tests that TeslaBLE never runs two tesla-control invocations at once.

Every BLE operation writes key material to one shared path, runs
tesla-control against it, and unlinks it afterwards. Overlapping operations
delete each other's key file, so they must be serialized.
"""

import base64
import os
import threading
import time
from unittest.mock import Mock, patch

import pytest

from TWCManager.Vehicle.TeslaBLE import TeslaBLE

VIN = "5YJ3E1EA1KF000001"


@pytest.fixture
def ble(tmp_path):
    master = Mock()
    master.config = {"config": {"teslaControlPath": "/fake/tesla-control"}}
    # A real lock, so the pairing-status writes run here too.
    master.settingsLock = threading.RLock()
    master.settings = {
        "Vehicles": {
            VIN: {
                "privKey": base64.b64encode(b"private").decode(),
                "pubKeyPEM": base64.b64encode(b"public").decode(),
            }
        }
    }
    with patch.object(TeslaBLE, "pipeName", str(tmp_path / "ble_data")), patch(
        "TWCManager.Vehicle.TeslaBLE.shutil.which", return_value=None
    ), patch(
        "TWCManager.Vehicle.TeslaBLE.os.path.isfile", return_value=True
    ), patch(
        "TWCManager.Vehicle.TeslaBLE.os.access", return_value=True
    ):
        instance = TeslaBLE(master)
        instance.isDockerCached = False
        yield instance


class OverlapRecorder:
    """Stand-in for _run_command_with_timeout that records concurrency and
    whether the key file still exists while the 'command' runs."""

    def __init__(self, pipe_name, delay=0.05):
        self.pipe_name = pipe_name
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.missing_key = 0
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, command_string, timeout, use_process_group=True):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if not os.path.exists(self.pipe_name):
            self.missing_key += 1
        time.sleep(self.delay)
        if not os.path.exists(self.pipe_name):
            self.missing_key += 1
        with self._lock:
            self.active -= 1
        if "add-key-request" in command_string:
            return b"", b"Sent add-key request. Confirm by tapping NFC card.", 0
        return b"{}", b"ok", 0


def run_concurrently(*calls):
    threads = [threading.Thread(target=c) for c in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads), "concurrent call deadlocked"


def test_ble_lock_exists(ble):
    assert hasattr(ble._bleLock, "acquire")
    assert not ble._bleLock.locked()


def test_send_commands_do_not_overlap(ble):
    recorder = OverlapRecorder(ble.pipeName)
    ble._run_command_with_timeout = recorder

    run_concurrently(
        lambda: ble._sendCommand_internal(VIN, "wake"),
        lambda: ble._sendCommand_internal(VIN, "charging-start"),
    )

    assert recorder.calls == 2
    assert recorder.max_active == 1
    assert recorder.missing_key == 0
    # The status write ran under _bleLock without deadlocking.
    assert ble.bleStatus(VIN)["status"] == "paired"


def test_state_read_does_not_overlap_command(ble):
    recorder = OverlapRecorder(ble.pipeName)
    ble._run_command_with_timeout = recorder

    run_concurrently(
        lambda: ble._fetch_state_json(VIN, "charge"),
        lambda: ble._sendCommand_internal(VIN, "charging-set-amps", 16),
    )

    assert recorder.calls == 2
    assert recorder.max_active == 1
    assert recorder.missing_key == 0
    assert ble.bleStatus(VIN)["status"] == "paired"


def test_pairing_does_not_overlap_command(ble):
    recorder = OverlapRecorder(ble.pipeName)
    ble._run_command_with_timeout = recorder

    run_concurrently(
        lambda: ble.peerWithVehicle(VIN),
        lambda: ble._sendCommand_internal(VIN, "wake"),
    )

    assert recorder.calls == 2
    assert recorder.max_active == 1
    assert recorder.missing_key == 0
    # Either order ends proven: pairing then proof, or proof kept by pairing.
    assert ble.bleStatus(VIN)["status"] == "paired"
    assert ble.blePairingAttempt(VIN)["ok"] is True


def test_quick_retry_sleeps_outside_lock(ble):
    results = iter([(None, None, None), (b"", b"ok", 0)])
    ble._run_command_with_timeout = lambda *a, **k: next(results)
    lock_held_during_sleep = []

    def fake_sleep(seconds):
        lock_held_during_sleep.append(ble._bleLock.locked())

    with patch("TWCManager.Vehicle.TeslaBLE.time.sleep", side_effect=fake_sleep):
        assert ble.sendCommand(VIN, "wake") == "ok"

    assert lock_held_during_sleep == [False]
