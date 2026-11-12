from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.Control.HTTPControl import vehicleLocalMgmt
from TWCManager.Control.Views import pageFingerprint
from tests.unit.test_vehicles_page import env, master, spending, VIN


def render(m, vin=VIN):
    return (
        env(m)
        .get_template("vehicleDetail.html.j2")
        .render(master=m, url=SimpleNamespace(path=f"/vehicleDetail/{vin}"))
    )


def withBLE(m):
    m.modules = {"TeslaBLE": object()}
    return m


def test_detail_renders_without_history_and_shows_ble_panel():  # Review Focus 5
    m = withBLE(master())
    m.settings["Vehicles"] = {}
    html = render(m)
    assert "BLE Control" in html and "Not Implemented" not in html
    assert 'value="scan"' in html and 'value="pair"' in html and "Tier" not in html
    assert 'data-field="vehicle.ble" data-format="bleLabel"' in html
    assert 'data-field="vehicle.ble" data-format="bleSince"' in html
    assert 'data-field="vehicle.ble" data-format="bleAttempt"' in html
    assert 'data-field="vehicle.ble" data-format="bleLastScan"' in html


def test_detail_shows_charge_sessions():
    m = master()
    assert 'data-field="vehicle.chargeSessions"' in render(m)
    # a VIN the registry doesn't know shows its history as is
    other = "5YJ3E1EA1KF000009"
    m.settings["Vehicles"][other] = {"chargeSessions": 7}
    html = render(m, vin=other)
    assert "Charge Sessions" in html and ">7<" in html.replace(" ", "")
    assert "vehicle.chargeSessions" not in html


def test_detail_without_ble_module_has_no_ble_panel():
    m = master()
    html = render(m)
    assert "BLE Control" not in html and 'value="scan"' not in html


def test_scan_buttons_follow_scan_pending():
    html = render(withBLE(master()))
    form = html[html.index('data-show="vehicle.ble.canScan"') :].split("</form>")[0]
    assert 'value="scan"' in form
    pending, idle = form.split('data-show="vehicle.ble.scanPending"')[1:3]
    assert 'value="Scanning…" disabled' in pending
    assert idle.startswith(' data-show-if="not"') and 'value="Scan for vehicle"' in idle


def test_scan_pending_no_longer_meta_refreshes():
    assert 'http-equiv="refresh"' not in render(withBLE(master()))


def test_the_reason_no_scan_is_possible_is_a_hook():
    html = render(withBLE(master()))
    assert (
        'data-field="vehicle.ble" data-format="bleScanBlocked"'
        ' data-show="vehicle.ble" data-show-if="bleScanBlocked"' in html
    )


def test_pair_and_repair_buttons_follow_the_key():
    html = render(withBLE(master()))
    repair = html.split('data-show-if="bleRepair"')[1].split("</span>")[0]
    pair = html.split('data-show-if="bleNotRepair"')[1].split("</span>")[0]
    assert "Re-pair with Vehicle" in repair and '"Pair with Vehicle"' in pair


def test_page_carries_fingerprint_and_starts_live():
    m = master()
    html = render(m)
    assert "Live.start" in html and pageFingerprint(m, "vehicle", VIN) in html
    assert "/static/js/format.js" in html and "/static/js/live.js" in html
    assert f'"/api/getVehicle/{VIN}"' in html and "interval: 5000" in html
    # names arrive by the API and are written as text
    assert "TestCar" not in html and 'data-field="vehicle.name"' in html


def test_unknown_vin_page_does_not_poll():  # Review Focus 2
    m = withBLE(master())
    html = render(m, vin="5YJ3E1EA1KF000009")
    assert "Live.start" not in html and "Vehicle VIN" in html
    assert "Tier" not in html and "BLE Control" not in html


def test_pending_section_is_a_list_hook():
    html = render(master())
    assert "Pending commands" in html
    assert '<tbody data-list="vehicle.pending" data-row="pendingRow">' in html
    assert 'data-show="vehicle.pending" data-show-if="isEmpty"' in html
    assert "Nothing pending" in html


def test_a_render_creates_no_desired_state():
    m = master()
    render(m)
    assert m.desired.keys() == []


def test_vehicle_page_spend_section_is_hooks():
    m = master()
    spending(m, telemetry=True)
    html = render(m)
    assert 'data-show="vehicle.spend"' in html and "Fleet spend" in html
    assert 'data-list="vehicle.spend.types" data-row="spendTypeRow"' in html
    assert 'data-list="vehicle.spend.fields" data-row="fieldRow"' in html
    assert 'data-field="vehicle" data-format="spendHeading"' in html
    assert 'data-field="vehicle" data-format="spendTelemetry"' in html
    assert "$" not in html.split("<body>")[1]


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
    html = render(m, vin=TYPO)
    assert "Forget" not in html


def test_forget_button_only_for_an_unattached_user_asserted_vehicle():  # I1
    m = master()
    v = added(m)
    from TWCManager.Control.HTTPControl import canForgetVehicle

    m.modules = {}
    assert 'action="/vehicle/forget"' in render(m, vin=TYPO)
    assert 'action="/vehicle/forget"' not in render(m, vin=VIN)
    v.attach("TeslaAPI", object())  # the account lists it: not ours to forget
    assert not canForgetVehicle(m, TYPO)
    assert 'action="/vehicle/forget"' not in render(m, vin=TYPO)


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


def test_forget_refuses_a_car_listed_after_the_check(monkeypatch):  # F7
    from TWCManager.Control import HTTPControl
    from tests.unit.teslatokens import new_api

    m = master()
    m.queue_background_task = Mock()
    v = added(m)
    m.config = {"config": {}}
    api = new_api(master=m)
    # The check passed; the account lists the car before the forget
    monkeypatch.setattr(
        HTTPControl,
        "canForgetVehicle",
        lambda master, vin: api.addVehicle({"id": 1, "vin": TYPO, "display_name": "C"})
        or True,
    )
    assert HTTPControl.forgetVehicle(m, TYPO) == "/vehicleDetail/" + TYPO
    assert m.vehicleRegistry.get(TYPO) is v and TYPO in m.settings["Vehicles"]
    assert v.attachment("TeslaAPI") is not None


def test_live_row_tables_keep_a_gap_between_cells():
    # live.js draws one cell per value, without the old &nbsp; spacer column
    html = render(master())
    for hook in ("vehicle.pending", "vehicle.spend.types", "vehicle.spend.fields"):
        before = html[: html.index('data-list="%s"' % hook)]
        assert before[before.rindex("<table") :].startswith('<table class="live-rows"')
    assert "table.live-rows td + td" in html
