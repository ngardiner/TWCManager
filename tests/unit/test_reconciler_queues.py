import logging
import threading
import time
import pytest
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile import Run
import inspect
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING, Attempt, DesiredStore
from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
from TWCManager.Vehicle.Reconcile.Run import entryKey, runAttempt
from TWCManager.VehicleCommandQueue import VehicleCommandQueue
from tests.unit.reconcile_helpers import ScriptedProvider, entries, inline, make_order
VIN = "5YJ3E1EA1KF000001"
VIN2 = "5YJ3E1EA1KF000002"

def attempt(cap=C.SET_CHARGE_LIMIT, value=80, provider="TeslaBLE", aspect=CHARGE_LIMIT, wake=False, maxAge=None):
    return Attempt(VIN, aspect, cap, value, provider, wake, maxAge, False, time.time())

def one_shot(cap, provider="TeslaBLE"):
    return Attempt(VIN, None, cap, None, provider, False, None, False, time.time())

def reconciler(*providers):
    Run._badReturns.clear()      # warn-once state is per process
    m, o = make_order({}, *providers); m.providerOrder = o
    # A known current limit (any channel): no limit is sent without one. An
    # hour old, so a write with a maxAge still reads first
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    v.observe("chargeLimit", 90, "TeslaMateVehicle", at=time.time() - 3600)
    # Plugged in at home: every message applies
    v.observe("pluggedIn", True, "Gen3TWC")
    v.mark("plugIn", time.time())
    m.desired = DesiredStore(); m.heldLimits.get.return_value = None
    m.reconciler = r = VehicleReconciler(m); return m, r

def setup(*providers, inline_=True):
    # Reports are captured instead of noted: these tests are about the runner
    m, r = reconciler(*providers); reports = []
    def report(a, out):
        # As VehicleReconciler.report: a queue delivers a BaseException raw
        out = O.Outcome(O.ERROR, type(out).__name__) if isinstance(out, BaseException) else out
        reports.append((a.provider, out.kind, out.detail))
    r.report = report
    if inline_: inline(r)
    return m, r, reports

def submit(r, a):
    # The attempt's desire, so the runner's stale-desire guard lets it run
    if a.aspect is not None: r.desired.set(a.key, a.aspect, a.value, "test")
    return r.submit(a)

def pending(r): return len(entries(r.ble)) + len(entries(r.cloud))

def kinds(reports): return [r[1] for r in reports]

def test_worker_follows_the_provider_s_worker_attribute():
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    x = ScriptedProvider("Custom", 7, False, [O.Outcome(O.DONE)], worker="ble"); m, r, reports = setup(b, f, x, inline_=False)
    assert (b.worker, f.worker, x.worker, Provider.worker, ScriptedProvider("Gen3TWC", 1, True, []).worker) == ("ble", "cloud", "ble", "cloud", "local")
    submit(r, attempt(provider="TeslaBLE")); submit(r, attempt(provider="Custom")._replace(key=VIN2))
    submit(r, attempt(provider="FleetAPI")); submit(r, attempt(provider="Nope")._replace(key=VIN2))   # unknown: cloud
    assert len(entries(r.ble)) == 2 and len(entries(r.cloud)) == 2 and reports == []

def test_inline_lane_runs_reports_and_stamps_the_write():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, r, reports = setup(p)
    assert submit(r, attempt()) is True and kinds(reports) == [O.DONE] and p.calls == [("setChargeLimit", VIN, 80)]
    w = m.vehicleRegistry.get(VIN).lastWrite("chargeLimit"); assert (w.value, w.source) == (80, "TeslaBLE") and abs(w.at - time.time()) < 5   # wall-clock domain

def test_bad_return_and_exception_become_error_outcomes_without_a_stamp():
    p = ScriptedProvider("FleetAPI", 5, False, []); p.setChargeLimit = lambda vin, limit: "success"; m, r, reports = setup(p)
    submit(r, attempt(provider="FleetAPI")); assert reports[-1][1] == O.ERROR and m.vehicleRegistry.get(VIN).lastWrite("chargeLimit") is None
    def boom(vin, limit): raise RuntimeError("secret-token")
    p.setChargeLimit = boom; submit(r, attempt(provider="FleetAPI")); assert reports[-1][1] == O.ERROR

def test_wake_first_refused_aborts_and_a_failed_local_wake_still_sends():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.REFUSED, "key"), O.Outcome(O.DONE)]); m, r, reports = setup(p)
    submit(r, attempt(wake=True)); assert kinds(reports) == [O.REFUSED] and p.calls == [("wake", VIN, None)]
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)]); m, r, reports = setup(p)
    submit(r, attempt(wake=True)); assert kinds(reports) == [O.DONE] and [x[0] for x in p.calls] == ["wake", "setChargeLimit"]
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)]); m, r, reports = setup(f)
    submit(r, attempt(provider="FleetAPI", wake=True)); assert kinds(reports) == [O.UNREACHABLE] and [x[0] for x in f.calls] == ["wake"]

def test_read_before_write_satisfied_skips_the_command():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, r, reports = setup(p)
    m.vehicleRegistry.get(VIN).observe("chargeLimit", 80, "TeslaBLE")
    submit(r, attempt(maxAge=300)); assert reports == [("TeslaBLE", O.SATISFIED, "read")] and p.calls == []

def test_disruptive_stop_and_satisfied_start_are_not_stamped():
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.STOP_CHARGING: C.DISRUPTIVE}); m, r, reports = setup(g)
    submit(r, attempt(C.STOP_CHARGING, False, "Gen2TWC", CHARGING)); assert kinds(reports) == [O.DONE] and m.vehicleRegistry.get(VIN).lastWrite("chargingState") is None
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.SATISFIED, "complete")]); m, r, reports = setup(b)
    submit(r, attempt(C.START_CHARGING, True, "TeslaBLE", CHARGING)); assert m.vehicleRegistry.get(VIN).lastWrite("chargingState") is None

def test_queue_coalesces_per_key_aspect_stops_first_and_is_bounded_and_reports_the_losers():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.cloud
    for i in range(70): submit(r, attempt(C.SET_AMPS, i, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF%06d" % i))
    assert len(entries(q)) == VehicleCommandQueue.MAX_PENDING == 64 and [r[2] for r in reports] == ["dropped"] * 6
    submit(r, attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); assert len(entries(q)) == 64 and entries(q)[0].capability == C.STOP_CHARGING
    submit(r, attempt(C.SET_AMPS, 5, "FleetAPI", AMPS)); submit(r, attempt(C.SET_AMPS, 6, "FleetAPI", AMPS))
    assert sum(1 for a in entries(q) if a.key == VIN and a.aspect == AMPS) == 1 and reports[-1][2] == "replaced"

def test_cancel_removes_only_stops_and_reports_them():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.cloud
    submit(r, attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); submit(r, attempt(C.START_CHARGING, True, "FleetAPI", CHARGING)._replace(key=VIN2))
    assert r.cancel(VIN) == 1 and reports == [("FleetAPI", O.ERROR, "cancelled")] and len(entries(q)) == 1 and entries(q)[0].capability == C.START_CHARGING

def test_one_shots_never_replace_each_other():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.ble
    q.put(one_shot(C.WAKE)); q.put(one_shot(C.WAKE)); assert len(entries(q)) == 2 and reports == []
    assert entryKey(one_shot(C.WAKE)) != entryKey(one_shot(C.WAKE)) and entryKey(attempt()) == (VIN, CHARGE_LIMIT)

def test_debug_command_submits_a_one_shot_past_a_twc():
    t = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.SET_AMPS: C.LOCAL}, amps=(6, 32)); t.module = None
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(t, p)
    assert m.providerOrder.debugCommand("setChargeRate", VIN, '{"charge_rate": "12"}') is True and p.calls == [("setAmps", VIN, 12)] and t.calls == []
    assert m.providerOrder.debugCommand("wakeVehicle", "5YJ3E1EA1KF000009", "{}") is False and reports[-1][1] == O.DONE


# Threads, identity, logging and the drain edges

def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()

def threaded(*providers):
    # Started queues, no reconciler thread; the report runs on the queue's thread
    m, r = reconciler(*providers); reports = []; done = threading.Event()
    def report(a, out): reports.append((a, out)); done.set()
    r.report = report; r.ble.start(); r.cloud.start(); return m, r, reports, done

def joined(q):
    t = q._thread
    if t is not None:
        t.join(5)
        assert not t.is_alive(), "worker thread hung"

def gated(provider, gate):
    # The provider's wake blocks on gate without recording a call: a one-shot wake holds its worker
    def wake(vin): gate.wait(5); return O.Outcome(O.DONE)
    provider.wake = wake

def test_started_lane_runs_on_its_own_thread_and_drain_joins_it():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports, done = threaded(p)
    q = r.cloud; seen = []
    p.setChargeLimit = lambda vin, limit: (seen.append(threading.current_thread().name), O.Outcome(O.DONE))[1]
    a = attempt(provider="FleetAPI")
    try:
        assert submit(r, a) is True and done.wait(5)
        assert reports[0][0] is a and reports[0][1].kind == O.DONE and seen == [q._thread.name] == ["Vehicle-cloud"]
        w = m.vehicleRegistry.get(VIN).lastWrite("chargeLimit"); assert abs(w.at - time.time()) < 5
        drained = r.drain(5)
    finally:
        r.drain(5); joined(q); joined(r.ble)
    assert drained is True

def test_drain_runs_pending_stops_only():                                  # Review Focus 5
    gate = threading.Event(); p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(p, gate)
    m, r, reports, done = threaded(p); q = r.cloud; timer = threading.Timer(0.2, gate.set)
    try:
        wake = one_shot(C.WAKE, "FleetAPI"); submit(r, wake); assert wait_for(lambda: q._busy)
        limit = attempt(C.SET_CHARGE_LIMIT, 80, "FleetAPI", CHARGE_LIMIT); stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)
        submit(r, limit); submit(r, stop); timer.start()
        assert r.drain(5) is True and [c[0] for c in p.calls] == ["stopCharging"] and len(entries(q)) == 0
        assert [(r[0], r[1].kind, r[1].detail) for r in reports] == [(limit, O.ERROR, "dropped"), (wake, O.DONE, ""), (stop, O.DONE, "")]
    finally:
        gate.set(); joined(q); joined(r.ble); timer.join(5)

def test_drain_is_not_held_past_its_timeout_by_a_hung_attempt():   # Review Focus 5
    release = threading.Event(); started = threading.Event()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def slowStop(vin): started.set(); release.wait(10); return O.Outcome(O.DONE)
    p.stopCharging = slowStop; m, r, reports, done = threaded(p); q = r.cloud
    try:
        stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING); submit(r, stop); assert started.wait(5)
        limit = attempt(provider="FleetAPI"); submit(r, limit)
        t0 = time.time(); assert r.drain(0.3) is False and time.time() - t0 < 3
        assert [(r[0], r[1].detail) for r in reports] == [(limit, "dropped")]                # the limit is dropped, the stop still runs
        assert submit(r, attempt(provider="FleetAPI")) is False and reports[-1][1].detail == "dropped"   # closed after the drain
    finally:
        release.set(); joined(q); joined(r.ble)
    assert reports[-1][0] is stop and reports[-1][1].kind == O.DONE and len(reports) == 3     # each entry reported once

def test_drain_on_a_started_lane_lets_its_thread_run_the_pending_stop():
    gate = threading.Event(); calls = []; timer = threading.Timer(0, lambda: None)
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def first(vin, limit): gate.wait(5); calls.append(threading.current_thread().name); return O.Outcome(O.DONE)
    def stop(vin): calls.append(threading.current_thread().name); return O.Outcome(O.DONE)
    p.setChargeLimit = first; p.stopCharging = stop; m, r, reports, done = threaded(p); q = r.cloud
    try:
        submit(r, attempt(provider="FleetAPI")); assert wait_for(lambda: q._busy)
        submit(r, attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING))
        timer = threading.Timer(0.2, gate.set); timer.start()
        assert r.drain(5) is True and calls == [q._thread.name] * 2 and [r[1].kind for r in reports] == [O.DONE, O.DONE]
    finally:
        gate.set(); joined(q); joined(r.ble); timer.join(5)

def test_drain_delivers_a_cloud_stop_and_drops_a_ble_limit():       # Review Focus 5, both workers
    gate = threading.Event(); timer = threading.Timer(0.2, gate.set)
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(b, gate); gated(f, gate)
    m, r, reports, done = threaded(b, f)
    try:
        submit(r, one_shot(C.WAKE, "TeslaBLE")); submit(r, one_shot(C.WAKE, "FleetAPI"))
        assert wait_for(lambda: r.ble._busy and r.cloud._busy)
        submit(r, attempt(provider="TeslaBLE")); submit(r, attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); timer.start()
        assert r.drain(5) is True and b.calls == [] and f.calls == [("stopCharging", VIN, None)]
        got = sorted((a.provider, a.capability, out.kind, out.detail) for a, out in reports)
        assert got == [("FleetAPI", C.STOP_CHARGING, O.DONE, ""), ("FleetAPI", C.WAKE, O.DONE, ""), ("TeslaBLE", C.SET_CHARGE_LIMIT, O.ERROR, "dropped"), ("TeslaBLE", C.WAKE, O.DONE, "")]
        assert pending(r) == 0
    finally:
        gate.set(); joined(r.ble); joined(r.cloud); timer.join(5)

def test_replaced_and_dropped_report_the_losing_attempt_object():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, _ = setup(p, inline_=False)
    got = []; r.report = lambda a, out: got.append((a, out.detail)); q = r.cloud
    first = attempt(C.SET_AMPS, 5, "FleetAPI", AMPS); second = attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)
    submit(r, first); submit(r, second); assert got == [(first, "replaced")] and entries(q) == [second]
    q.close(); assert got[-1] == (second, "dropped") and len(entries(q)) == 0

def test_overflow_warns_once_and_a_full_lane_of_stops_rejects_the_newcomer(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.cloud
    with caplog.at_level(logging.DEBUG):
        for i in range(70): submit(r, attempt(C.SET_AMPS, i, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF%06d" % i))
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    q.close(); m, r, reports = setup(p, inline_=False); q = r.cloud
    for i in range(VehicleCommandQueue.MAX_PENDING): assert submit(r, attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)._replace(key="5YJ3E1EA1KF%06d" % i))
    extra = attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF000999")
    assert submit(r, extra) is False and len(entries(q)) == VehicleCommandQueue.MAX_PENDING and reports == [("FleetAPI", O.ERROR, "dropped")]

def test_cancel_leaves_a_running_stop_alone():
    gate = threading.Event()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def stop(vin): gate.wait(5); return O.Outcome(O.DONE)
    p.stopCharging = stop; m, r, reports, done = threaded(p); q = r.cloud
    try:
        submit(r, attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); assert wait_for(lambda: q._busy)
        assert r.cancel(VIN) == 0 and reports == []
        gate.set(); assert done.wait(5) and reports[0][1].kind == O.DONE
    finally:
        gate.set(); r.drain(5); joined(q); joined(r.ble)

def test_a_raising_report_is_logged_by_type_name(caplog):
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, r, _ = setup(p)
    def bad(a, out): raise ValueError("secret-token")
    r.report = bad
    with caplog.at_level(logging.DEBUG):
        assert submit(r, attempt()) is True
    warnings = [x.getMessage() for x in caplog.records if x.levelno >= logging.WARNING]
    assert any("ValueError" in w for w in warnings)
    assert "secret-token" not in caplog.text                                   # at every level, the DEBUG traceback too (audit R9)

@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")   # the SystemExit is the point
def test_a_worker_that_dies_logs_no_exception_text_at_any_level(caplog):     # audit R9
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.cloud
    def exit_now(vin): raise SystemExit("secret-token")
    p.stopCharging = exit_now
    with caplog.at_level(logging.DEBUG):
        q.start(); q.put(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); joined(q)
    assert reports == [("FleetAPI", O.ERROR, "SystemExit")] and "SystemExit" in caplog.text and "secret-token" not in caplog.text

def test_errors_log_type_names_only_and_warn_once(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, []); m, r, reports = setup(p)
    p.setAmps = lambda vin, amps: {"access_token": "secret-token"}
    with caplog.at_level(logging.DEBUG):
        submit(r, attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)); submit(r, attempt(C.SET_AMPS, 7, "FleetAPI", AMPS))
        def boom(vin, amps): raise RuntimeError("secret-token")
        p.setAmps = boom; submit(r, attempt(C.SET_AMPS, 8, "FleetAPI", AMPS))
    assert [r[1:] for r in reports] == [(O.ERROR, "bad return dict")] * 2 + [(O.ERROR, "RuntimeError")]
    assert "secret-token" not in caplog.text and len([r for r in caplog.records if "bad return" in r.getMessage() and r.levelno == logging.WARNING]) == 1

def test_read_before_write_uses_only_the_lane_s_own_providers():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    ble = ScriptedProvider("TeslaBLE", 2, True, []).reads(80)                                          # cheapest, but on the BLE worker
    custom = ScriptedProvider("Custom", 3, False, [], worker="ble").reads(75)                         # the worker attribute decides, not the name
    owner = ScriptedProvider("OwnerAPI", 4, False, []).reads(70)
    m, r, reports = setup(p, ble, custom, owner)
    submit(r, attempt(provider="FleetAPI", maxAge=300)); assert (ble.fetched, custom.fetched, owner.fetched) == (0, 0, 1) and p.calls == [("setChargeLimit", VIN, 80)] and kinds(reports) == [O.DONE]

def test_satisfied_limit_from_the_provider_is_stamped_and_no_vehicle_is_an_error():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.SATISFIED, "already")]); m, r, reports = setup(p)
    submit(r, attempt()); assert m.vehicleRegistry.get(VIN).lastWrite("chargeLimit").value == 80
    m.desired.set("5YJ3E1EA1KF000009", CHARGE_LIMIT, 80, "test")
    assert runAttempt(r, attempt()._replace(key="5YJ3E1EA1KF000009")) == O.Outcome(O.ERROR, "no vehicle")
    assert runAttempt(r, attempt(provider="Nope")).kind == O.ERROR

def test_one_shot_outcome_is_logged_at_info(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE, "timeout")]); m, r = reconciler(p); inline(r)
    with caplog.at_level(logging.INFO):
        assert m.providerOrder.debugCommand("wakeVehicle", VIN, "{}") is True
    assert "Debug wake for %s via FleetAPI: unreachable timeout" % VIN in caplog.text and p.calls == [("wake", VIN, None)]

def test_local_providers_run_inline_even_when_workers_are_threaded():
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.STOP_CHARGING: C.DISRUPTIVE}); m, r, reports = setup(g, inline_=False)
    assert submit(r, attempt(C.STOP_CHARGING, False, "Gen2TWC", CHARGING)) is True and kinds(reports) == [O.DONE] and pending(r) == 0
    assert r.cloud._thread is None


def test_a_raising_after_write_keeps_the_sent_command_done():          # I1: a sent paid command never comes back retryable
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p)
    def boom(*a): raise RuntimeError("secret-token")
    m.vehicleRegistry.get(VIN).noteWrite = boom
    assert submit(r, attempt(provider="FleetAPI")) is True and reports == [("FleetAPI", O.DONE, "")]
    assert p.calls == [("setChargeLimit", VIN, 80)]                                       # sent once, nothing resent
    p.cost = boom; submit(r, attempt(C.SET_AMPS, 7, "FleetAPI", AMPS)); assert reports[-1] == ("FleetAPI", O.DONE, "")   # amps: no rules read the cost first

def test_a_raising_after_write_hook_keeps_the_sent_command_done(monkeypatch):   # I1, the hook path
    def afterWrite(vehicle, attempt, outcome): raise ValueError("secret-token")
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p); r.afterWrite = afterWrite
    submit(r, attempt(provider="FleetAPI")); assert reports == [("FleetAPI", O.DONE, "")] and len(p.calls) == 1
    assert m.vehicleRegistry.get(VIN).lastWrite("chargeLimit").value == 80

def test_a_failed_billed_wake_never_sends_the_billed_command():        # I2
    for wakeResult in (O.Outcome(O.ERROR, "500"), True):
        f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); f.wake = lambda vin, r=wakeResult: (f.calls.append(("wake", vin, None)), r)[1]
        m, r, reports = setup(f)
        submit(r, attempt(provider="FleetAPI", wake=True)); assert [c[0] for c in f.calls] == ["wake"] and kinds(reports) == [O.ERROR]
    for ok in (O.DONE, O.SATISFIED, O.UNSUPPORTED):
        f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(ok), O.Outcome(O.DONE)]); m, r, reports = setup(f)
        submit(r, attempt(provider="FleetAPI", wake=True)); assert [c[0] for c in f.calls] == ["wake", "setChargeLimit"]
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.ERROR, "timeout"), O.Outcome(O.DONE)]); m, r, reports = setup(b)
    submit(r, attempt(wake=True)); assert [c[0] for c in b.calls] == ["wake", "setChargeLimit"] and kinds(reports) == [O.DONE]   # local: still sent

def test_a_failed_wake_is_tagged():                                         # pre-flight M8
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE, "asleep"), O.Outcome(O.DONE)]); m, r, reports = setup(f)
    submit(r, attempt(provider="FleetAPI", wake=True)); assert reports[-1][1:] == (O.UNREACHABLE, "wake asleep")
    f.outcomes = [O.Outcome(O.REFUSED, "wake held")]; submit(r, attempt(provider="FleetAPI", wake=True))
    assert reports[-1][1:] == (O.REFUSED, "wake held") and [c[0] for c in f.calls] == ["wake", "wake"]   # already tagged: not doubled

def test_a_wake_that_sent_no_request_keeps_its_not_sent_detail():      # Task 9 carry (a)
    for out in (O.Outcome(O.UNREACHABLE, "backoff"), O.Outcome(O.REFUSED, "no command token"), O.Outcome(O.UNREACHABLE, "online check failed")):
        f = ScriptedProvider("FleetAPI", 5, False, [out, O.Outcome(O.DONE)]); m, r, reports = setup(f)
        submit(r, attempt(provider="FleetAPI", wake=True))
        assert reports[-1][1:] == (out.kind, out.detail) and O.isNotSent(O.Outcome(*reports[-1][1:])) and [c[0] for c in f.calls] == ["wake"]

def test_an_inline_lane_is_busy_while_it_runs():                        # M3
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, r, reports = setup(p); q = r.ble; seen = []
    p.setChargeLimit = lambda vin, limit: (seen.append(q._busy), O.Outcome(O.DONE))[1]
    submit(r, attempt()); assert seen == [True] and q._busy is False

def test_an_attempt_whose_desire_changed_is_cancelled_before_any_read():   # pre-flight M7
    gate = threading.Event(); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(f, gate)
    m, r, reports, done = threaded(f); q = r.cloud
    src = f.reads("Stopped", field="chargingState")
    m.desired.set(VIN, CHARGING, True, "test")
    try:
        submit(r, one_shot(C.WAKE, "FleetAPI")); assert wait_for(lambda: q._busy)
        submit(r, attempt(C.START_CHARGING, True, "FleetAPI", CHARGING, maxAge=0))
        m.desired.clear(VIN, CHARGING); gate.set()                         # the brief's clearIf is Task 7's; clear is today's
        assert wait_for(lambda: len(reports) == 2) and reports[-1][1] == O.Outcome(O.ERROR, "cancelled") and f.calls == [] and src.fetched == 0
        m.desired.set(VIN, CHARGING, True, "test"); submit(r, attempt(C.START_CHARGING, True, "FleetAPI", CHARGING, maxAge=0))
        assert wait_for(lambda: len(reports) == 3) and reports[-1][1].kind == O.DONE and f.calls == [("startCharging", VIN, None)] and src.fetched == 1
    finally:
        gate.set(); r.drain(5); joined(q); joined(r.ble)

def test_base_exception_in_a_run_still_reports_and_clears_busy():            # ledger Task 3 M6
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, r, reports = setup(p); q = r.ble
    def exit_now(vin, limit): raise SystemExit("secret-token")
    p.setChargeLimit = exit_now
    with pytest.raises(SystemExit):
        submit(r, attempt())
    assert reports[-1] == ("TeslaBLE", O.ERROR, "SystemExit") and not q._busy


# Fix round 1

@pytest.mark.parametrize("putFirst", [True, False])
def test_a_stop_put_as_the_worker_exits_is_dropped_and_reported_once(putFirst):   # review I1
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.cloud
    real = q._next; gap = threading.Event(); hold = threading.Event()
    def slow():
        item = real()
        if item is None: gap.set(); hold.wait(5)     # widen the worker's exit window
        return item
    q._next = slow; r.ble.start(); r.cloud.start()
    try:
        q.close(); assert gap.wait(5)
        stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)
        if not putFirst: assert q.waitDrained(time.monotonic() + 1) is True
        t0 = time.monotonic(); assert submit(r, stop) is False and reports == [("FleetAPI", O.ERROR, "dropped")]
        if putFirst: assert q.waitDrained(time.monotonic() + 1) is True and time.monotonic() - t0 < 0.5
        assert len(entries(q)) == 0 and p.calls == []
    finally:
        hold.set(); joined(q)

@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")   # the SystemExit is the point
def test_a_dead_worker_rejects_new_entries_and_its_drain_does_not_wait():   # review M2
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p, inline_=False); q = r.cloud
    def exit_now(vin): raise SystemExit("bye")
    p.stopCharging = exit_now; q.start()
    q.put(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); joined(q)
    assert reports == [("FleetAPI", O.ERROR, "SystemExit")]
    assert q.put(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)) is False and reports[-1] == ("FleetAPI", O.ERROR, "dropped")
    t0 = time.monotonic(); assert q.waitDrained(time.monotonic() + 2) is True and time.monotonic() - t0 < 0.5


# Task 10: the reconciler owns the queues

def test_no_callback_lists_or_test_modes_in_lib():
    from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
    assert not hasattr(VehicleCommandQueue, "_entries") and "inline" not in inspect.signature(VehicleCommandQueue).parameters
    assert not hasattr(VehicleCommandQueue, "summary") and not hasattr(Run, "Workers") and not hasattr(Run, "RateLimits")
    assert not hasattr(DesiredStore, "onChange") and not hasattr(DesiredStore, "followRegistry") and not hasattr(VehicleRegistry, "onRemove")
    assert not hasattr(C, "RateLimit") and "rateLimit" not in C.Cost._fields

def test_the_store_and_the_registry_each_call_one_callable_outside_their_locks():
    m, r = reconciler(); woke = []; r.wake = lambda *a: woke.append(a)
    assert m.desired.changed.__self__ is r                                   # bound at construction
    m.desired.changed = lambda key, aspect: woke.append((key, aspect, m.desired._lock.locked()))
    m.desired.set(VIN, CHARGING, True, "test"); assert woke == [(VIN, CHARGING, False)]
    m.vehicleRegistry.removed = m.desired.forget; m.vehicleRegistry.forget(VIN); assert m.desired.peek(VIN) is None

def test_start_starts_both_queues_then_the_thread():
    m, r = reconciler()
    try:
        r.start(); assert r.ble._thread.is_alive() and r.cloud._thread.is_alive() and r.is_alive()
    finally:
        r.stop(); r.join(3); r.drain(5); joined(r.ble); joined(r.cloud)
    assert not r.is_alive()

def test_report_turns_a_raw_base_exception_into_an_error():
    m, r = reconciler(); m.desired.set(VIN, CHARGE_LIMIT, 80, "test"); a = attempt()
    m.desired.record(VIN, CHARGE_LIMIT).inFlight = a
    r.report(a, SystemExit("x")); assert m.desired.record(VIN, CHARGE_LIMIT).lastOutcome == O.Outcome(O.ERROR, "SystemExit")


# Slim Task 12b: moved from test_own_write_stamps.py and test_vehicle_command_queue.py

@pytest.mark.parametrize("name, local, tier", [("TeslaBLE", True, None), ("FleetAPI", False, C.FLEET_COMMAND), ("HomeAssistant", False, C.HA_BILLED)])
def test_a_rate_is_stamped_as_the_provider_s_own_write_only_on_success(name, local, tier):
    p = ScriptedProvider(name, 2, local, [O.Outcome(O.ERROR, "500"), O.Outcome(O.DONE)], tier=tier); m, r, reports = setup(p)
    v = m.vehicleRegistry.get(VIN); rate = attempt(C.SET_AMPS, 16, name, AMPS)
    submit(r, rate); assert kinds(reports) == [O.ERROR] and v.lastWrite("chargeCurrentRequest") is None
    submit(r, rate); w = v.lastWrite("chargeCurrentRequest"); assert (w.value, w.source) == (16, name) and v.lastWrite("availableCurrent") is None

def test_a_rate_for_a_forgotten_car_never_revives_it():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, r, reports = setup(p)
    rate = attempt(C.SET_AMPS, 16, "FleetAPI", AMPS); r.desired.set(VIN, AMPS, 16, "test"); m.vehicleRegistry.forget(VIN)
    assert runAttempt(r, rate) == O.Outcome(O.ERROR, "no vehicle") and m.vehicleRegistry.get(VIN) is None and p.calls == []

def test_put_never_blocks_while_the_worker_runs_and_a_raising_run_spares_the_worker():
    gate = threading.Event(); ran = []; reported = []
    def run(entry):
        if entry == "boom": raise RuntimeError("secret-token")
        gate.wait(5); ran.append(entry); return O.Outcome(O.DONE)
    q = VehicleCommandQueue(name="t", run=run, key=lambda e: e, isHigh=lambda e: False, report=lambda e, out: reported.append((e, out)), lost=lambda e, d: None)
    q.start()
    try:
        q.put("boom"); q.put("first"); assert wait_for(lambda: q._busy and len(reported) == 1)
        t0 = time.monotonic(); assert q.put("second") is True and time.monotonic() - t0 < 0.1
        assert isinstance(reported[0][1], RuntimeError)                                            # reported, and the worker survives
        gate.set(); assert wait_for(lambda: ran == ["first", "second"])
    finally:
        gate.set(); q.close(); assert q.waitDrained(time.monotonic() + 5) is True

def test_a_drain_of_an_idle_queue_returns_at_once():
    q = VehicleCommandQueue(name="t", run=lambda e: O.Outcome(O.DONE), key=lambda e: e, isHigh=lambda e: False, report=lambda e, out: None, lost=lambda e, d: None)
    q.start(); t0 = time.monotonic(); q.close()
    assert q.waitDrained(time.monotonic() + 5) is True and time.monotonic() - t0 < 0.5

def test_a_stop_submitted_during_a_live_drain_is_still_sent():             # deleted-test audit row 56
    gate = threading.Event(); p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(p, gate)
    m, r, reports, done = threaded(p); q = r.cloud; result = []
    try:
        submit(r, one_shot(C.WAKE, "FleetAPI")); assert wait_for(lambda: q._busy)
        drainer = threading.Thread(target=lambda: result.append(r.drain(5))); drainer.start()
        assert wait_for(lambda: q._draining)
        stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING); limit = attempt(provider="FleetAPI")._replace(key=VIN2)
        assert submit(r, stop) is True and submit(r, limit) is False             # mid-drain: the stop is taken, the limit dropped
        gate.set(); drainer.join(5)
        assert result == [True] and [c[0] for c in p.calls] == ["stopCharging"]
    finally:
        gate.set(); joined(q); joined(r.ble)
