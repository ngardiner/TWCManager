import time
import pytest
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING, Attempt, DesiredStore
from tests.unit.reconcile_helpers import Clock, settled
VIN = "5YJ3E1EA1KF000001"
VIN2 = "5YJ3E1EA1KF000002"

def attempt(provider, aspect=CHARGE_LIMIT, value=50):
    return Attempt(VIN, aspect, C.SET_CHARGE_LIMIT, value, provider, False, None, False, 1000.0)

def test_outcome_vocabulary():
    assert O.SETTLED | O.RETRYABLE | O.EXCLUDING == O.KINDS and O.isOutcome(O.Outcome(O.DONE)) and not O.isOutcome("done")
    assert O.Outcome(O.REFUSED, "401").detail == "401" and "cancelled" in O.QUEUE_DETAILS
    assert "wake held" in O.NOT_SENT and O.QUEUE_DETAILS <= O.NOT_SENT
    assert O.isNotSent(O.Outcome(O.UNREACHABLE, "wake held")) and O.isNotSent(O.Outcome(O.SATISFIED, "rules: limit unknown"))
    assert not O.isNotSent(O.Outcome(O.ERROR, "500")) and not O.isNotSent(O.Outcome(O.DONE)) and not O.isNotSent(O.Outcome(O.ERROR, "rules"))

def test_set_get_clear_and_change_signal():
    c = Clock(); s = DesiredStore(clock=c); seen = []; s.changed = lambda key, aspect: seen.append((key, aspect))
    assert s.set(VIN, CHARGE_LIMIT, 80, "policy") is True and s.set(VIN, CHARGE_LIMIT, 80, "policy") is False
    assert s.get(VIN, CHARGE_LIMIT).value == 80 and s.get(VIN, CHARGE_LIMIT).by == "policy" and seen == [(VIN, CHARGE_LIMIT)]
    assert s.clear(VIN, CHARGE_LIMIT) and s.get(VIN, CHARGE_LIMIT) is None

def test_default_clock_is_wall_time():
    s = DesiredStore(); before = time.time(); s.set(VIN, CHARGE_LIMIT, 80, "policy"); s.adopt(VIN2, CHARGE_LIMIT, 70)
    assert before <= s.get(VIN, CHARGE_LIMIT).at <= time.time() and before <= s.get(VIN2, CHARGE_LIMIT).at <= time.time()

def test_adopted_slow_aspect_holds_until_a_decision():
    s = DesiredStore(); s.set(VIN, CHARGE_LIMIT, 50, "policy"); s.adopt(VIN, CHARGE_LIMIT, 80)
    assert s.get(VIN, CHARGE_LIMIT).value == 80 and s.get(VIN, CHARGE_LIMIT).adopted
    assert s.set(VIN, CHARGE_LIMIT, 50, "policy") is False and s.get(VIN, CHARGE_LIMIT).value == 80
    assert s.set(VIN, CHARGE_LIMIT, 50, "policy", decision=True) is True and s.get(VIN, CHARGE_LIMIT).value == 50

def test_adopt_is_for_the_charge_limit_only():
    s = DesiredStore(); s.adopt(VIN, CHARGE_LIMIT, 80); assert s.get(VIN, CHARGE_LIMIT).adopted
    with pytest.raises(ValueError): s.adopt(VIN, CHARGING, "Charging")
    with pytest.raises(ValueError): s.adopt(VIN, AMPS, 16)
    s.adopt(VIN, CHARGE_LIMIT, None); assert s.get(VIN, CHARGE_LIMIT).value == 80        # an unread value is never the user's

def test_clear_if_drops_only_a_matching_value():
    s = DesiredStore(); s.set(VIN, CHARGING, True, "heartbeat")
    assert s.clearIf(VIN, CHARGING, False) is False and s.clearIf(VIN, CHARGING, True) is True and s.get(VIN, CHARGING) is None
    assert s.clearIf(VIN2, CHARGING, True) is False and VIN2 not in s.keys()

def test_free_channel_is_per_car_and_shared_by_aspects():
    s = DesiredStore(); f = s.free(VIN); f.failure(0.0); f.failure(70.0)
    assert (f.failingSince, f.lastAttemptAt) == (0.0, 70.0) and s.free(VIN) is f and s.free(VIN2) is not f
    f.success(80.0); assert f.failingSince is None and f.lastAttemptAt == 80.0 and s.free(VIN) is f

def test_note_outcome_bookkeeping():
    c = Clock(0.0); s = DesiredStore(clock=c); s.set(VIN, CHARGE_LIMIT, 50, "policy"); r = s.record(VIN, CHARGE_LIMIT)
    a = attempt("FleetAPI"); r.inFlight = a
    assert s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.ERROR, "500"), 100.0, paid=True)
    assert r.paidAt == 100.0 and r.lastSentAt == 100.0 and r.inFlight is None and s.free(VIN).failingSince is None
    assert r.paidRetryDue and r.lastFreeAt == 0 and (r.lastOutcome, r.lastOutcomeAt, r.lastProvider) == (O.Outcome(O.ERROR, "500"), 100.0, "FleetAPI")
    b = attempt("TeslaBLE"); r.inFlight = b
    s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.ERROR, "pairing"), 110.0, paid=False)
    assert s.free(VIN).failingSince is None and r.lastFreeAt == 110.0                        # pairing window: not a failure
    r.inFlight = b; s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.UNREACHABLE, "no reply"), 120.0, paid=False)
    assert s.free(VIN).failingSince == 120.0 and r.paidRetryDue                              # a free outcome leaves the paid retry alone
    assert s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.DONE), 130.0, paid=True) is False   # stale report
    assert r.lastOutcomeAt == 120.0
    r.inFlight = a; s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.UNREACHABLE, "wake held"), 140.0, paid=True)
    assert r.paidAt == 100.0 and r.lastSentAt == 120.0 and r.paidRetryDue and r.lastOutcomeAt == 140.0   # nothing was sent
    r.inFlight = a; s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.UNREACHABLE, "rules: limit unknown"), 150.0, paid=True)
    assert r.paidAt == 100.0                                                                  # rules outcome: no request
    r.inFlight = a; s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.ERROR, "500"), 160.0, paid=True)
    assert r.paidAt == 160.0 and not r.paidRetryDue                                          # the one retry is used
    r.inFlight = a; s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.ERROR, "cancelled"), 170.0, paid=True)
    assert r.inFlight is None and r.lastOutcomeAt == 160.0                                   # a queue loser never ran
    r.inFlight = b; s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.REFUSED, "key"), 180.0, paid=False)
    assert r.excluded == {"TeslaBLE": O.REFUSED} and s.free(VIN).lastAttemptAt == 180.0 and s.free(VIN).failingSince == 120.0
    r.inFlight = b; s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.DONE), 190.0, paid=False)
    assert r.settledAt == 190.0 and s.free(VIN).failingSince is None and settled(s, VIN, CHARGE_LIMIT)

def test_a_failed_paid_wake_sets_no_retry():
    s = DesiredStore(); s.set(VIN, CHARGE_LIMIT, 50, "policy"); r = s.record(VIN, CHARGE_LIMIT); a = attempt("FleetAPI")._replace(since=s.get(VIN, CHARGE_LIMIT).at); r.inFlight = a
    s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.UNREACHABLE, "wake asleep"), 100.0, paid=True)
    assert r.paidAt == 100.0 and r.paidRetryDue is False

def test_a_new_desire_resets_the_per_desire_stamps():
    s = DesiredStore(); s.set(VIN, CHARGE_LIMIT, 50, "policy"); r = s.record(VIN, CHARGE_LIMIT)
    r.lastFreeAt, r.paidAt, r.paidRetryDue, r.settledAt, r.lastSentAt = 1.0, 2.0, True, 3.0, 4.0
    r.paidSends, r.paidSaw, r.lastOutcome, r.lastOutcomeAt = 1, 90, O.Outcome(O.UNREACHABLE, "wake held"), 5.0
    assert s.set(VIN, CHARGE_LIMIT, 50, "heartbeat") is False and r.paidAt == 2.0          # unchanged: nothing reset
    s.set(VIN, CHARGE_LIMIT, 60, "policy")
    assert (r.lastFreeAt, r.paidAt, r.paidRetryDue, r.settledAt, r.lastSentAt) == (0, None, False, None, 4.0)
    assert (r.paidSends, r.paidSaw, r.lastOutcome, r.lastOutcomeAt) == (0, None, None, 0)   # the NOT_SENT hold is the old desire's (I1)

def test_a_superseded_attempt_feeds_the_free_channel_but_never_the_new_desire():   # C1
    s = DesiredStore(clock=Clock(1000.0)); s.set(VIN, CHARGE_LIMIT, 50, "policy"); r = s.record(VIN, CHARGE_LIMIT)
    a = attempt("FleetAPI"); r.inFlight = a; s.set(VIN, CHARGE_LIMIT, 60, "policy")
    assert s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.DONE), 1001.0, paid=True)
    assert r.inFlight is None and (r.settledAt, r.paidAt, r.paidSends, r.lastOutcome) == (None, None, 0, None) and not settled(s, VIN, CHARGE_LIMIT)
    assert r.lastSentAt == 1001.0                                                            # it was sent: the gap still spaces the next (I5)
    b = attempt("TeslaBLE"); r.inFlight = b
    s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.UNREACHABLE, "no reply"), 1002.0, paid=False)
    assert s.free(VIN).failingSince == 1002.0 and r.lastFreeAt == 0
    r.inFlight = b; s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.UNSUPPORTED, "range"), 1003.0, paid=False)
    assert r.excluded == {}                                                                  # value-dependent: not the new value's
    r.inFlight = b; s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.REFUSED, "key"), 1004.0, paid=False)
    assert r.excluded == {"TeslaBLE": O.REFUSED}

def test_a_raising_change_callback_does_not_break_the_write():
    s = DesiredStore(); s.changed = lambda key, aspect: 1 / 0
    assert s.set(VIN, AMPS, 16, "distributor") and s.get(VIN, AMPS).value == 16

def test_scripted_provider_and_order_helpers():
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

def test_forget_drops_every_trace_of_a_key():
    s = DesiredStore(); seen = []; s.changed = lambda key, aspect: seen.append(key)
    s.set(VIN, AMPS, 16, "distributor"); s.free(VIN).failure(1.0); s.state(VIN).lastStopProvider = "TeslaBLE"; seen.clear()
    assert s.forget(VIN) is True and s.keys() == [] and seen == [] and s.forget(VIN) is False and s.get(VIN, AMPS) is None
    assert s.free(VIN).failingSince is None and s.peek(VIN).lastStopProvider is None

def test_registry_removals_forget_desired_state():
    from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
    c = Clock(); reg = VehicleRegistry({}, clock=c); s = DesiredStore(clock=c); reg.removed = s.forget
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
    assert r.excluded == {"Gen2TWC": O.UNSUPPORTED, "FleetAPI": O.REFUSED}
    s.set(VIN, AMPS, 4, "distributor"); assert r.excluded == {"FleetAPI": O.REFUSED}
    assert s.record(VIN, AMPS).excluded == {"FleetAPI": O.REFUSED} and s.keys() == [VIN]

def test_a_by_only_change_is_a_no_op():
    s = DesiredStore(); seen = []; s.changed = lambda key, aspect: seen.append(aspect); s.set(VIN, CHARGING, False, "distributor")
    r = s.record(VIN, CHARGING); r.lastFreeAt = 5.0; at = s.get(VIN, CHARGING).at; seen.clear()
    assert s.set(VIN, CHARGING, False, "heartbeat") is False and s.get(VIN, CHARGING) == (False, "distributor", at) + (False,) * 3
    assert r.lastFreeAt == 5.0 and seen == []

def test_a_decision_on_the_adopted_value_takes_it_over_in_place():
    c = Clock(); s = DesiredStore(clock=c); s.adopt(VIN, CHARGE_LIMIT, 80); r = s.record(VIN, CHARGE_LIMIT); r.lastFreeAt = 5.0; c.t += 5
    assert s.set(VIN, CHARGE_LIMIT, 80, "policy", decision=True) is False
    d = s.get(VIN, CHARGE_LIMIT); assert (d.value, d.by, d.at, d.adopted) == (80, "policy", 1000.0, False) and r.lastFreeAt == 5.0


# Task 7 carry-forwards

@pytest.mark.parametrize("detail", ["no vehicle", "no provider"])
def test_a_worker_error_that_made_no_request_counts_against_nothing(detail):
    s = DesiredStore(); s.set(VIN, CHARGE_LIMIT, 50, "policy"); r = s.record(VIN, CHARGE_LIMIT)
    a = attempt("FleetAPI"); r.inFlight = a
    assert detail in O.NOT_SENT and s.noteOutcome(VIN, CHARGE_LIMIT, a, O.Outcome(O.ERROR, detail), 100.0, paid=True)
    assert r.paidAt is None and not r.paidRetryDue and r.lastSentAt == 0
    b = attempt("TeslaBLE"); r.inFlight = b
    s.noteOutcome(VIN, CHARGE_LIMIT, b, O.Outcome(O.ERROR, detail), 110.0, paid=False)
    assert s.free(VIN).failingSince is None and s.free(VIN).lastAttemptAt == 0 and r.lastFreeAt == 0

def test_a_debug_one_shot_report_changes_nothing():
    s = DesiredStore(); a = attempt("FleetAPI", aspect=None)
    assert s.noteOutcome(VIN, None, a, O.Outcome(O.DONE), 100.0, paid=True) is False and s.keys() == []


@pytest.mark.parametrize("outcome", [O.Outcome(O.SATISFIED, "read"), O.Outcome(O.SATISFIED, "rules: limit unknown"), O.Outcome(O.UNREACHABLE, "rules: could not read")])
def test_read_and_rules_outcomes_spend_no_send_gap(outcome):                 # deleted-test audit row 77
    c = Clock(0.0); s = DesiredStore(clock=c); s.set(VIN, CHARGE_LIMIT, 50, "policy"); r = s.record(VIN, CHARGE_LIMIT)
    a = attempt("FleetAPI"); r.inFlight = a
    assert s.noteOutcome(VIN, CHARGE_LIMIT, a, outcome, 100.0, paid=True)
    assert r.lastSentAt == 0 and r.paidAt is None and r.paidSends == 0
