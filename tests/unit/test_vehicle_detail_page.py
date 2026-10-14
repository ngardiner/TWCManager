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


TYPO = "5YJ3E1EA1KF000004"


def added(m, vin=TYPO):
    from TWCManager.Control.HTTPControl import addVehicleByVIN

    m.queue_background_task = Mock()
    assert addVehicleByVIN(m, vin) == "/vehicleDetail/" + vin
    m.settings["Vehicles"][vin]["privKey"] = "k"  # scanForVehicles' key
    return m.vehicleRegistry.get(vin)


def test_forget_an_added_vin_removes_vehicle_record_and_key():  # I1
    from TWCManager.Control.HTTPControl import canForgetVehicle, forgetVehicle

    m = master()
    added(m)
    assert canForgetVehicle(m, TYPO)
    assert forgetVehicle(m, TYPO) == "/vehicles"
    assert m.vehicleRegistry.get(TYPO) is None and TYPO not in m.settings["Vehicles"]
    m.queue_background_task.assert_called_with({"cmd": "saveSettings"})
    html = render(m, None, vin=TYPO)
    assert "Forget" not in html


def test_forget_button_only_for_an_unattached_user_asserted_vehicle():  # I1
    m = master()
    v = added(m)
    from TWCManager.Control.HTTPControl import canForgetVehicle

    m.modules = {}
    assert 'action="/vehicle/forget"' in render(m, None, vin=TYPO)
    assert 'action="/vehicle/forget"' not in render(m, None, vin=VIN)
    v.attach("TeslaAPI", object())  # the account lists it: not ours to forget
    assert not canForgetVehicle(m, TYPO)
    assert 'action="/vehicle/forget"' not in render(m, None, vin=TYPO)


def test_forget_refuses_listed_paired_and_unknown_vehicles():  # I1
    from TWCManager.Control.HTTPControl import canForgetVehicle, forgetVehicle

    m = master()
    m.queue_background_task = Mock()
    # Not user-asserted (seen by a TWC): Phase 6.
    assert not canForgetVehicle(m, VIN)
    assert forgetVehicle(m, VIN) == "/vehicleDetail/" + VIN
    assert m.vehicleRegistry.get(VIN) is not None and VIN in m.settings["Vehicles"]
    # Attached by a provider.
    v = added(m)
    v.attach("HomeAssistant", object())
    assert forgetVehicle(m, TYPO) == "/vehicleDetail/" + TYPO
    assert m.vehicleRegistry.get(TYPO) is v
    # A proven BLE key: a real car, not a typo.
    m2 = master()
    added(m2)
    m2.settings["Vehicles"][TYPO]["ble"] = {"status": "paired", "since": 1.0}
    assert not canForgetVehicle(m2, TYPO)
    # Bad input never reaches the registry or the Location header.
    assert forgetVehicle(m, "x\r\nSet-Cookie: a=b") == "/vehicles"
    assert not canForgetVehicle(m, "5YJ3E1EA1KF000009")


def test_forget_keeps_a_record_that_has_history():  # I1
    from TWCManager.Control.HTTPControl import forgetVehicle

    m = master()
    m.queue_background_task = Mock()
    # Known to settings before the add (charge history): only the
    # registry entry goes.
    m.settings["Vehicles"][TYPO] = {"chargeSessions": 2, "totalkWh": 5}
    added(m)
    assert forgetVehicle(m, TYPO) == "/vehicles"
    assert m.vehicleRegistry.get(TYPO) is None
    assert m.settings["Vehicles"][TYPO]["chargeSessions"] == 2
