import time
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING, DesiredStore
from tests.unit.reconcile_helpers import Clock
VIN = "5YJ3E1EA1KF000001"

def test_outcome_vocabulary():
    assert O.SETTLED | O.RETRYABLE | O.EXCLUDING == O.KINDS and O.isOutcome(O.Outcome(O.DONE)) and not O.isOutcome("done")
    assert O.Outcome(O.REFUSED, "401").detail == "401" and "cancelled" in O.LANE_DETAILS

def test_set_get_clear_and_change_signal():
    c = Clock(); s = DesiredStore(clock=c); seen = []; s.onChange(lambda key, aspect: seen.append((key, aspect)))
    assert s.set(VIN, CHARGE_LIMIT, 80, "policy") is True and s.set(VIN, CHARGE_LIMIT, 80, "policy") is False
    assert s.get(VIN, CHARGE_LIMIT).value == 80 and s.get(VIN, CHARGE_LIMIT).by == "policy" and seen == [(VIN, CHARGE_LIMIT)]
    assert s.clear(VIN, CHARGE_LIMIT) and s.get(VIN, CHARGE_LIMIT) is None

def test_default_clock_is_wall_time():
    s = DesiredStore(); before = time.time(); s.set(VIN, CHARGE_LIMIT, 80, "policy")
    assert before <= s.get(VIN, CHARGE_LIMIT).at <= time.time()

def test_adopted_slow_aspect_holds_until_a_decision():
    s = DesiredStore(); s.set(VIN, CHARGE_LIMIT, 50, "policy"); s.adopt(VIN, CHARGE_LIMIT, 80)
    assert s.get(VIN, CHARGE_LIMIT).value == 80 and s.get(VIN, CHARGE_LIMIT).adopted
    assert s.set(VIN, CHARGE_LIMIT, 50, "policy") is False and s.get(VIN, CHARGE_LIMIT).value == 80
    assert s.set(VIN, CHARGE_LIMIT, 50, "policy", decision=True) is True and s.get(VIN, CHARGE_LIMIT).value == 50

def test_fast_aspects_adopt_as_session_bounds():
    s = DesiredStore(); s.set(VIN, CHARGING, False, "distributor"); s.adopt(VIN, CHARGING, "Charging")
    assert s.get(VIN, CHARGING) is None and s.bounds(VIN).wantsCharge
    assert s.set(VIN, CHARGING, False, "distributor") is False                 # user started it: not re-stopped this session
    assert s.set(VIN, CHARGING, False, "distributor", forced=True) is True      # unless the wiring requires it
    s.set(VIN, AMPS, 32, "distributor"); s.adopt(VIN, AMPS, 16)
    assert s.bounds(VIN).maxAmps == 16 and s.get(VIN, AMPS) is None
    s.endSession(VIN); assert s.bounds(VIN).maxAmps is None and not s.bounds(VIN).wantsCharge and s.set(VIN, CHARGING, False, "distributor")

def test_adopting_a_user_stop_holds_off_starts_for_the_session():
    s = DesiredStore(); s.set(VIN, CHARGING, True, "distributor"); s.adopt(VIN, CHARGING, "Stopped")
    assert s.get(VIN, CHARGING) is None and s.bounds(VIN).userStopped and s.set(VIN, CHARGING, True, "distributor") is False
    s.endSession(VIN); assert s.set(VIN, CHARGING, True, "distributor") is True

def test_release_drops_only_an_undelivered_matching_desire():
    c = Clock(); s = DesiredStore(clock=c); s.set(VIN, CHARGING, False, "distributor")
    assert s.release(VIN, CHARGING, False, "distributor") is True and s.get(VIN, CHARGING) is None
    s.set(VIN, CHARGING, False, "distributor"); s.record(VIN, CHARGING).settledAt = c.t                 # same instant: settled
    assert s.settled(VIN, CHARGING) and s.release(VIN, CHARGING, False, "distributor") is False

def test_record_round_and_summary():
    s = DesiredStore(); r = s.record(VIN, AMPS); r.tried["TeslaBLE"] = 3; r.excluded.add("FleetAPI"); r.backoff = 600
    r.newRound(); assert r.tried == {} and r.excluded == set() and r.backoff == 600
    r.reset(); assert r.backoff == 0 and s.summary(VIN)["records"][AMPS]["backoff"] == 0 and s.keys() == [VIN]

def test_a_new_desire_resets_free_tries_and_a_free_only_backoff():
    c = Clock(); s = DesiredStore(clock=c); s.set(VIN, CHARGING, False, "distributor"); r = s.record(VIN, CHARGING)
    r.tried["TeslaBLE"] = 3; r.backoff = 600; r.backoffUntil = c.t + 600; r.notBefore = c.t + 60
    assert s.set(VIN, CHARGING, False, "distributor") is False and r.backoffUntil == c.t + 600            # unchanged: nothing reset
    assert s.set(VIN, CHARGING, True, "distributor") is True and (r.tried, r.backoff, r.backoffUntil, r.notBefore) == ({}, 0, 0, 0)
    r.tried.update({"TeslaBLE": 3, "FleetAPI": 2}); r.paid.add("FleetAPI"); r.backoff = 600; r.backoffUntil = c.t + 600
    assert s.set(VIN, CHARGING, False, "distributor") is True and r.tried == {"FleetAPI": 2} and r.backoffUntil == c.t + 600   # the paid backoff persists
    r.reset(); assert r.paid == set() and r.backoffUntil == 0
    assert s.state(VIN).offering is None; s.noteOffering(VIN, True); assert s.state(VIN).offering and s.state(VIN).offeringSince == c.t

def test_channel_health_is_per_car_and_provider_and_shared_by_aspects():
    s = DesiredStore(); h = s.health(VIN, "TeslaBLE"); assert not h.failing and h.failingSince is None
    h.failure(10.0); h.failure(70.0); assert h.failing and (h.failingSince, h.lastFailureAt) == (10.0, 70.0)
    h.success(80.0); assert not h.failing and h.lastSuccessAt == 80.0 and s.health(VIN, "TeslaBLE") is h and s.health(VIN, "FleetAPI") is not h
    h.failure(90.0); s.state(VIN).lastStopProvider = "TeslaBLE"; s.endSession(VIN)
    assert not s.health(VIN, "TeslaBLE").failing and s.state(VIN).lastStopProvider is None

def test_a_raising_change_callback_is_dropped_and_others_still_run():
    s = DesiredStore(); seen = []
    s.onChange(lambda key, aspect: 1 / 0); s.onChange(lambda key, aspect: seen.append(aspect))
    assert s.set(VIN, AMPS, 16, "distributor") and seen == [AMPS]

def test_scripted_provider_and_order_helpers():
    from TWCManager.Vehicle.Providers import Capability as C
    from tests.unit.reconcile_helpers import ScriptedProvider, make_order
    b = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE)])
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)], tier=C.FLEET_COMMAND)
    m, o = make_order({}, f, b); v = m.vehicleRegistry.getOrCreate(VIN, "t")
    assert not hasattr(m, "reconciler") and [p.name for p in o.candidates(v, C.SET_CHARGE_LIMIT)] == ["TeslaBLE", "FleetAPI"]
    assert o.candidates(None, C.SET_CHARGE_LIMIT) == [] and f.cost(C.WAKE) == C.FLEET_COMMAND
    assert [b.setChargeLimit(VIN, 80), b.stopCharging(VIN), b.wake(VIN)] == [O.Outcome(O.UNREACHABLE), O.Outcome(O.DONE), O.Outcome(O.DONE)]
    assert b.calls == [("setChargeLimit", VIN, 80), ("stopCharging", VIN, None), ("wake", VIN, None)]
    g = ScriptedProvider("Gen2TWC", 0, True, [O.Outcome(O.DONE)], readyFor=lambda vehicle, cap: cap == C.STOP_CHARGING)
    assert g.ready(v, C.STOP_CHARGING) and not g.ready(v, C.START_CHARGING)

def test_end_session_resets_health_and_records_in_place():
    s = DesiredStore(); h = s.health(VIN, "TeslaBLE"); r = s.record(VIN, AMPS); h.failure(10.0); r.backoff = 600
    s.endSession(VIN); assert s.health(VIN, "TeslaBLE") is h and not h.failing and s.record(VIN, AMPS) is r and r.backoff == 0

def test_forget_drops_every_trace_of_a_key():
    s = DesiredStore(); seen = []; s.onChange(lambda key, aspect: seen.append(key))
    s.set(VIN, AMPS, 16, "distributor"); s.health(VIN, "TeslaBLE").failure(1.0); s.noteOffering(VIN, True); seen.clear()
    assert s.forget(VIN) is True and s.keys() == [] and seen == [] and s.forget(VIN) is False and s.get(VIN, AMPS) is None

def test_registry_removals_forget_desired_state():
    from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
    c = Clock(); reg = VehicleRegistry({}, clock=c); s = DesiredStore(clock=c); s.followRegistry(reg)
    for _ in range(50):                                                           # anon sessions churn
        v = reg.beginPlugSession("A1"); s.set(v.key, AMPS, 16, "distributor"); s.record(v.key, AMPS); reg.endPlugSession("A1")
    assert s.keys() == []
    v = reg.beginPlugSession("B2"); s.set(v.key, CHARGING, False, "distributor")
    for _ in range(VehicleRegistry.CONFIRM_ATTEMPTS): reg.observeTwcVin("B2", VIN)   # the anon vehicle merges into the VIN
    assert v.key not in s.keys()
    s.set(VIN, CHARGE_LIMIT, 80, "policy"); reg.forget(VIN); assert s.keys() == []
    reg.getOrCreate(VIN, "t"); s.set(VIN, CHARGE_LIMIT, 80, "policy"); c.t += reg.forgetAfter + 1; reg.expire()
    assert s.keys() == []

def test_a_new_desire_drops_unsupported_exclusions_and_keeps_refused():
    s = DesiredStore(); s.set(VIN, AMPS, 16, "distributor"); r = s.record(VIN, AMPS)
    r.exclude("Gen2TWC", O.UNSUPPORTED); r.exclude("FleetAPI", O.REFUSED)
    assert r.excluded == {"Gen2TWC", "FleetAPI"}
    s.set(VIN, AMPS, 4, "distributor"); assert r.excluded == {"FleetAPI"}
    r.newRound(); assert r.excluded == set() and r.refused == set()

def test_a_by_only_change_is_a_no_op():
    s = DesiredStore(); seen = []; s.onChange(lambda key, aspect: seen.append(aspect)); s.set(VIN, CHARGING, False, "distributor")
    r = s.record(VIN, CHARGING); r.tried["TeslaBLE"] = 1; at = s.get(VIN, CHARGING).at; seen.clear()
    assert s.set(VIN, CHARGING, False, "heartbeat") is False and s.get(VIN, CHARGING) == (False, "distributor", at) + (False,) * 4
    assert r.tried == {"TeslaBLE": 1} and seen == []

def test_a_decision_on_the_adopted_value_takes_it_over_in_place():
    c = Clock(); s = DesiredStore(clock=c); s.adopt(VIN, CHARGE_LIMIT, 80); r = s.record(VIN, CHARGE_LIMIT); r.tried["TeslaBLE"] = 1; c.t += 5
    assert s.set(VIN, CHARGE_LIMIT, 80, "policy", decision=True) is False
    d = s.get(VIN, CHARGE_LIMIT); assert (d.value, d.by, d.at, d.adopted) == (80, "policy", 1000.0, False) and r.tried == {"TeslaBLE": 1}

def test_adopt_ignores_charging_values_that_are_not_a_user_action():
    s = DesiredStore(); s.set(VIN, CHARGING, False, "distributor")
    for value in ("Disconnected", "NoPower", None): s.adopt(VIN, CHARGING, value)
    b = s.bounds(VIN); assert not b.wantsCharge and not b.userStopped and s.get(VIN, CHARGING).value is False
    s.set(VIN, AMPS, 16, "distributor"); s.adopt(VIN, AMPS, None); assert b.maxAmps is None and s.get(VIN, AMPS).value == 16

def test_new_round_keeps_paid_and_offering_restamps_on_each_start():
    s = DesiredStore(); r = s.record(VIN, CHARGING); r.paid.add("FleetAPI"); r.tried["FleetAPI"] = 2; r.newRound()
    assert r.paid == {"FleetAPI"} and r.tried == {}
    c = Clock(); s = DesiredStore(clock=c); s.noteOffering(VIN, True); c.t += 10; s.noteOffering(VIN, True)
    assert s.state(VIN).offeringSince == 1000.0
    s.noteOffering(VIN, False); c.t += 10; s.noteOffering(VIN, True); assert s.state(VIN).offeringSince == 1020.0

def test_a_broken_reconciler_module_is_not_masked(monkeypatch):
    import sys, types
    import pytest
    broken = types.ModuleType("TWCManager.Vehicle.Reconcile.Reconciler")          # exists, but has no satisfied
    monkeypatch.setitem(sys.modules, "TWCManager.Vehicle.Reconcile.Reconciler", broken)
    with pytest.raises(ImportError):
        DesiredStore().adopt(VIN, CHARGING, "Charging")
