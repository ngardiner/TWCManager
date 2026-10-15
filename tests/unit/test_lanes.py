import logging
import threading
import time
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING, Attempt
from TWCManager.Vehicle.Reconcile.Lanes import Lane, Lanes, entryKey, runAttempt
from TWCManager.Vehicle.Registry.ReadSource import ReadSource
from tests.unit.reconcile_helpers import Clock, ScriptedProvider, make_order
VIN = "5YJ3E1EA1KF000001"

def attempt(cap=C.SET_CHARGE_LIMIT, value=80, provider="TeslaBLE", aspect=CHARGE_LIMIT, wake=False, maxAge=None):
    return Attempt(VIN, aspect, cap, value, provider, wake, maxAge, False, time.time())

def setup(*providers, inline=True):
    m, o = make_order({}, *providers); m.providerOrder = o; m.vehicleRegistry.getOrCreate(VIN, "t")
    reports = []; m.lanes = Lanes(m, report=lambda a, out: reports.append((a.provider, out.kind, out.detail)), inline=inline); return m, reports

def kinds(reports): return [r[1] for r in reports]

def test_lane_membership_and_unknown_provider_goes_to_cloud():
    m, _ = setup(); assert m.lanes.laneFor("TeslaBLE").name == "ble" and m.lanes.laneFor("Gen2TWC").name == "local" and m.lanes.laneFor("Nope").name == "cloud"

def test_inline_lane_runs_reports_and_stamps_the_write():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p)
    assert m.lanes.submit(attempt()) is True and kinds(reports) == [O.DONE] and p.calls == [("setChargeLimit", VIN, 80)]
    w = m.vehicleRegistry.get(VIN).lastWrite("chargeLimit"); assert (w.value, w.source) == (80, "TeslaBLE") and abs(w.at - time.time()) < 5   # wall-clock domain

def test_bad_return_and_exception_become_error_outcomes_without_a_stamp():
    p = ScriptedProvider("FleetAPI", 5, False, []); p.setChargeLimit = lambda vin, limit: "success"; m, reports = setup(p)
    m.lanes.submit(attempt(provider="FleetAPI")); assert reports[-1][1] == O.ERROR and m.vehicleRegistry.get(VIN).lastWrite("chargeLimit") is None
    def boom(vin, limit): raise RuntimeError("secret-token")
    p.setChargeLimit = boom; m.lanes.submit(attempt(provider="FleetAPI")); assert reports[-1][1] == O.ERROR

def test_wake_first_refused_aborts_and_a_failed_local_wake_still_sends():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.REFUSED, "key"), O.Outcome(O.DONE)]); m, reports = setup(p)
    m.lanes.submit(attempt(wake=True)); assert kinds(reports) == [O.REFUSED] and p.calls == [("wake", VIN, None)]
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)]); m, reports = setup(p)
    m.lanes.submit(attempt(wake=True)); assert kinds(reports) == [O.DONE] and [x[0] for x in p.calls] == ["wake", "setChargeLimit"]
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)]); m, reports = setup(f)
    m.lanes.submit(attempt(provider="FleetAPI", wake=True)); assert kinds(reports) == [O.UNREACHABLE] and [x[0] for x in f.calls] == ["wake"]

def test_read_before_write_satisfied_skips_the_command():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p)
    m.vehicleRegistry.get(VIN).observe("chargeLimit", 80, "TeslaBLE")
    m.lanes.submit(attempt(maxAge=300)); assert kinds(reports) == [O.SATISFIED] and p.calls == []

def test_disruptive_stop_and_satisfied_start_are_not_stamped():
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.STOP_CHARGING: C.Declaration(C.DISRUPTIVE)}); m, reports = setup(g)
    m.lanes.submit(attempt(C.STOP_CHARGING, False, "Gen2TWC", CHARGING)); assert kinds(reports) == [O.DONE] and m.vehicleRegistry.get(VIN).lastWrite("chargingState") is None
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.SATISFIED, "complete")]); m, reports = setup(b)
    m.lanes.submit(attempt(C.START_CHARGING, True, "TeslaBLE", CHARGING)); assert m.vehicleRegistry.get(VIN).lastWrite("chargingState") is None

def test_rate_limit_per_provider_capability_and_vin(monkeypatch):
    import TWCManager.Vehicle.Reconcile.Lanes as L
    mono = Clock(); monkeypatch.setattr(L, "_monotonic", mono)
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); p.declarations = lambda: {C.SET_AMPS: C.Declaration(C.FLEET_COMMAND, C.RateLimit(2, 60))}
    m, _ = setup(p)
    for _ in range(2): assert m.lanes.allows(p, C.SET_AMPS, VIN); m.lanes.submit(attempt(C.SET_AMPS, 10, "FleetAPI", AMPS))
    assert not m.lanes.allows(p, C.SET_AMPS, VIN) and m.lanes.allows(p, C.SET_AMPS, "5YJ3E1EA1KF000002")
    mono.t += 61; assert m.lanes.allows(p, C.SET_AMPS, VIN)

def test_queue_coalesces_per_key_aspect_stops_first_and_is_bounded_and_reports_the_losers():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); lane = m.lanes.laneFor("FleetAPI")
    for i in range(70): lane.submit(attempt(C.SET_AMPS, i, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF%06d" % i))
    assert lane.pending() == Lane.MAX_PENDING and [r[2] for r in reports] == ["dropped"] * 6
    lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); assert lane.pending() == Lane.MAX_PENDING and lane._peek().capability == C.STOP_CHARGING
    lane.submit(attempt(C.SET_AMPS, 5, "FleetAPI", AMPS)); lane.submit(attempt(C.SET_AMPS, 6, "FleetAPI", AMPS))
    assert sum(1 for a in lane._entries() if a.key == VIN and a.aspect == AMPS) == 1 and reports[-1][2] == "replaced"

def test_cancel_removes_only_stops_and_reports_them():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); lane = m.lanes.laneFor("FleetAPI")
    lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); lane.submit(attempt(C.START_CHARGING, True, "FleetAPI", CHARGING)._replace(key="5YJ3E1EA1KF000002"))
    assert m.lanes.cancel(VIN) == 1 and reports == [("FleetAPI", O.ERROR, "cancelled")] and lane.pending() == 1 and lane._peek().capability == C.START_CHARGING

def test_probe_and_one_shot_do_not_replace_each_other():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); lane = m.lanes.laneFor("TeslaBLE")
    lane.submit(attempt("probe", None, "TeslaBLE", None)); lane.submit(attempt(C.WAKE, None, "TeslaBLE", None)); assert lane.pending() == 2

def test_drain_runs_pending_stops_only():                                  # Review Focus 5
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); lane = m.lanes.laneFor("FleetAPI")
    lane.submit(attempt(C.SET_CHARGE_LIMIT, 80, "FleetAPI", CHARGE_LIMIT)); lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING))
    assert m.lanes.drain(5) is True                                                        # never started: the stop runs here
    assert [c[0] for c in p.calls] == ["stopCharging"] and lane.pending() == 0 and sorted(r[2] for r in reports) == ["", "dropped"]

def test_debug_command_submits_a_one_shot_past_a_twc():
    t = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.SET_AMPS: C.Declaration(C.LOCAL)}, amps=(6, 32)); t.module = None
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(t, p)
    assert m.providerOrder.debugCommand("setChargeRate", VIN, '{"charge_rate": "12"}') is True and p.calls == [("setAmps", VIN, 12)] and t.calls == []
    assert m.providerOrder.debugCommand("wakeVehicle", "5YJ3E1EA1KF000009", "{}") is False and reports[-1][1] == O.DONE


# Beyond the brief: threads, identity, logging, the probe path and the drain edges

def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()

def threaded(*providers):
    # A started lane with the production clock; the report callback runs on the lane thread
    m, o = make_order({}, *providers); m.providerOrder = o; m.vehicleRegistry.getOrCreate(VIN, "t")
    reports = []; done = threading.Event()
    def report(a, out): reports.append((a, out)); done.set()
    m.lanes = Lanes(m, report=report); m.lanes.start(); return m, reports, done

def joined(lane):
    t = lane._thread
    if t is not None:
        t.join(5)
        assert not t.is_alive(), "lane thread hung"

def test_started_lane_runs_on_its_own_thread_and_drain_joins_it():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports, done = threaded(p)
    lane = m.lanes.laneFor("FleetAPI"); seen = []
    p.setChargeLimit = lambda vin, limit: (seen.append(threading.current_thread().name), O.Outcome(O.DONE))[1]
    a = attempt(provider="FleetAPI")
    try:
        assert m.lanes.submit(a) is True and done.wait(5)
        assert reports[0][0] is a and reports[0][1].kind == O.DONE and seen == [lane._thread.name] != [threading.current_thread().name]
        w = m.vehicleRegistry.get(VIN).lastWrite("chargeLimit"); assert abs(w.at - time.time()) < 5
        drained = m.lanes.drain(5)
    finally:
        m.lanes.drain(5); joined(lane); joined(m.lanes.laneFor("TeslaBLE"))
    assert drained is True

def test_drain_is_not_held_past_its_timeout_by_a_hung_attempt():   # Review Focus 5
    release = threading.Event(); started = threading.Event()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def slowStop(vin): started.set(); release.wait(10); return O.Outcome(O.DONE)
    p.stopCharging = slowStop; m, reports, done = threaded(p); lane = m.lanes.laneFor("FleetAPI")
    try:
        stop = attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING); lane.submit(stop); assert started.wait(5)
        limit = attempt(provider="FleetAPI"); lane.submit(limit)
        t0 = time.time(); assert m.lanes.drain(0.3) is False and time.time() - t0 < 3
        assert [(r[0], r[1].detail) for r in reports] == [(limit, "dropped")]                # the limit is dropped, the stop still runs
        assert lane.submit(attempt(provider="FleetAPI")) is False and reports[-1][1].detail == "dropped"   # closed after the drain
    finally:
        release.set(); joined(lane)
    assert reports[-1][0] is stop and reports[-1][1].kind == O.DONE and len(reports) == 3     # each entry reported once

def test_drain_on_a_started_lane_lets_its_thread_run_the_pending_stop():
    gate = threading.Event(); calls = []; timer = threading.Timer(0, lambda: None)
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def first(vin, limit): gate.wait(5); calls.append(threading.current_thread().name); return O.Outcome(O.DONE)
    def stop(vin): calls.append(threading.current_thread().name); return O.Outcome(O.DONE)
    p.setChargeLimit = first; p.stopCharging = stop; m, reports, done = threaded(p); lane = m.lanes.laneFor("FleetAPI")
    try:
        lane.submit(attempt(provider="FleetAPI")); assert wait_for(lambda: lane.busy)
        lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING))
        timer = threading.Timer(0.2, gate.set); timer.start()
        assert m.lanes.drain(5) is True and calls == [lane._thread.name] * 2 and [r[1].kind for r in reports] == [O.DONE, O.DONE]
    finally:
        gate.set(); joined(lane); timer.join(5)

def test_replaced_and_dropped_report_the_losing_attempt_object():
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, _ = setup(p, inline=False)
    got = []; m.lanes.setReport(lambda a, out: got.append((a, out.detail))); lane = m.lanes.laneFor("FleetAPI")
    first = attempt(C.SET_AMPS, 5, "FleetAPI", AMPS); second = attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)
    lane.submit(first); lane.submit(second); assert got == [(first, "replaced")] and lane._entries() == [second]
    lane.stop(); assert got[-1] == (second, "dropped") and lane.pending() == 0

def test_overflow_warns_once_and_a_full_lane_of_stops_rejects_the_newcomer(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p, inline=False); lane = m.lanes.laneFor("FleetAPI")
    with caplog.at_level(logging.DEBUG):
        for i in range(70): lane.submit(attempt(C.SET_AMPS, i, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF%06d" % i))
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    lane.stop(); m, reports = setup(p, inline=False); lane = m.lanes.laneFor("FleetAPI")
    for i in range(Lane.MAX_PENDING): assert lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)._replace(key="5YJ3E1EA1KF%06d" % i))
    extra = attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)._replace(key="5YJ3E1EA1KF000999")
    assert lane.submit(extra) is False and lane.pending() == Lane.MAX_PENDING and reports == [("FleetAPI", O.ERROR, "dropped")]

def test_cancel_leaves_a_running_stop_alone():
    gate = threading.Event()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def stop(vin): gate.wait(5); return O.Outcome(O.DONE)
    p.stopCharging = stop; m, reports, done = threaded(p); lane = m.lanes.laneFor("FleetAPI")
    try:
        lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)); assert wait_for(lambda: lane.busy)
        assert m.lanes.cancel(VIN) == 0 and reports == []
        gate.set(); assert done.wait(5) and reports[0][1].kind == O.DONE
    finally:
        gate.set(); m.lanes.drain(5); joined(lane)

def test_a_raising_report_is_logged_by_type_name(caplog):
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, _ = setup(p)
    def bad(a, out): raise ValueError("secret-token")
    m.lanes.setReport(bad)
    with caplog.at_level(logging.DEBUG):
        assert m.lanes.submit(attempt()) is True
    assert "ValueError" in caplog.text and "secret-token" not in caplog.text

def test_errors_log_type_names_only_and_warn_once(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, []); m, reports = setup(p)
    p.setAmps = lambda vin, amps: {"access_token": "secret-token"}
    with caplog.at_level(logging.DEBUG):
        m.lanes.submit(attempt(C.SET_AMPS, 6, "FleetAPI", AMPS)); m.lanes.submit(attempt(C.SET_AMPS, 7, "FleetAPI", AMPS))
        def boom(vin, amps): raise RuntimeError("secret-token")
        p.setAmps = boom; m.lanes.submit(attempt(C.SET_AMPS, 8, "FleetAPI", AMPS))
    assert [r[1:] for r in reports] == [(O.ERROR, "bad return dict")] * 2 + [(O.ERROR, "RuntimeError")]
    assert "secret-token" not in caplog.text and len([r for r in caplog.records if "bad return" in r.getMessage() and r.levelno == logging.WARNING]) == 1

def test_probe_runs_alone_without_read_wake_or_stamp():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p)
    src = ReadSource(); src.name = "ble"; src.provider = "TeslaBLE"; src.fields = frozenset({"chargeLimit"}); src.fetched = 0
    def fetch(vehicle, field, allowWake): src.fetched += 1; return {}
    src.fetch = fetch; m.vehicleRegistry.registerReadSource(src)
    m.lanes.submit(Attempt(VIN, None, "probe", None, "TeslaBLE", True, 0, False, time.time()))
    assert p.calls == [("probe", VIN, None)] and kinds(reports) == [O.DONE] and m.vehicleRegistry.get(VIN).lastWrite("chargeLimit") is None and src.fetched == 0
    m.lanes.submit(attempt(maxAge=0)); assert src.fetched == 1          # the source is reachable: the probe really skipped it
    assert entryKey(Attempt(VIN, None, "probe", None, "TeslaBLE", False, None, False, 0)) == (VIN, "probe")

def test_read_before_write_uses_only_the_lane_s_own_sources():
    class Src(ReadSource):
        fields = frozenset({"chargeLimit"})
        def __init__(self, name, provider, value): self.name, self.provider, self.value, self.fetched = name, provider, value, 0
        def canRead(self, vehicle): return True
        def fetch(self, vehicle, field, allowWake): self.fetched += 1; vehicle.observe("chargeLimit", self.value, self.name); return {"chargeLimit": self.value}
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p)
    ble, cloud = Src("TeslaBLE", "TeslaBLE", 80), Src("FleetAPI", "FleetAPI", 70)
    m.vehicleRegistry.registerReadSource(ble); m.vehicleRegistry.registerReadSource(cloud)
    m.lanes.submit(attempt(provider="FleetAPI", maxAge=300)); assert ble.fetched == 0 and cloud.fetched == 1 and p.calls == [("setChargeLimit", VIN, 80)] and kinds(reports) == [O.DONE]

def test_satisfied_limit_from_the_provider_is_stamped_and_no_vehicle_is_an_error():
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.SATISFIED, "already")]); m, reports = setup(p)
    m.lanes.submit(attempt()); assert m.vehicleRegistry.get(VIN).lastWrite("chargeLimit").value == 80
    assert runAttempt(m, m.lanes.laneFor("TeslaBLE"), attempt()._replace(key="5YJ3E1EA1KF000009")) == O.Outcome(O.ERROR, "no vehicle")
    assert runAttempt(m, m.lanes.laneFor("TeslaBLE"), attempt(provider="Nope")).kind == O.ERROR

def test_one_shot_outcome_is_logged_at_info(caplog):
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.UNREACHABLE, "timeout")]); m, reports = setup(p)
    with caplog.at_level(logging.INFO):
        assert m.providerOrder.debugCommand("wakeVehicle", VIN, "{}") is True
    assert "Debug wake for %s via FleetAPI: unreachable timeout" % VIN in caplog.text and p.calls == [("wake", VIN, None)]

def test_local_lane_is_always_inline():
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], caps={C.STOP_CHARGING: C.Declaration(C.DISRUPTIVE)}); m, reports = setup(g, inline=False)
    assert m.lanes.submit(attempt(C.STOP_CHARGING, False, "Gen2TWC", CHARGING)) is True and kinds(reports) == [O.DONE] and m.lanes.pending() == 0
    assert set(m.lanes.summary()) == {"local", "ble", "cloud"} and m.lanes.summary()["local"]["inline"] is True

def test_drain_delivers_a_cloud_stop_and_drops_a_ble_limit():       # Review Focus 5, both lanes
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    m, reports = setup(b, f, inline=False)
    m.lanes.submit(attempt(provider="TeslaBLE")); m.lanes.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING))
    assert m.lanes.drain(5) is True and b.calls == [] and f.calls == [("stopCharging", VIN, None)]
    assert sorted(reports) == [("FleetAPI", O.DONE, ""), ("TeslaBLE", O.ERROR, "dropped")] and m.lanes.pending() == 0


# Fix round 1

def test_a_raising_after_write_keeps_the_sent_command_done():          # I1: a sent paid command never comes back retryable
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p)
    def boom(*a): raise RuntimeError("secret-token")
    m.vehicleRegistry.get(VIN).noteWrite = boom
    assert m.lanes.submit(attempt(provider="FleetAPI")) is True and reports == [("FleetAPI", O.DONE, "")]
    assert p.calls == [("setChargeLimit", VIN, 80)]                                       # sent once, nothing resent
    p.cost = boom; m.lanes.submit(attempt(provider="FleetAPI")); assert reports[-1] == ("FleetAPI", O.DONE, "")

def test_a_raising_after_write_hook_keeps_the_sent_command_done(monkeypatch):   # I1, the Task 6 hook path
    import TWCManager.Vehicle.Reconcile.Lanes as L
    class Hooks:
        def beforeWrite(self, vehicle, attempt, provider, believed): return attempt.value, None
        def afterWrite(self, vehicle, attempt, outcome): raise ValueError("secret-token")
    monkeypatch.setattr(L, "_reconcilerParts", lambda: (Hooks, L._satisfied, frozenset()))
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); m, reports = setup(p); m.reconciler = Hooks()
    m.lanes.submit(attempt(provider="FleetAPI")); assert reports == [("FleetAPI", O.DONE, "")] and len(p.calls) == 1
    assert m.vehicleRegistry.get(VIN).lastWrite("chargeLimit").value == 80

def test_a_failed_billed_wake_never_sends_the_billed_command():        # I2
    for wakeResult in (O.Outcome(O.ERROR, "500"), True):
        f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)]); f.wake = lambda vin, r=wakeResult: (f.calls.append(("wake", vin, None)), r)[1]
        m, reports = setup(f)
        m.lanes.submit(attempt(provider="FleetAPI", wake=True)); assert [c[0] for c in f.calls] == ["wake"] and kinds(reports) == [O.ERROR]
    for ok in (O.DONE, O.SATISFIED, O.UNSUPPORTED):
        f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(ok), O.Outcome(O.DONE)]); m, reports = setup(f)
        m.lanes.submit(attempt(provider="FleetAPI", wake=True)); assert [c[0] for c in f.calls] == ["wake", "setChargeLimit"]
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.ERROR, "timeout"), O.Outcome(O.DONE)]); m, reports = setup(b)
    m.lanes.submit(attempt(wake=True)); assert [c[0] for c in b.calls] == ["wake", "setChargeLimit"] and kinds(reports) == [O.DONE]   # local: still sent

def test_concurrent_drains_of_a_never_started_lane_share_one_runner():   # M1
    gate = threading.Event(); started = threading.Event(); state = {"in": 0, "max": 0}; lock = threading.Lock()
    p = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)])
    def stop(vin):
        with lock: state["in"] += 1; state["max"] = max(state["max"], state["in"])
        started.set(); gate.wait(5)
        with lock: state["in"] -= 1
        return O.Outcome(O.DONE)
    p.stopCharging = stop; m, reports = setup(p, inline=False); lane = m.lanes.laneFor("FleetAPI")
    for i in range(3):
        k = "5YJ3E1EA1KF%06d" % (i + 10); m.vehicleRegistry.getOrCreate(k, "t"); lane.submit(attempt(C.STOP_CHARGING, False, "FleetAPI", CHARGING)._replace(key=k))
    res = []; ts = [threading.Thread(target=lambda: res.append(lane.drain(5))) for _ in range(3)]
    ts[0].start(); assert started.wait(5)
    for t in ts[1:]: t.start()
    time.sleep(0.1); gate.set()
    for t in ts: t.join(10); assert not t.is_alive()
    assert res == [True] * 3 and state["max"] == 1 and kinds(reports) == [O.DONE] * 3 and lane.pending() == 0

def test_an_inline_lane_is_busy_while_it_runs():                        # M3
    p = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)]); m, reports = setup(p); lane = m.lanes.laneFor("TeslaBLE"); seen = []
    p.setChargeLimit = lambda vin, limit: (seen.append((lane.busy, lane.summary()["running"]["key"])), O.Outcome(O.DONE))[1]
    m.lanes.submit(attempt()); assert seen == [(True, VIN)] and lane.busy is False
