import logging
import threading
import time
import pytest
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Provider import Provider
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile import Run
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING, Attempt, DesiredStore
from TWCManager.Vehicle.Reconcile.Run import Workers, entryKey, runAttempt
from TWCManager.Vehicle.Registry.ReadSource import ReadSource
from TWCManager.VehicleCommandQueue import VehicleCommandQueue
from tests.unit.reconcile_helpers import Clock, ScriptedProvider, make_order
VIN = "5YJ3E1EA1KF000001"
VIN2 = "5YJ3E1EA1KF000002"

def attempt(cap=C.SET_CHARGE_LIMIT, value=80, provider="TeslaBLE", aspect=CHARGE_LIMIT, wake=False, maxAge=None):
    return Attempt(VIN, aspect, cap, value, provider, wake, maxAge, False, time.time())

def one_shot(cap, provider="TeslaBLE"):
    return Attempt(VIN, None, cap, None, provider, False, None, False, time.time())

def setup(*providers, inline=True):
    Run._badReturns.clear()      # warn-once state is per process
    m, o = make_order({}, *providers); m.providerOrder = o; m.vehicleRegistry.getOrCreate(VIN, "t")
    reports = []; m.workers = Workers(m, report=lambda a, out: reports.append((a.provider, out.kind, out.detail)), inline=inline); return m, reports

def kinds(reports): return [r[1] for r in reports]

class Src(ReadSource):
    def __init__(self, name, provider, value, field="chargeLimit"):
        self.name, self.provider, self.value, self.field, self.fetched = name, provider, value, field, 0; self.fields = frozenset({field})
    def canRead(self, vehicle): return True
    def fetch(self, vehicle, field, allowWake): self.fetched += 1; vehicle.observe(self.field, self.value, self.name); return {self.field: self.value}

def test_worker_follows_the_provider_s_worker_attribute():
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    x = ScriptedProvider("Custom", 7, False, [O.Outcome(O.DONE)], worker="ble"); m, reports = setup(b, f, x, inline=False)
    assert (b.worker, f.worker, x.worker, Provider.worker, ScriptedProvider("Gen3TWC", 1, True, []).worker) == ("ble", "cloud", "ble", "cloud", "local")
    m.workers.submit(attempt(provider="TeslaBLE")); m.workers.submit(attempt(provider="Custom")._replace(key=VIN2))
    m.workers.submit(attempt(provider="FleetAPI")); m.workers.submit(attempt(provider="Nope")._replace(key=VIN2))   # unknown: cloud
    assert m.workers.ble.pending() == 2 and m.workers.cloud.pending() == 2 and reports == []

def test_inline_lane_runs_reports_and_stamps_the_write():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p)
    assert m.workers.submit(attempt()) is True and kinds(reports) == [O.DONE] and p.calls == [("setChargeLimit", VIN, 80)]
    w = m.vehicleRegistry.get(VIN).lastWrite("chargeLimit"); assert (w.value, w.source) == (80, "TeslaBLE") and abs(w.at - time.time()) < 5   # wall-clock domain

def test_bad_return_and_exception_become_error_outcomes_without_a_stamp():
    p = ScriptedProvider("FleetAPI", 5, False, []); p.setChargeLimit = lambda vin, limit: "success"; m, reports = setup(p)
    m.workers.submit(attempt(provider="FleetAPI")); assert reports[-1][1] == O.ERROR and m.vehicleRegistry.get(VIN).lastWrite("chargeLimit") is None
    def boom(vin, limit): raise RuntimeError("secret-token")
    p.setChargeLimit = boom; m.workers.submit(attempt(provider="FleetAPI")); assert reports[-1][1] == O.ERROR

def test_wake_first_refused_aborts_and_a_failed_local_wake_still_sends():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.REFUSED, "key"), O.Outcome(O.DONE)]); m, reports = setup(p)
    m.workers.submit(attempt(wake=True)); assert kinds(reports) == [O.REFUSED] and p.calls == [("wake", VIN, None)]
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)]); m, reports = setup(p)
    m.workers.submit(attempt(wake=True)); assert kinds(reports) == [O.DONE] and [x[0] for x in p.calls] == ["wake", "setChargeLimit"]
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)]); m, reports = setup(f)
    m.workers.submit(attempt(provider="FleetAPI", wake=True)); assert kinds(reports) == [O.UNREACHABLE] and [x[0] for x in f.calls] == ["wake"]

def test_read_before_write_satisfied_skips_the_command():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p)
    m.vehicleRegistry.get(VIN).observe("chargeLimit", 80, "TeslaBLE")
    m.workers.submit(attempt(maxAge=300)); assert reports == [("TeslaBLE", O.SATISFIED, "read")] and p.calls == []

def test_disruptive_stop_and_satisfied_start_are_not_stamped():
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.STOP_CHARGING: C.Declaration(C.DISRUPTIVE)}); m, reports = setup(g)
    m.workers.submit(attempt(C.STOP_CHARGING, False, "Gen2TWC", CHARGING)); assert kinds(reports) == [O.DONE] and m.vehicleRegistry.get(VIN).lastWrite("chargingState") is None
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.SATISFIED, "complete")]); m, reports = setup(b)
    m.workers.submit(attempt(C.START_CHARGING, True, "TeslaBLE", CHARGING)); assert m.vehicleRegistry.get(VIN).lastWrite("chargingState") is None

def test_rate_slot_is_used_only_when_a_command_is_sent(monkeypatch):      # ledger Task 3 M2
    mono = Clock(); monkeypatch.setattr(Run, "_monotonic", mono)
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)], caps={C.SET_CHARGE_LIMIT: C.Declaration(C.FLEET_COMMAND, C.RateLimit(2, 60))})
    m, reports = setup(p); m.vehicleRegistry.get(VIN).observe("chargeLimit", 80, "FleetAPI")
    for _ in range(3): m.workers.submit(attempt(provider="FleetAPI", maxAge=300))          # read-satisfied: no slot
    assert kinds(reports) == [O.SATISFIED] * 3 and m.workers.allows(p, C.SET_CHARGE_LIMIT, VIN, CHARGE_LIMIT) and p.calls == []
    class Hooks:
        def beforeWrite(self, vehicle, attempt, provider, believed): return None, O.Outcome(O.SATISFIED, "held")
        def afterWrite(self, vehicle, attempt, outcome): pass
    monkeypatch.setattr(Run, "_reconcilerParts", lambda: (Hooks, Run._satisfied, frozenset())); m.reconciler = Hooks()
    for _ in range(3): m.workers.submit(attempt(provider="FleetAPI", value=70))             # rules-settled: no slot
    assert reports[-1] == ("FleetAPI", O.SATISFIED, "rules: held") and m.workers.allows(p, C.SET_CHARGE_LIMIT, VIN, CHARGE_LIMIT) and p.calls == []
    m.reconciler = None
    for v in (70, 71): assert m.workers.allows(p, C.SET_CHARGE_LIMIT, VIN, CHARGE_LIMIT); m.workers.submit(attempt(provider="FleetAPI", value=v))
    assert len(p.calls) == 2 and not m.workers.allows(p, C.SET_CHARGE_LIMIT, VIN, CHARGE_LIMIT) and m.workers.allows(p, C.SET_CHARGE_LIMIT, VIN2, CHARGE_LIMIT)
    mono.t += 61; assert m.workers.allows(p, C.SET_CHARGE_LIMIT, VIN, CHARGE_LIMIT)

def test_ha_start_and_stop_share_one_window_per_car(monkeypatch):           # ledger Task 5 M6
    mono = Clock(); monkeypatch.setattr(Run, "_monotonic", mono); limit = C.Declaration(C.HA_BILLED, C.RateLimit(1, 60))
    ha = ScriptedProvider("HomeAssistant", 6, False, [O.Outcome(O.DONE)], caps={C.START_CHARGING: limit, C.STOP_CHARGING: limit}); m, reports = setup(ha)
    assert m.workers.allows(ha, C.START_CHARGING, VIN, CHARGING)
    m.workers.submit(attempt(C.STOP_CHARGING, False, "HomeAssistant", CHARGING)); assert kinds(reports) == [O.DONE]
    assert not m.workers.allows(ha, C.START_CHARGING, VIN, CHARGING) and m.workers.allows(ha, C.START_CHARGING, VIN2, CHARGING)

def test_queue_coalesces_per_key_aspect_stops_first_and_is_bounded_and_reports_the_losers():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); q = m.workers.cloud
    for i in range(70): m.workers.submit(attempt(C.SET_AMPS, i, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF%06d" % i))
    assert q.pending() == VehicleCommandQueue.MAX_PENDING == 64 and [r[2] for r in reports] == ["dropped"] * 6
    m.workers.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); assert q.pending() == 64 and q._entries()[0].capability == C.STOP_CHARGING
    m.workers.submit(attempt(C.SET_AMPS, 5, "FleetAPI", AMPS)); m.workers.submit(attempt(C.SET_AMPS, 6, "FleetAPI", AMPS))
    assert sum(1 for a in q._entries() if a.key == VIN and a.aspect == AMPS) == 1 and reports[-1][2] == "replaced"

def test_cancel_removes_only_stops_and_reports_them():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); q = m.workers.cloud
    m.workers.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); m.workers.submit(attempt(C.START_CHARGING, True, "FleetAPI", CHARGING)._replace(key=VIN2))
    assert m.workers.cancel(VIN) == 1 and reports == [("FleetAPI", O.ERROR, "cancelled")] and q.pending() == 1 and q._entries()[0].capability == C.START_CHARGING

def test_one_shots_never_replace_each_other():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); q = m.workers.ble
    q.put(one_shot(C.WAKE)); q.put(one_shot(C.WAKE)); assert q.pending() == 2 and reports == []
    assert entryKey(one_shot(C.WAKE)) != entryKey(one_shot(C.WAKE)) and entryKey(attempt()) == (VIN, CHARGE_LIMIT)

def test_debug_command_submits_a_one_shot_past_a_twc():
    t = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.SET_AMPS: C.Declaration(C.LOCAL)}, amps=(6, 32)); t.module = None
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(t, p)
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
    # Started workers; the report callback runs on the worker thread
    m, o = make_order({}, *providers); m.providerOrder = o; m.vehicleRegistry.getOrCreate(VIN, "t")
    reports = []; done = threading.Event()
    def report(a, out): reports.append((a, out)); done.set()
    m.workers = Workers(m, report=report); m.workers.start(); return m, reports, done

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
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports, done = threaded(p)
    q = m.workers.cloud; seen = []
    p.setChargeLimit = lambda vin, limit: (seen.append(threading.current_thread().name), O.Outcome(O.DONE))[1]
    a = attempt(provider="FleetAPI")
    try:
        assert m.workers.submit(a) is True and done.wait(5)
        assert reports[0][0] is a and reports[0][1].kind == O.DONE and seen == [q._thread.name] == ["Vehicle-cloud"]
        w = m.vehicleRegistry.get(VIN).lastWrite("chargeLimit"); assert abs(w.at - time.time()) < 5
        drained = m.workers.drain(5)
    finally:
        m.workers.drain(5); joined(q); joined(m.workers.ble)
    assert drained is True

def test_drain_runs_pending_stops_only():                                  # Review Focus 5
    gate = threading.Event(); p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(p, gate)
    m, reports, done = threaded(p); q = m.workers.cloud; timer = threading.Timer(0.2, gate.set)
    try:
        wake = one_shot(C.WAKE, "FleetAPI"); m.workers.submit(wake); assert wait_for(lambda: q.busy)
        limit = attempt(C.SET_CHARGE_LIMIT, 80, "FleetAPI", CHARGE_LIMIT); stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)
        m.workers.submit(limit); m.workers.submit(stop); timer.start()
        assert m.workers.drain(5) is True and [c[0] for c in p.calls] == ["stopCharging"] and q.pending() == 0
        assert [(r[0], r[1].kind, r[1].detail) for r in reports] == [(limit, O.ERROR, "dropped"), (wake, O.DONE, ""), (stop, O.DONE, "")]
    finally:
        gate.set(); joined(q); joined(m.workers.ble); timer.join(5)

def test_drain_is_not_held_past_its_timeout_by_a_hung_attempt():   # Review Focus 5
    release = threading.Event(); started = threading.Event()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def slowStop(vin): started.set(); release.wait(10); return O.Outcome(O.DONE)
    p.stopCharging = slowStop; m, reports, done = threaded(p); q = m.workers.cloud
    try:
        stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING); m.workers.submit(stop); assert started.wait(5)
        limit = attempt(provider="FleetAPI"); m.workers.submit(limit)
        t0 = time.time(); assert m.workers.drain(0.3) is False and time.time() - t0 < 3
        assert [(r[0], r[1].detail) for r in reports] == [(limit, "dropped")]                # the limit is dropped, the stop still runs
        assert m.workers.submit(attempt(provider="FleetAPI")) is False and reports[-1][1].detail == "dropped"   # closed after the drain
    finally:
        release.set(); joined(q); joined(m.workers.ble)
    assert reports[-1][0] is stop and reports[-1][1].kind == O.DONE and len(reports) == 3     # each entry reported once

def test_drain_on_a_started_lane_lets_its_thread_run_the_pending_stop():
    gate = threading.Event(); calls = []; timer = threading.Timer(0, lambda: None)
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def first(vin, limit): gate.wait(5); calls.append(threading.current_thread().name); return O.Outcome(O.DONE)
    def stop(vin): calls.append(threading.current_thread().name); return O.Outcome(O.DONE)
    p.setChargeLimit = first; p.stopCharging = stop; m, reports, done = threaded(p); q = m.workers.cloud
    try:
        m.workers.submit(attempt(provider="FleetAPI")); assert wait_for(lambda: q.busy)
        m.workers.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING))
        timer = threading.Timer(0.2, gate.set); timer.start()
        assert m.workers.drain(5) is True and calls == [q._thread.name] * 2 and [r[1].kind for r in reports] == [O.DONE, O.DONE]
    finally:
        gate.set(); joined(q); joined(m.workers.ble); timer.join(5)

def test_drain_delivers_a_cloud_stop_and_drops_a_ble_limit():       # Review Focus 5, both workers
    gate = threading.Event(); timer = threading.Timer(0.2, gate.set)
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(b, gate); gated(f, gate)
    m, reports, done = threaded(b, f)
    try:
        m.workers.submit(one_shot(C.WAKE, "TeslaBLE")); m.workers.submit(one_shot(C.WAKE, "FleetAPI"))
        assert wait_for(lambda: m.workers.ble.busy and m.workers.cloud.busy)
        m.workers.submit(attempt(provider="TeslaBLE")); m.workers.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); timer.start()
        assert m.workers.drain(5) is True and b.calls == [] and f.calls == [("stopCharging", VIN, None)]
        got = sorted((a.provider, a.capability, out.kind, out.detail) for a, out in reports)
        assert got == [("FleetAPI", C.STOP_CHARGING, O.DONE, ""), ("FleetAPI", C.WAKE, O.DONE, ""), ("TeslaBLE", C.SET_CHARGE_LIMIT, O.ERROR, "dropped"), ("TeslaBLE", C.WAKE, O.DONE, "")]
        assert m.workers.pending() == 0
    finally:
        gate.set(); joined(m.workers.ble); joined(m.workers.cloud); timer.join(5)

def test_replaced_and_dropped_report_the_losing_attempt_object():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, _ = setup(p, inline=False)
    got = []; m.workers.setReport(lambda a, out: got.append((a, out.detail))); q = m.workers.cloud
    first = attempt(C.SET_AMPS, 5, "FleetAPI", AMPS); second = attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)
    m.workers.submit(first); m.workers.submit(second); assert got == [(first, "replaced")] and q._entries() == [second]
    q.close(); assert got[-1] == (second, "dropped") and q.pending() == 0

def test_overflow_warns_once_and_a_full_lane_of_stops_rejects_the_newcomer(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); q = m.workers.cloud
    with caplog.at_level(logging.DEBUG):
        for i in range(70): m.workers.submit(attempt(C.SET_AMPS, i, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF%06d" % i))
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    q.close(); m, reports = setup(p, inline=False); q = m.workers.cloud
    for i in range(VehicleCommandQueue.MAX_PENDING): assert m.workers.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)._replace(key="5YJ3E1EA1KF%06d" % i))
    extra = attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF000999")
    assert m.workers.submit(extra) is False and q.pending() == VehicleCommandQueue.MAX_PENDING and reports == [("FleetAPI", O.ERROR, "dropped")]

def test_cancel_leaves_a_running_stop_alone():
    gate = threading.Event()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def stop(vin): gate.wait(5); return O.Outcome(O.DONE)
    p.stopCharging = stop; m, reports, done = threaded(p); q = m.workers.cloud
    try:
        m.workers.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); assert wait_for(lambda: q.busy)
        assert m.workers.cancel(VIN) == 0 and reports == []
        gate.set(); assert done.wait(5) and reports[0][1].kind == O.DONE
    finally:
        gate.set(); m.workers.drain(5); joined(q); joined(m.workers.ble)

def test_a_raising_report_is_logged_by_type_name(caplog):
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, _ = setup(p)
    def bad(a, out): raise ValueError("secret-token")
    m.workers.setReport(bad)
    with caplog.at_level(logging.DEBUG):
        assert m.workers.submit(attempt()) is True
    assert "ValueError" in caplog.text and "secret-token" not in caplog.text

def test_errors_log_type_names_only_and_warn_once(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, []); m, reports = setup(p)
    p.setAmps = lambda vin, amps: {"access_token": "secret-token"}
    with caplog.at_level(logging.DEBUG):
        m.workers.submit(attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)); m.workers.submit(attempt(C.SET_AMPS, 7, "FleetAPI", AMPS))
        def boom(vin, amps): raise RuntimeError("secret-token")
        p.setAmps = boom; m.workers.submit(attempt(C.SET_AMPS, 8, "FleetAPI", AMPS))
    assert [r[1:] for r in reports] == [(O.ERROR, "bad return dict")] * 2 + [(O.ERROR, "RuntimeError")]
    assert "secret-token" not in caplog.text and len([r for r in caplog.records if "bad return" in r.getMessage() and r.levelno == logging.WARNING]) == 1

def test_read_before_write_uses_only_the_lane_s_own_sources():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, ScriptedProvider("TeslaBLE", 2, True, []))
    ble, other = Src("TeslaBLE", "TeslaBLE", 80), Src("TeslaMate", "Unregistered", 70)                   # an unregistered provider counts as cloud
    m.vehicleRegistry.registerReadSource(ble); m.vehicleRegistry.registerReadSource(other)
    m.workers.submit(attempt(provider="FleetAPI", maxAge=300)); assert ble.fetched == 0 and other.fetched == 1 and p.calls == [("setChargeLimit", VIN, 80)] and kinds(reports) == [O.DONE]

def test_satisfied_limit_from_the_provider_is_stamped_and_no_vehicle_is_an_error():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.SATISFIED, "already")]); m, reports = setup(p)
    m.workers.submit(attempt()); assert m.vehicleRegistry.get(VIN).lastWrite("chargeLimit").value == 80
    assert runAttempt(m, attempt()._replace(key="5YJ3E1EA1KF000009"), m.workers.rateLimits) == O.Outcome(O.ERROR, "no vehicle")
    assert runAttempt(m, attempt(provider="Nope"), m.workers.rateLimits).kind == O.ERROR

def test_one_shot_outcome_is_logged_at_info(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE, "timeout")]); m, reports = setup(p)
    with caplog.at_level(logging.INFO):
        assert m.providerOrder.debugCommand("wakeVehicle", VIN, "{}") is True
    assert "Debug wake for %s via FleetAPI: unreachable timeout" % VIN in caplog.text and p.calls == [("wake", VIN, None)]

def test_local_providers_run_inline_even_when_workers_are_threaded():
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.STOP_CHARGING: C.Declaration(C.DISRUPTIVE)}); m, reports = setup(g, inline=False)
    assert m.workers.submit(attempt(C.STOP_CHARGING, False, "Gen2TWC", CHARGING)) is True and kinds(reports) == [O.DONE] and m.workers.pending() == 0
    assert set(m.workers.summary()) == {"ble", "cloud"} and m.workers.summary()["cloud"]["started"] is False


def test_a_raising_after_write_keeps_the_sent_command_done():          # I1: a sent paid command never comes back retryable
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p)
    def boom(*a): raise RuntimeError("secret-token")
    m.vehicleRegistry.get(VIN).noteWrite = boom
    assert m.workers.submit(attempt(provider="FleetAPI")) is True and reports == [("FleetAPI", O.DONE, "")]
    assert p.calls == [("setChargeLimit", VIN, 80)]                                       # sent once, nothing resent
    p.cost = boom; m.workers.submit(attempt(provider="FleetAPI")); assert reports[-1] == ("FleetAPI", O.DONE, "")

def test_a_raising_after_write_hook_keeps_the_sent_command_done(monkeypatch):   # I1, the hook path
    class Hooks:
        def beforeWrite(self, vehicle, attempt, provider, believed): return attempt.value, None
        def afterWrite(self, vehicle, attempt, outcome): raise ValueError("secret-token")
    monkeypatch.setattr(Run, "_reconcilerParts", lambda: (Hooks, Run._satisfied, frozenset()))
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p); m.reconciler = Hooks()
    m.workers.submit(attempt(provider="FleetAPI")); assert reports == [("FleetAPI", O.DONE, "")] and len(p.calls) == 1
    assert m.vehicleRegistry.get(VIN).lastWrite("chargeLimit").value == 80

def test_a_failed_billed_wake_never_sends_the_billed_command():        # I2
    for wakeResult in (O.Outcome(O.ERROR, "500"), True):
        f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); f.wake = lambda vin, r=wakeResult: (f.calls.append(("wake", vin, None)), r)[1]
        m, reports = setup(f)
        m.workers.submit(attempt(provider="FleetAPI", wake=True)); assert [c[0] for c in f.calls] == ["wake"] and kinds(reports) == [O.ERROR]
    for ok in (O.DONE, O.SATISFIED, O.UNSUPPORTED):
        f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(ok), O.Outcome(O.DONE)]); m, reports = setup(f)
        m.workers.submit(attempt(provider="FleetAPI", wake=True)); assert [c[0] for c in f.calls] == ["wake", "setChargeLimit"]
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.ERROR, "timeout"), O.Outcome(O.DONE)]); m, reports = setup(b)
    m.workers.submit(attempt(wake=True)); assert [c[0] for c in b.calls] == ["wake", "setChargeLimit"] and kinds(reports) == [O.DONE]   # local: still sent

def test_a_failed_wake_is_tagged():                                         # pre-flight M8
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE, "asleep"), O.Outcome(O.DONE)]); m, reports = setup(f)
    m.workers.submit(attempt(provider="FleetAPI", wake=True)); assert reports[-1][1:] == (O.UNREACHABLE, "wake asleep")
    f.outcomes = [O.Outcome(O.REFUSED, "wake held")]; m.workers.submit(attempt(provider="FleetAPI", wake=True))
    assert reports[-1][1:] == (O.REFUSED, "wake held") and [c[0] for c in f.calls] == ["wake", "wake"]   # already tagged: not doubled

def test_an_inline_lane_is_busy_while_it_runs():                        # M3
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p); q = m.workers.ble; seen = []
    p.setChargeLimit = lambda vin, limit: (seen.append((q.busy, q.summary()["running"]["key"])), O.Outcome(O.DONE))[1]
    m.workers.submit(attempt()); assert seen == [(True, VIN)] and q.busy is False

def test_an_attempt_whose_desire_changed_is_cancelled_before_any_read():   # pre-flight M7
    gate = threading.Event(); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); gated(f, gate)
    m, reports, done = threaded(f); q = m.workers.cloud; m.desired = DesiredStore()
    src = Src("FleetAPI", "FleetAPI", "Stopped", field="chargingState"); m.vehicleRegistry.registerReadSource(src)
    m.desired.set(VIN, CHARGING, True, "test")
    try:
        m.workers.submit(one_shot(C.WAKE, "FleetAPI")); assert wait_for(lambda: q.busy)
        m.workers.submit(attempt(C.START_CHARGING, True, "FleetAPI", CHARGING, maxAge=0))
        m.desired.clear(VIN, CHARGING); gate.set()                         # the brief's clearIf is Task 7's; clear is today's
        assert wait_for(lambda: len(reports) == 2) and reports[-1][1] == O.Outcome(O.ERROR, "cancelled") and f.calls == [] and src.fetched == 0
        m.desired.set(VIN, CHARGING, True, "test"); m.workers.submit(attempt(C.START_CHARGING, True, "FleetAPI", CHARGING, maxAge=0))
        assert wait_for(lambda: len(reports) == 3) and reports[-1][1].kind == O.DONE and f.calls == [("startCharging", VIN, None)] and src.fetched == 1
    finally:
        gate.set(); m.workers.drain(5); joined(q); joined(m.workers.ble)

def test_base_exception_in_a_run_still_reports_and_clears_busy():            # ledger Task 3 M6
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p); q = m.workers.ble
    def exit_now(vin, limit): raise SystemExit("secret-token")
    p.setChargeLimit = exit_now
    with pytest.raises(SystemExit):
        m.workers.submit(attempt())
    assert reports[-1] == ("TeslaBLE", O.ERROR, "SystemExit") and not q.busy


# Fix round 1

@pytest.mark.parametrize("putFirst", [True, False])
def test_a_stop_put_as_the_worker_exits_is_dropped_and_reported_once(putFirst):   # review I1
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); q = m.workers.cloud
    real = q._next; gap = threading.Event(); hold = threading.Event()
    def slow():
        item = real()
        if item is None: gap.set(); hold.wait(5)     # widen the worker's exit window
        return item
    q._next = slow; m.workers.start()
    try:
        q.close(); assert gap.wait(5)
        stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)
        if not putFirst: assert q.waitDrained(time.monotonic() + 1) is True
        t0 = time.monotonic(); assert m.workers.submit(stop) is False and reports == [("FleetAPI", O.ERROR, "dropped")]
        if putFirst: assert q.waitDrained(time.monotonic() + 1) is True and time.monotonic() - t0 < 0.5
        assert q.pending() == 0 and p.calls == []
    finally:
        hold.set(); joined(q)

@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")   # the SystemExit is the point
def test_a_dead_worker_rejects_new_entries_and_its_drain_does_not_wait():   # review M2
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); q = m.workers.cloud
    def exit_now(vin): raise SystemExit("bye")
    p.stopCharging = exit_now; q.start()
    q.put(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); joined(q)
    assert reports == [("FleetAPI", O.ERROR, "SystemExit")]
    assert q.put(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)) is False and reports[-1] == ("FleetAPI", O.ERROR, "dropped")
    t0 = time.monotonic(); assert q.waitDrained(time.monotonic() + 2) is True and time.monotonic() - t0 < 0.5
