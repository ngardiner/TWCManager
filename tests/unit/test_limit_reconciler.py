import time
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile import Run
from TWCManager.Vehicle.Reconcile.Desired import CHARGE_LIMIT, CHARGING, Attempt, DesiredStore
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit
from TWCManager.Vehicle.Reconcile.Reconciler import PAID_AFTER, PROBE_AFTER, RETRY, VehicleReconciler
from TWCManager.Vehicle.Reconcile.Run import Workers
from tests.unit.reconcile_helpers import Clock, ScriptedProvider
from tests.unit.test_gen2twc_provider import VIN, make as make_master

def setup(*providers, clock=True):
    m, s = make_master(); c = Clock(1000.0) if clock else time.time
    m.config = dict(m.config, config=dict(m.config["config"]))          # never the shared CONFIG
    if clock: m.vehicleRegistry._clock = c
    for p in providers: m.providerOrder.register(p)
    m.desired = DesiredStore(clock=c); m.workers = Workers(m, inline=True); m.heldLimits.load()
    m.reconciler = r = VehicleReconciler(m, clock=c); v = m.vehicleRegistry.getOrCreate(VIN, "t"); return m, r, v, c

def ble(*outs): return ScriptedProvider("TeslaBLE", 2, True, list(outs))
def fleet(*outs): return ScriptedProvider("FleetAPI", 5, False, list(outs), tier=C.FLEET_COMMAND)
def cmds(p): return [x[0] for x in p.calls if x[0] != "wake"]
def names(p): return [x[0] for x in p.calls]
def prov(m, name): return m.providerOrder.provider(name)

def test_limit_is_sent_once_and_recorded_as_held():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    assert r.pass_() == 1 and prov(m, "TeslaBLE").calls == [("setChargeLimit", VIN, 50)]
    assert m.heldLimits.get(VIN) == HeldLimit(90, 50) and v.believed("chargeLimit").value == 50
    assert r.pass_() == 0 and m.desired.settled(VIN, CHARGE_LIMIT)                            # satisfied by our own write

def test_default_clocks_share_one_domain():                                  # production clocks, no fake
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE)), clock=False); v.observe("chargeLimit", 50, "TeslaBLE"); v.observe("chargingState", "Stopped", "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy"); m.desired.set(VIN, CHARGING, True, "distributor")
    assert r.pass_() == 1 and m.desired.settled(VIN, CHARGE_LIMIT) and m.desired.get(VIN, CHARGING) is not None
    assert r.pass_() == 0                                                     # inside the 5 s start gap, measured on one clock
    w = v.lastWrite("chargeLimit"); assert abs(w.at - time.time()) < 5 and abs(m.desired.record(VIN, CHARGE_LIMIT).settledAt - w.at) < 5

def test_app_set_limit_is_adopted_and_becomes_outside():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 10; v.observe("chargeLimit", 80, "TeslaMateVehicle")
    assert r.pass_() == 0 and m.desired.get(VIN, CHARGE_LIMIT).value == 80 and m.desired.get(VIN, CHARGE_LIMIT).adopted
    assert m.heldLimits.get(VIN) == HeldLimit(80, None) and len(prov(m, "TeslaBLE").calls) == 1
    for _ in range(3): assert r.pass_() == 0                                  # adopted once: the read is not newer than the adoption
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); assert r.pass_() == 0                     # not undone until a decision
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy", decision=True); assert r.pass_() == 1 and m.heldLimits.get(VIN) == HeldLimit(80, 50)

def test_adopted_float_is_saved_as_an_int():                                 # ledger L26
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 10; v.observe("chargeLimit", 80.0, "HomeAssistant"); assert r.pass_() == 0
    assert m.heldLimits.get(VIN) == HeldLimit(80, None) and type(m.settings["heldLimits"][VIN]["outside"]) is int

def test_a_confirming_read_is_not_adopted_when_desired_changes():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 10; v.observe("chargeLimit", 50, "TeslaMateVehicle")                                 # the car confirms our 50
    c.t += 10; m.desired.set(VIN, CHARGE_LIMIT, 70, "policy", decision=True)
    assert r.pass_() == 1 and m.desired.get(VIN, CHARGE_LIMIT).value == 70 and not m.desired.get(VIN, CHARGE_LIMIT).adopted
    assert cmds(prov(m, "TeslaBLE")) == ["setChargeLimit", "setChargeLimit"]

def test_a_read_not_newer_than_our_write_is_reasserted_not_adopted():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    v.observe("chargeLimit", 90, "TeslaAPI", at=v.lastWrite("chargeLimit").at)                 # stale poll, same instant
    assert r.pass_() == 0 and v.believed("chargeLimit").value == 50 and not m.desired.get(VIN, CHARGE_LIMIT).adopted

def test_no_wake_to_lower_wake_to_raise_never_for_a_restore():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); p = prov(m, "TeslaBLE"); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert names(p) == ["setChargeLimit"]
    c.t += 400; v.observe("chargeLimit", 50, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 70, "policy", decision=True); r.pass_()
    assert names(p)[1:] == ["wake", "setChargeLimit"]
    c.t += 400; v.observe("chargeLimit", 70, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", decision=True, restore=True); r.pass_()
    assert names(p)[3:] == ["setChargeLimit"] and m.heldLimits.get(VIN) == HeldLimit(90, None)   # a restore raises without a wake

def test_fleet_waits_for_ble_to_time_out_and_is_sent_once():                # Review Focus 1, paid counted
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaAPI")
    f = prov(m, "FleetAPI"); b = prov(m, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    r.pass_(); assert len(b.calls) == 1 and f.calls == [] and m.desired.free(VIN).failingSince == 1000.0
    c.t += PAID_AFTER[CHARGE_LIMIT] - 1; r.pass_(); assert f.calls == [] and len(b.calls) == 1
    c.t += 1; assert r.pass_() == 1 and len(f.calls) == 1 and len(b.calls) == 1 and m.heldLimits.get(VIN) == HeldLimit(90, 50)
    for _ in range(12): c.t += 300; r.pass_()
    assert len(f.calls) == 1 and len(b.calls) == 1                           # our stamp settles it: neither BLE nor Fleet again

def test_ble_only_install_keeps_retrying_ble_at_the_retry_cadence():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE))); b = prov(m, "TeslaBLE"); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    for _ in range(4): r.pass_(); c.t += RETRY[CHARGE_LIMIT] - 1; r.pass_(); c.t += 1
    assert len(b.calls) == 4                                                  # no ladder, no give-up, never inside the gap

def test_restore_to_an_absent_car_retries_hourly_and_never_wakes():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE))); b = prov(m, "TeslaBLE"); m.heldLimits.set(VIN, 90, 50)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True); r.pass_()
    assert [x[0] for x in b.calls] == ["setChargeLimit"]; c.t += 3599; r.pass_(); assert len(b.calls) == 1
    c.t += 1; r.pass_(); assert len(b.calls) == 2 and not any(x[0] == "wake" for x in b.calls)

def test_stale_free_failure_gets_the_real_command_over_ble_before_paid():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); b, f = prov(m, "TeslaBLE"), prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.free(VIN).failure(c() - PROBE_AFTER - 300)                     # failing long enough, but untried for > PROBE_AFTER (pre-flight M1)
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert cmds(b) == ["setChargeLimit"] and f.calls == []
    r.pass_(); assert len(cmds(f)) == 1                                      # the fresh failure lets paid go at once

def test_a_recent_free_failure_goes_paid_without_a_new_ble_try():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); b, f = prov(m, "TeslaBLE"), prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.free(VIN).failure(c() - PAID_AFTER[CHARGE_LIMIT])
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); assert r.pass_() == 1 and b.calls == [] and cmds(f) == ["setChargeLimit"]

def test_pairing_error_does_not_open_the_paid_window():                     # ledger L52
    m, r, v, c = setup(ble(O.Outcome(O.ERROR, "pairing")), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += 600; r.pass_()
    assert f.calls == [] and len(prov(m, "TeslaBLE").calls) == 2 and m.desired.free(VIN).failingSince is None

def test_an_open_pairing_window_holds_a_paid_fallback_already_due():       # ledger L52: the channel answers
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE), O.Outcome(O.ERROR, "pairing")), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 90, "TeslaAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert m.desired.free(VIN).failingSince == 1000.0
    c.t += 30; m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()        # BLE answers inside the pairing window
    assert m.desired.record(VIN, CHARGING).lastOutcome == O.Outcome(O.ERROR, "pairing")
    for _ in range(10): c.t += 60; r.pass_()
    assert f.calls == [] and m.desired.free(VIN).failingSince is None

def test_unknown_ble_output_is_a_retryable_failure():                       # ledger L50 (rc 0, unknown output)
    m, r, v, c = setup(ble(O.Outcome(O.ERROR, "unknown"))); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert m.desired.free(VIN).failingSince == c()
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert len(prov(m, "TeslaBLE").calls) == 2

def test_a_free_channel_that_cannot_reach_the_car_still_starts_the_paid_timer():   # carry (d), paid counted
    # A free provider with no read of the limit sends anyway (never a rules
    # UNREACHABLE), so its real failure marks the channel
    free = ScriptedProvider("FreeCloud", 4, False, [O.Outcome(O.UNREACHABLE)], tier=C.CLOUD_FREE)
    m, r, v, c = setup(free, fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    assert free.calls == [("setChargeLimit", VIN, 50)] and m.desired.free(VIN).failingSince == 1000.0 and f.calls == []
    c.t += PAID_AFTER[CHARGE_LIMIT] - 1; v.observe("chargeLimit", 90, "TeslaAPI"); r.pass_(); assert f.calls == []
    c.t += 1; r.pass_(); assert cmds(f) == ["setChargeLimit"] and len(free.calls) == 1
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 1
    a = Attempt(VIN, CHARGE_LIMIT, C.SET_CHARGE_LIMIT, 60, "FreeCloud", False, None, False, c())
    assert r.beforeWrite(v, a, free, None) == (60, None)                     # unknown limit: free sends
    assert r.beforeWrite(v, a._replace(provider="FleetAPI"), f, None)[1].kind == O.UNREACHABLE   # paid never sends blind

def test_failed_paid_call_retries_once_then_waits_for_a_newer_reading_or_a_new_value():   # Review Focus 2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert len(f.calls) == 1
    c.t += RETRY[CHARGE_LIMIT] - 1; r.pass_(); assert len(f.calls) == 1                      # not before the gap
    c.t += 1; r.pass_(); assert len(f.calls) == 2                                            # the one blind retry, fails again
    for _ in range(10): c.t += 600; r.pass_()
    assert len(f.calls) == 2                                                                 # exactly 2 paid calls for this desire
    v.observe("chargeLimit", 90, "TeslaAPI"); c.t += 1; r.pass_(); assert len(f.calls) == 3       # a newer reading disagrees
    m.desired.set(VIN, CHARGE_LIMIT, 60, "policy", decision=True); r.pass_(); assert len(f.calls) == 4
    assert cmds(f) == names(f)                                                               # lowering: never a wake

def test_a_paid_retry_that_succeeds_is_not_repeated():
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"), O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    for _ in range(10): c.t += 600; r.pass_()
    assert len(f.calls) == 2 and v.believed("chargeLimit").value == 50 and m.heldLimits.get(VIN) == HeldLimit(90, 50)

def test_a_refused_or_not_sent_paid_outcome_gets_no_blind_retry():
    m, r, v, c = setup(fleet(O.Outcome(O.REFUSED, "412"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 1 and m.desired.record(VIN, CHARGE_LIMIT).excluded == {"FleetAPI": O.REFUSED}
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy")                           # a raise: wake first, held
    for _ in range(10): r.pass_(); c.t += 600
    rec = m.desired.record(VIN, CHARGE_LIMIT); assert cmds(f) == [] and rec.paidAt is None and not rec.paidRetryDue

def test_a_wake_held_outcome_is_not_counted_as_the_paid_call():
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"), O.Outcome(O.DONE), O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 60, "TeslaAPI"); m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()
    assert names(f) == ["wake"] and m.desired.record(VIN, CHARGE_LIMIT).paidAt is None
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["wake", "wake", "setChargeLimit"]   # the desire's first paid call
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["wake", "wake", "setChargeLimit", "setChargeLimit"]   # its one blind retry, no wake
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 2

def test_a_not_sent_outcome_is_not_replanned_inside_retry():                # pre-flight H2
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(10): r.pass_()
    assert names(f) == ["wake"]; c.t += RETRY[CHARGING]; r.pass_(); assert names(f) == ["wake", "wake"]

def test_no_resubmit_loop_during_a_30_minute_wake_hold():                   # pre-flight H2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat"); submitted = 0
    for _ in range(1800): c.t += 1; submitted += r.pass_()
    assert submitted == 30 and names(f) == ["wake"] * 30 and cmds(f) == []      # one per RETRY[CHARGING]; 0 paid requests

def test_paid_retry_never_wakes_and_a_failed_wake_gets_no_blind_retry():   # pre-flight M8, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE), O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()                                 # a raise: wake + command
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["wake", "setChargeLimit", "setChargeLimit"]   # retry: no wake
    for _ in range(10): c.t += 600; r.pass_()
    assert len(f.calls) == 3
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "asleep"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()                                 # the wake itself fails: "wake asleep"
    for _ in range(10): c.t += 600; r.pass_()
    assert names(f) == ["wake"] and m.desired.record(VIN, CHARGE_LIMIT).lastOutcome.detail == "wake asleep"

def test_a_scheduled_charge_is_left_alone():                                # pre-flight H3, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "TeslaAPI")
    v.observe("scheduledChargingPending", True, "TeslaAPI"); m.desired.set(VIN, CHARGING, True, "heartbeat")
    assert r.pass_() == 0 and f.calls == []
    m.config["config"]["respectVehicleSchedule"] = False; assert r.pass_() == 1 and names(f) == ["wake", "startCharging"]

def test_a_start_to_a_car_that_cannot_charge_is_left_alone():
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Disconnected", "TeslaAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat"); assert r.pass_() == 0 and f.calls == []

def test_a_refused_fleet_wake_does_not_exclude_fleet_for_the_car():        # Task 8, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake refused"), O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()
    assert names(f) == ["wake"] and m.desired.record(VIN, CHARGE_LIMIT).excluded == {}
    c.t += RETRY[CHARGE_LIMIT]; m.desired.set(VIN, CHARGE_LIMIT, 55, "policy", decision=True); r.pass_()
    assert names(f) == ["wake", "setChargeLimit"]                             # a lower needs no wake: Fleet still commands

def test_refused_excludes_the_provider_and_unsupported_falls_through():
    m, r, v, c = setup(ble(O.Outcome(O.REFUSED, "key")), fleet(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.excluded == {"TeslaBLE": O.REFUSED} and m.desired.free(VIN).failingSince == 1000.0
    c.t += PAID_AFTER[CHARGE_LIMIT] + 1; r.pass_(); assert len(prov(m, "FleetAPI").calls) == 1
    m, r, v, c = setup(ble(O.Outcome(O.UNSUPPORTED, "no claim")), fleet(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    assert m.desired.record(VIN, CHARGE_LIMIT).excluded == {"TeslaBLE": O.UNSUPPORTED} and m.desired.free(VIN).failingSince is None
    r.pass_(); assert len(prov(m, "FleetAPI").calls) == 1                    # no free candidate left: paid at once

def test_restore_with_telemetry_showing_a_limit_not_ours_forces_a_live_read():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 80, "TeslaMateVehicle")
    m.vehicleRegistry.registerLiveness("TeslaMateVehicle", lambda vehicle=None: c())            # a healthy feed: age 0 without force
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True)
    attempt = r.plan(v, CHARGE_LIMIT, c()); assert attempt is not None and attempt.force and attempt.value == 90 and not attempt.wake

def test_restore_with_nothing_held_clears_the_desire_and_unknown_limit_on_cloud_reads_first():
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True)
    assert r.pass_() == 0 and m.desired.get(VIN, CHARGE_LIMIT) is None                           # nothing held: settled at plan time, no loop
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy", decision=True); assert r.pass_() == 1 and prov(m, "FleetAPI").calls == []   # read first; nothing known: nothing sent
    rec = m.desired.record(VIN, CHARGE_LIMIT); assert rec.lastOutcome.kind == O.UNREACHABLE and rec.paidAt is None

def test_a_rules_settled_restore_clears_the_desire_and_does_not_loop():
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), clock=False); m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 80, "TeslaBLE")   # hand-set before a restart: no write stamp
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True)
    assert r.pass_() == 1 and prov(m, "TeslaBLE").calls == [] and m.desired.get(VIN, CHARGE_LIMIT) is None
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)
    for _ in range(5): assert r.pass_() == 0                                                   # real clock, no busy loop

def test_a_satisfied_outcome_without_a_stamp_is_not_resent_until_new_evidence():   # N1, paid repeat counted
    m, r, v, c = setup(fleet(O.Outcome(O.SATISFIED, "not_charging"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Charging", "TeslaAPI")
    m.desired.set(VIN, CHARGING, False, "distributor"); r.pass_()
    for _ in range(5): r.pass_()
    assert names(f) == ["stopCharging"] and v.lastWrite("chargingState") is None                    # settled, unstamped, not repeated, never woken
    c.t += 10; v.observe("chargingState", "Charging", "TeslaAPI"); assert r.pass_() == 0               # new evidence inside RETRY: still held
    c.t += RETRY[CHARGING]; assert r.pass_() == 1 and names(f) == ["stopCharging"] * 2                 # new evidence past RETRY: one more

def test_rate_limited_provider_is_skipped_at_plan_time(monkeypatch):        # ledger Task 5 M6
    mono = Clock(); monkeypatch.setattr(Run, "_monotonic", mono)
    f = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)], caps={C.SET_CHARGE_LIMIT: C.Declaration(C.FLEET_COMMAND, C.RateLimit(1, 60))})
    m, r, v, c = setup(f); v.observe("chargeLimit", 90, "TeslaAPI"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); assert r.pass_() == 1
    seen = []; allows = m.workers.allows; m.workers.allows = lambda *a: (seen.append(a[0].name), allows(*a))[1]
    m.desired.set(VIN, CHARGE_LIMIT, 60, "policy", decision=True); assert r.pass_() == 0 and len(f.calls) == 1 and seen == ["FleetAPI"]
    mono.t += 61; assert r.pass_() == 1 and len(f.calls) == 2

def test_a_late_report_for_a_replaced_attempt_does_not_clear_a_newer_one():   # ledger L16
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    newer = r.plan(v, CHARGE_LIMIT, c()); old = newer._replace(value=40)
    r.report(old, O.Outcome(O.ERROR, "replaced")); r.report(old, O.Outcome(O.DONE))
    rec = m.desired.record(VIN, CHARGE_LIMIT); assert rec.inFlight is newer and rec.lastOutcome is None

def test_a_debug_one_shot_report_is_ignored():                              # pre-flight L2
    m, r, v, c = setup(fleet(O.Outcome(O.DONE)))
    r.report(Attempt(VIN, None, C.WAKE, None, "FleetAPI", False, None, False, c()), O.Outcome(O.DONE)); assert m.desired.peek(VIN) is None

def test_pass_is_bounded_and_survives_a_raising_provider():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); p = prov(m, "TeslaBLE")
    def boom(vin, limit): raise RuntimeError("x")
    p.setChargeLimit = boom; m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    assert r.pass_() == 1 and m.desired.record(VIN, CHARGE_LIMIT).lastOutcome.kind == O.ERROR and r.pass_() == 0

def test_a_submit_that_raises_or_is_refused_never_leaves_the_aspect_in_flight():   # pre-flight L10
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    def boom(attempt): raise RuntimeError("x")
    m.workers.submit = boom; assert r.pass_() == 0 and m.desired.record(VIN, CHARGE_LIMIT).inFlight is None
    m.workers.submit = lambda attempt: False; assert r.pass_() == 0 and m.desired.record(VIN, CHARGE_LIMIT).inFlight is None

def test_the_thread_runs_a_pass_on_a_change_and_exits_on_stop():
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), clock=False); v.observe("chargeLimit", 90, "TeslaBLE"); b = prov(m, "TeslaBLE")
    r.start(); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    deadline = time.time() + 5
    while not b.calls and time.time() < deadline: time.sleep(0.01)
    r.stop(); r.join(3); assert b.calls == [("setChargeLimit", VIN, 50)] and not r.is_alive() and r.name == "VehicleReconciler" and r.daemon
