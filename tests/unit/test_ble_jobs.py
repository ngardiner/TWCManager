# tests/unit/test_ble_jobs.py: BLE work that is no command (a range scan, a
# VIN-less plug's attribution) runs on the reconciler's BLE worker, in turn
# with the BLE commands, never on the background-task thread.
import threading
import time
from unittest.mock import Mock

from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT
from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
from tests.unit.reconcile_helpers import ScriptedProvider, entries, runPending
from tests.unit.test_limit_reconciler import VIN, setup
from tests.unit.test_teslable_pairing_status import VIN as BVIN, scanner


class SlowBle(ScriptedProvider):
    # A BLE command that takes a while, stamped with its thread and times
    def __init__(self):
        super().__init__("TeslaBLE", 2, True, [O.Outcome(O.DONE)])
        self.spans = []

    def setChargeLimit(self, vin, limit):
        start = time.monotonic()
        time.sleep(0.3)
        self.spans.append((threading.current_thread().name, start, time.monotonic()))
        return super().setChargeLimit(vin, limit)


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


def test_a_scan_runs_on_the_ble_worker_after_the_command_ahead_of_it():
    p = SlowBle()
    m, _, v, c = setup(p)
    v.observe("chargeLimit", 90, "TeslaBLE")
    m.reconciler = r = VehicleReconciler(m, clock=c)
    ble, _, master = scanner()
    master.reconciler = r
    scans = []

    def scan(vin):
        now = time.monotonic()
        scans.append((threading.current_thread().name, now, time.monotonic()))

    ble.scanForVehicleInRange = scan
    r.ble.start()
    try:
        m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
        assert r.pass_() == 1
        ble.requestRangeScan(BVIN)
        assert until(lambda: scans)
    finally:
        r.ble.close()
        r.ble.waitDrained(time.monotonic() + 5)
    (cmdThread, _, cmdEnd), (scanThread, scanStart, _) = p.spans[0], scans[0]
    assert cmdThread == scanThread == "Vehicle-ble"
    assert scanStart >= cmdEnd
    master.queue_background_task.assert_not_called()
    assert ble.rangeStatus(BVIN)["pending"] is False


def test_a_burst_of_requests_for_one_car_runs_one_scan():
    ble, _, master = scanner()
    ble.scanForVehicleInRange = seen = Mock()
    for _ in range(5):
        ble.requestRangeScan(BVIN)
    assert len(entries(master.reconciler.ble)) == 1
    runPending(master.reconciler.ble)
    assert seen.call_count == 1
    master.queue_background_task.assert_not_called()


def test_a_failing_job_is_logged_and_the_worker_runs_the_next(caplog):
    ble, _, master = scanner()
    r = master.reconciler
    ran = []

    def boom():
        raise RuntimeError("radio gone")

    r.submitJob("A", "scan", boom)
    r.submitJob("B", "scan", lambda: ran.append("B"))
    r.ble.start()
    try:
        assert until(lambda: ran)
    finally:
        r.ble.close()
        r.ble.waitDrained(time.monotonic() + 5)
    assert ran == ["B"]
    assert "RuntimeError" in caplog.text


def test_a_vinless_plug_queues_its_attribution_on_the_ble_worker():
    m, _, v, c = setup(here=False)
    m.reconciler = r = VehicleReconciler(m, clock=c)
    m.queue_background_task = Mock()
    s = m.getSlaveTWCs()[0]
    m._plugSession(s, True)
    m._plugSession(s, True)
    jobs = entries(r.ble)
    assert [(j.key, j.kind) for j in jobs] == [("8244", "attribute")]
    m.queue_background_task.assert_not_called()


def test_a_scan_the_stopped_worker_refuses_is_not_shown_pending(caplog):
    ble, _, master = scanner()
    master.reconciler.ble.close()
    ble.requestRangeScan(BVIN)
    assert ble.rangeStatus(BVIN)["pending"] is False
    assert caplog.text.count("not queued") == 1
