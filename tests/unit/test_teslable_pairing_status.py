"""
The vehicle page shows what happened to the last BLE pairing request, and
marks the car Paired once it accepts a command signed with our key.
"""

import base64
from unittest.mock import Mock, patch

import jinja2
import pytest

from TWCManager.Vehicle.TeslaBLE import TeslaBLE

VIN = "5YJ3E1EA1KF000001"
SENT = b"Sent add-key request to %s. Confirm by tapping NFC card on center console.\n"


@pytest.fixture
def ble(tmp_path):
    master = Mock()
    master.config = {"config": {"teslaControlPath": "/fake/tesla-control"}}
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
        instance = TeslaBLE(master)
        instance.isDockerCached = False
        yield instance


def test_sent_request_is_recorded(ble):
    ble._run_command_with_timeout = lambda *a, **k: (b"", SENT % VIN.encode(), 0)
    assert ble.peerWithVehicle(VIN)
    status = ble.pairingStatus[VIN]
    assert status["sent"] is True
    assert "tapping NFC card" in status["detail"]
    # Sending the request does not prove the car accepted the key
    assert "BLEPeeringStatus" not in ble.master.settings["Vehicles"][VIN]


def test_timeout_is_recorded_as_failed(ble):
    ble._run_command_with_timeout = lambda *a, **k: (None, None, -9)
    assert not ble.peerWithVehicle(VIN)
    assert ble.pairingStatus[VIN]["sent"] is False
    assert ble.pairingStatus[VIN]["detail"] == "no response from the car"


def test_signed_command_marks_paired(ble):
    ble.pairingStatus[VIN] = {"sent": True, "time": "12:00:00", "detail": ""}
    ble._run_command_with_timeout = lambda *a, **k: (b"", b"", 0)
    assert ble._sendCommand_internal(VIN, "wake") == "ok"
    assert ble.master.settings["Vehicles"][VIN]["BLEPeeringStatus"] == "peered"
    assert VIN not in ble.pairingStatus
    ble.master.queue_background_task.assert_called_with({"cmd": "saveSettings"})


def test_state_read_marks_paired(ble):
    ble._run_command_with_timeout = lambda *a, **k: (b'{"chargeState": {}}', b"", 0)
    assert ble._fetch_state_json(VIN, "charge") == {"chargeState": {}}
    assert ble.master.settings["Vehicles"][VIN]["BLEPeeringStatus"] == "peered"


def test_failed_command_does_not_mark_paired(ble):
    ble._run_command_with_timeout = lambda *a, **k: (b"", b"key not paired", 1)
    assert ble._sendCommand_internal(VIN, "wake") is None
    assert "BLEPeeringStatus" not in ble.master.settings["Vehicles"][VIN]


def render(ble, vehicle):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader("lib/TWCManager/Control/themes/Default"),
        autoescape=True,
    )
    env.globals["include"] = None
    source = env.loader.get_source(env, "vehicleDetail.html.j2")[0]
    start = source.index("{% if 'TeslaBLE' in master.modules %}")
    end = source.index("{% endif %}", source.index("</table>", start)) + len(
        "{% endif %}"
    )
    master = Mock()
    master.modules = {"TeslaBLE": {}}
    master.getModuleByName = lambda name: ble
    master.settings = {"Vehicles": {VIN: vehicle}}
    return env.from_string(source[start:end]).render(master=master, vin=VIN)


@pytest.mark.parametrize(
    "pairing,vehicle,expected",
    [
        (None, {}, "Not paired"),
        (None, {"BLEPeeringStatus": "peered"}, "Paired"),
        ({"sent": True, "time": "12:00:00", "detail": ""}, {}, "Tap your key card"),
        (
            {"sent": False, "time": "12:00:00", "detail": "no response from the car"},
            {},
            "failed at 12:00:00",
        ),
    ],
)
def test_page_shows_status(ble, pairing, vehicle, expected):
    if pairing:
        ble.pairingStatus[VIN] = pairing
    page = render(ble, vehicle)
    assert expected in page
    assert "Not Implemented" not in page


def test_debug_output_alone_is_not_a_sent_request(ble):
    # -debug output mentions "OAuth token", which used to match "ok"
    output = b"[debug] Set OAuth token name to ''\n[debug] Connecting over BLE...\n"
    ble._run_command_with_timeout = lambda *a, **k: (b"", output, 1)
    assert not ble.peerWithVehicle(VIN)
    assert ble.pairingStatus[VIN]["sent"] is False
