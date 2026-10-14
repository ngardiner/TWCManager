from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.Control.HTTPControl import vehicleLocalMgmt
from tests.unit.test_vehicles_page import env, master, VIN


def status(**kw):
    st = {
        "status": "unpaired",
        "label": "In range (not paired)",
        "inRange": True,
        "scannedAt": 1.0,
        "lastScanAge": 42.0,
        "scanPending": False,
        "canScan": True,
    }
    st.update(kw)
    return st


def render(m, st, vin=VIN):
    e = env(m)
    e.globals["bleStatus"] = lambda v: st
    return e.get_template("vehicleDetail.html.j2").render(
        master=m, url=SimpleNamespace(path=f"/vehicleDetail/{vin}")
    )


def test_detail_renders_without_history_and_shows_ble_status():  # Review Focus 5
    m = master()
    m.settings["Vehicles"] = {}
    m.modules = {"TeslaBLE": object()}
    html = render(m, status())
    assert "In range (not paired)" in html and "Not Implemented" not in html
    assert 'value="scan"' in html and 'value="pair"' in html and "managed" in html
    assert "42 s ago" in html and "Pair with Vehicle" in html
    assert "Re-pair" not in html


def test_detail_shows_charge_sessions_from_history():
    m = master()
    html = render(m, None)
    assert "Charge Sessions" in html and ">3<" in html.replace(" ", "")


def test_detail_without_ble_module_has_no_ble_panel():
    m = master()
    html = render(m, None)
    assert "BLE Control" not in html and 'value="scan"' not in html


def test_scan_pending_and_no_bluetoothctl():
    m = master()
    m.modules = {"TeslaBLE": object()}
    html = render(m, status(scanPending=True))
    assert "Scanning" in html and "disabled" in html
    html = render(
        m, status(label="Range unknown", inRange=None, canScan=False, lastScanAge=None)
    )
    assert "Range unknown (scanning unavailable)" in html
    assert 'value="scan"' not in html


def test_repair_button_and_since_and_attempt_message_escaped():
    m = master()
    m.modules = {"TeslaBLE": object()}
    st = status(
        status="refused",
        label="Key refused (re-pair)",
        sinceAge=120.0,
        attempt={"ok": False, "at": 1.0, "message": "<b>timed out</b>"},
    )
    html = render(m, st)
    assert "Re-pair with Vehicle" in html and "2 min ago" in html
    assert "<b>timed out</b>" not in html and "&lt;b&gt;timed out" in html


def test_registry_row_absent_for_unknown_vehicle():
    m = master()
    html = render(m, None, vin="5YJ3E1EA1KF000009")
    assert "Tier" not in html


def test_local_mgmt_pair_and_scan():
    m = master()
    ble = Mock()
    m.getModuleByName = lambda n: ble if n == "TeslaBLE" else None
    assert vehicleLocalMgmt(m, "scan", VIN) == "/vehicleDetail/" + VIN
    ble.requestRangeScan.assert_called_once_with(VIN)
    ble.peerWithVehicle.assert_not_called()
    assert vehicleLocalMgmt(m, "pair", VIN) == "/vehicleDetail/" + VIN
    ble.peerWithVehicle.assert_called_once_with(VIN)
    # A bad VIN never reaches the module or the Location header.
    assert vehicleLocalMgmt(m, "pair", "x\r\nSet-Cookie: a=b") == "/vehicles"
    assert ble.peerWithVehicle.call_count == 1
    m.getModuleByName = lambda n: None
    assert vehicleLocalMgmt(m, "scan", VIN) == "/vehicleDetail/" + VIN
