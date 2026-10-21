"""
Tests how TeslaBLE starts tesla-control, in and out of Docker.

In Docker, tesla-control runs in the host's network namespace through
nsenter; Popen gets no shell, so nsenter and its option must be separate
argv elements. A command that times out is killed with its whole process
group, so every command needs a group of its own, never TWCManager's.
"""

import base64
import os
from unittest.mock import Mock, patch

import pytest

from TWCManager.Vehicle.TeslaBLE import TeslaBLE

VIN = "5YJ3E1EA1KF000001"
BINARY = "/fake/tesla-control"

CALL_PATHS = {
    "pairing": lambda ble: ble.peerWithVehicle(VIN),
    "command": lambda ble: ble._sendCommand_internal(VIN, "charging-set-amps", 16),
    "state": lambda ble: ble._fetch_state_json(VIN, "charge"),
}


@pytest.fixture
def ble(tmp_path):
    master = Mock()
    master.config = {"config": {"teslaControlPath": BINARY}}
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
    ), patch("TWCManager.Vehicle.TeslaBLE.os.path.isfile", return_value=True), patch(
        "TWCManager.Vehicle.TeslaBLE.os.access", return_value=True
    ):
        yield TeslaBLE(master)


@pytest.fixture
def popen():
    proc = Mock(returncode=0, pid=12345)
    proc.communicate.return_value = (b"{}", b"ok")
    with patch(
        "TWCManager.Vehicle.TeslaBLE.subprocess.Popen", return_value=proc
    ) as fake:
        yield fake


@pytest.mark.parametrize("path", sorted(CALL_PATHS))
def test_docker_runs_tesla_control_through_nsenter(ble, popen, path):
    ble.isDockerCached = True
    CALL_PATHS[path](ble)
    argv = popen.call_args[0][0]
    assert argv[:3] == ["nsenter", "--net=/rootns/net", BINARY]


@pytest.mark.parametrize("path", sorted(CALL_PATHS))
def test_native_runs_tesla_control_directly(ble, popen, path):
    ble.isDockerCached = False
    CALL_PATHS[path](ble)
    assert popen.call_args[0][0][0] == BINARY


@pytest.mark.parametrize("docker", [False, True])
def test_timeout_kills_only_the_command(ble, docker):
    ble.isDockerCached = docker
    real_killpg = os.killpg
    killed = []

    def guarded_killpg(pgid, sig):
        killed.append(pgid)
        if pgid != os.getpgrp():  # never signal the test runner's own group
            real_killpg(pgid, sig)

    with patch("TWCManager.Vehicle.TeslaBLE.os.killpg", side_effect=guarded_killpg):
        result = ble._run_command_with_timeout(["sleep", "3"], timeout=0.2)

    assert result == (None, None, None)
    assert killed
    assert os.getpgrp() not in killed
