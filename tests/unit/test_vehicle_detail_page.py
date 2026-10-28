import time
from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.Control.HTTPControl import pendingCommands, vehicleLocalMgmt
from TWCManager.Vehicle.Reconcile.Desired import (
    AMPS,
    CHARGE_LIMIT,
    CHARGING,
    RELEASE,
    Attempt,
)
from TWCManager.Vehicle.Reconcile.Outcome import HELD, UNREACHABLE, Outcome
from tests.unit.test_vehicles_page import env, master, spending, VIN


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
    assert 'value="scan"' in html and 'value="pair"' in html and "Tier" not in html
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


def test_the_reason_no_scan_is_possible_replaces_the_button():
    m = master()
    m.modules = {"TeslaBLE": object()}
    reason = "Bluetooth adapter is powered off or blocked (rfkill)"
    html = render(m, status(canScan=False, scanBlocked=reason))
    assert "No range scan: " + reason in html and 'value="scan"' not in html
    html = render(m, status(scanBlocked=None))
    assert "No range scan" not in html and "Scan for vehicle" in html


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


def test_scan_pending_page_refreshes_itself_only_while_pending():
    m = master()
    m.modules = {"TeslaBLE": object()}
    pending = render(m, status(scanPending=True))
    assert '<meta http-equiv="refresh" content="3">' in pending
    assert 'http-equiv="refresh"' not in render(m, status())
    assert 'http-equiv="refresh"' not in render(m, None)


def pending(m):
    v = m.vehicleRegistry.get(VIN)
    return {r["aspect"]: r["why"] for r in pendingCommands(m, v)}


def text(html):
    return " ".join(html.split()).replace("&rarr;", "\u2192").replace("&#39;", "'")


def attempt(aspect, value, provider="TeslaBLE"):
    return Attempt(
        VIN, aspect, "x", value, provider, False, None, False, time.time() - 12
    )


def test_pending_section_says_nothing_pending_when_the_store_is_empty():
    m = master()
    assert pending(m) == {}
    html = render(m, None)
    assert "Pending commands" in html and "Nothing pending" in html


def test_pending_in_flight_names_the_source_and_age():
    m = master()
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    m.desired.record(VIN, CHARGE_LIMIT).inFlight = attempt(CHARGE_LIMIT, 50)
    assert pending(m) == {CHARGE_LIMIT: "sending via BLE, started 12 s ago"}
    html = text(render(m, None))
    assert "Charge limit \u2192 50%" in html
    assert "sending via BLE, started 12 s ago" in html
    assert "Nothing pending" not in html and "TeslaBLE" not in html


def test_pending_rows_word_each_aspect_and_value():
    m = master()
    m.desired.set(VIN, CHARGE_LIMIT, RELEASE, "policy")
    m.desired.set(VIN, CHARGING, True, "distributor")
    m.desired.set(VIN, AMPS, 16, "distributor")
    html = text(render(m, None))
    for want in (
        "Charge limit \u2192 car's own",
        "Charging \u2192 start",
        "Charge rate \u2192 16 A",
    ):
        assert want in html
    assert pending(m) == {a: "not tried yet" for a in (CHARGE_LIMIT, CHARGING, AMPS)}


def test_pending_held_by_a_real_hold_outcome_says_why():
    m = master()
    m.desired.set(VIN, CHARGING, True, "distributor")
    a = attempt(CHARGING, True, "Gen2TWC")
    a = a._replace(since=time.time())
    m.desired.record(VIN, CHARGING).inFlight = a
    m.desired.noteOutcome(
        VIN,
        CHARGING,
        a,
        Outcome("satisfied", HELD + "driving"),
        time.time(),
        paid=False,
    )
    rec = m.desired.record(VIN, CHARGING)
    assert rec.heldAt is not None
    assert pending(m)[CHARGING].startswith("held: driving via TWC, ")


def test_pending_held_with_no_outcome_yet_does_not_crash():
    m = master()
    m.desired.set(VIN, CHARGING, True, "distributor")
    m.desired.record(VIN, CHARGING).heldAt = time.time()
    assert pending(m) == {CHARGING: "not tried yet"}
    assert "not tried yet" in render(m, None)


def test_pending_says_what_the_reconciler_said_over_the_last_outcome():
    m = master()
    m.desired.set(VIN, CHARGING, False, "distributor")
    rec = m.desired.record(VIN, CHARGING)
    rec.lastOutcome, rec.lastOutcomeAt = Outcome(UNREACHABLE), time.time() - 5
    m.reconciler._said[(VIN, CHARGING)] = "hands off: battery below minChargeLevel"
    assert pending(m) == {CHARGING: "hands off: battery below minChargeLevel"}
    m.reconciler._said[(VIN, CHARGING)] = "no provider can deliver"
    assert pending(m) == {CHARGING: "no provider can deliver"}


def test_pending_last_outcome_with_source_and_age():
    m = master()
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy")
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    rec.lastOutcome, rec.lastOutcomeAt = Outcome(UNREACHABLE), time.time() - 120
    rec.lastProvider = "TeslaBLE"
    assert pending(m) == {CHARGE_LIMIT: "unreachable via BLE, 2 min ago"}
    rec.lastOutcome = Outcome("unreachable", "backoff")
    assert pending(m) == {CHARGE_LIMIT: "backoff via BLE, 2 min ago"}


def test_pending_hides_a_desire_the_car_already_has_or_that_settled():
    m = master()
    v = m.vehicleRegistry.get(VIN)
    v.observe("chargeLimit", 70, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy")
    m.desired.set(VIN, AMPS, 16, "distributor")
    m.desired.record(VIN, AMPS).settledAt = time.time()
    assert pending(m) == {}


def test_a_render_creates_no_desired_state():
    m = master()
    render(m, None)
    assert m.desired.keys() == []


def test_pending_shows_a_settled_desire_the_car_drifted_from():
    # Settled, then the car reads another rate: the reconciler sends again,
    # so the page lists it until a reading shows the car has it
    from tests.unit.test_limit_reconciler import ble, setup
    from TWCManager.Vehicle.Reconcile import Outcome as O
    from TWCManager.Vehicle.Reconcile.Reconciler import RETRY

    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE)))
    v.observe("chargeCurrentRequest", 32, "TeslaBLE")
    m.desired.set(VIN, AMPS, 16, "distributor")
    assert r.pass_() == 1 and m.desired.record(VIN, AMPS).settledAt is not None
    assert pendingCommands(m, v) == []
    c.t += 10
    v.observe("chargeCurrentRequest", 32, "TeslaMateVehicle")
    assert [row["aspect"] for row in pendingCommands(m, v)] == [AMPS]
    c.t += RETRY[AMPS]
    assert r.pass_() == 1
    c.t += 10
    v.observe("chargeCurrentRequest", 16, "TeslaMateVehicle")
    assert pendingCommands(m, v) == [] and r.pass_() == 0


def test_pending_shows_a_limit_held_asleep_until_the_car_reports_it():
    # The box after a restart: the hold ran before any limit was known, then
    # TeslaMate reported the car already at the desired limit
    from tests.unit.test_limit_reconciler import ble, setup
    from TWCManager.Vehicle.Reconcile import Outcome as O

    m, r, v, c = setup(ble())
    v.observe("state", "asleep", "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    assert r.pass_() == 1 and m.desired.record(VIN, CHARGE_LIMIT).heldAt is not None
    assert pending(m)[CHARGE_LIMIT].startswith("held: asleep via BLE")
    c.t += 30
    v.observe("chargeLimit", 50, "TeslaMateVehicle")
    assert pending(m) == {} and r.pass_() == 0


def test_vehicle_page_lists_types_and_fields():
    m = master()
    s = spending(m, telemetry=True)
    s.request(VIN, "wake_up", 0.02)
    s.request(VIN, "wake_up", 0.02)
    for _ in range(3):
        s.signal(VIN, "Location")
    html = render(m, status())
    assert "Fleet API this month: $0.04 in 2 requests" in html
    assert "<td>wake_up</td>" in html and "<td>Location</td>" in html
    assert "Fleet Telemetry this month: approx. 3 signals" in html
