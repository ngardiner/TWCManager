import time
from unittest.mock import patch
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import RELEASE, AMPS, CHARGE_LIMIT, CHARGING, Attempt, DesiredStore
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from TWCManager.Vehicle.Reconcile.Reconciler import MIN_SEND_GAP, PAID_AFTER, PROBE_AFTER, RETRY, VehicleReconciler
from tests.unit.reconcile_helpers import Clock, placeAt, ScriptedProvider, inline, settled
from tests.unit.test_gen2twc_provider import VIN, make as make_master

def setup(*providers, clock=True, here=True, battery=60, ble=False):
    # ble: the real TeslaBLE module and its provider; tests stub what it runs
    # here: the car is plugged in at home, so every message applies to it
    m, s = make_master(); c = Clock(1000.0) if clock else time.time
    m.config = dict(m.config, config=dict(m.config["config"]))          # never the shared CONFIG
    if clock: m.vehicleRegistry._clock = c
    for p in providers: m.providerOrder.register(p)
    m.desired = DesiredStore(clock=c); m.vehicleRegistry.removed = m.desired.forget; m.heldLimits.load()
    m.reconciler = r = inline(VehicleReconciler(m, clock=c)); v = m.vehicleRegistry.getOrCreate(VIN, "t")
    if ble:
        m.config["config"]["teslaControlPath"] = "/bin/true"
        with patch.object(TeslaBLE, "_start_dbus_session"), patch.object(TeslaBLE, "_cleanup_stale_pipe"): b = TeslaBLE(m)
        b.isDockerCached = False
        for p in b.providers(): m.providerOrder.register(p)
    if here:
        v.observe("pluggedIn", True, "Gen3TWC")
        if battery:
            v.observe("batteryLevel", battery, "TeslaMateVehicle")
    return m, r, v, c

def leave(v):                                                                # unplugged after the plug-in: a restore applies
    v.observe("pluggedIn", False, "Gen3TWC", at=v.markAt("plugIn") + 1)

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
    assert r.pass_() == 0 and settled(m.desired, VIN, CHARGE_LIMIT)                            # satisfied by our own write

def test_default_clocks_share_one_domain():                                  # production clocks, no fake
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE)), clock=False); v.observe("chargeLimit", 50, "TeslaBLE"); v.observe("chargingState", "Stopped", "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy"); m.desired.set(VIN, CHARGING, True, "distributor")
    assert r.pass_() == 1 and settled(m.desired, VIN, CHARGE_LIMIT) and m.desired.get(VIN, CHARGING) is not None
    assert r.pass_() == 0                                                     # inside the 5 s start gap, measured on one clock
    w = v.lastWrite("chargeLimit"); assert abs(w.at - time.time()) < 5 and abs(m.desired.record(VIN, CHARGE_LIMIT).settledAt - w.at) < 5

def test_app_set_limit_is_adopted_and_becomes_outside():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 10; v.observe("chargeLimit", 80, "TeslaMateVehicle")
    assert r.pass_() == 0 and m.desired.get(VIN, CHARGE_LIMIT).value == 80 and m.desired.get(VIN, CHARGE_LIMIT).adopted
    assert m.heldLimits.get(VIN) is None and len(prov(m, "TeslaBLE").calls) == 1   # adopted: nothing held, no entry
    for _ in range(3): assert r.pass_() == 0                                  # adopted once: the read is not newer than the adoption
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); assert r.pass_() == 0                     # not undone until a decision
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy", decision=True); assert r.pass_() == 1 and m.heldLimits.get(VIN) == HeldLimit(80, 50)

def test_adopted_float_is_saved_as_an_int():                                 # ledger L26
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 10; v.observe("chargeLimit", 80.0, "HomeAssistant"); assert r.pass_() == 0
    assert m.heldLimits.get(VIN) is None and VIN not in m.settings["chargeLimits"]   # adopted: nothing held
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy", decision=True); r.pass_()
    assert m.settings["chargeLimits"][VIN] == [80, 50] and type(m.settings["chargeLimits"][VIN][0]) is int

def test_a_confirming_read_is_not_adopted_when_desired_changes():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 10; v.observe("chargeLimit", 50, "TeslaMateVehicle")                                 # the car confirms our 50
    c.t += 10; m.desired.set(VIN, CHARGE_LIMIT, 70, "policy", decision=True)
    assert r.pass_() == 1 and m.desired.get(VIN, CHARGE_LIMIT).value == 70 and not m.desired.get(VIN, CHARGE_LIMIT).adopted
    assert cmds(prov(m, "TeslaBLE")) == ["setChargeLimit", "setChargeLimit"]

def test_a_read_not_newer_than_our_write_is_reasserted_not_adopted():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    v.observe("chargeLimit", 90, "FleetAPI", at=v.lastWrite("chargeLimit").at)                 # stale poll, same instant
    assert r.pass_() == 0 and v.believed("chargeLimit").value == 50 and not m.desired.get(VIN, CHARGE_LIMIT).adopted

def test_no_wake_to_lower_wake_to_raise_never_for_a_restore():
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); p = prov(m, "TeslaBLE"); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert names(p) == ["setChargeLimit"]
    c.t += 400; v.observe("chargeLimit", 50, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 70, "policy", decision=True); r.pass_()
    assert names(p)[1:] == ["wake", "setChargeLimit"]
    c.t += 400; v.observe("chargeLimit", 70, "TeslaBLE"); m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", decision=True, restore=True); r.pass_()
    assert names(p)[3:] == ["setChargeLimit"] and m.heldLimits.get(VIN) is None   # a restore raises without a wake
    assert VIN not in m.settings["chargeLimits"]                             # restored: nothing held, no entry

def test_fleet_waits_for_ble_to_time_out_and_is_sent_once():                # Review Focus 1, paid counted
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "FleetAPI")
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
    leave(v)
    v.observe("chargeLimit", 50, "TeslaMateVehicle")                            # the last known limit, from another channel
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True); r.pass_()
    assert [x[0] for x in b.calls] == ["setChargeLimit"]; c.t += 3599; r.pass_(); assert len(b.calls) == 1
    c.t += 1; r.pass_(); assert len(b.calls) == 2 and not any(x[0] == "wake" for x in b.calls)

def test_a_restore_falls_to_fleet_without_a_wake_and_at_most_twice():    # pre-flight L4, paid counted
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.UNREACHABLE, "timeout"))); f = prov(m, "FleetAPI")
    m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 50, "FleetAPI")
    leave(v)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True)
    r.pass_(); assert f.calls == []
    c.t += PAID_AFTER[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["setChargeLimit"]
    for _ in range(40): c.t += 300; r.pass_()
    assert names(f) == ["setChargeLimit"] * 2 and not any(x[0] == "wake" for x in prov(m, "TeslaBLE").calls)   # never a wake; one blind retry

def test_stale_free_failure_gets_the_real_command_over_ble_before_paid():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); b, f = prov(m, "TeslaBLE"), prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    c.t += PROBE_AFTER + 300                                                  # failing since the plug-in
    m.desired.free(VIN).failure(c() - PROBE_AFTER - 300)                     # failing long enough, but untried for > PROBE_AFTER (pre-flight M1)
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert cmds(b) == ["setChargeLimit"] and f.calls == []
    r.pass_(); assert f.calls == []                                           # never two sends inside the gap
    c.t += MIN_SEND_GAP[CHARGE_LIMIT]; r.pass_(); assert len(cmds(f)) == 1    # the fresh failure lets paid go at once

def test_a_recent_free_failure_goes_paid_without_a_new_ble_try():
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); b, f = prov(m, "TeslaBLE"), prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    c.t += PAID_AFTER[CHARGE_LIMIT]; m.desired.free(VIN).failure(c() - PAID_AFTER[CHARGE_LIMIT])  # failing since the plug-in
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); assert r.pass_() == 1 and b.calls == [] and cmds(f) == ["setChargeLimit"]

def test_pairing_error_does_not_open_the_paid_window():                     # ledger L52
    m, r, v, c = setup(ble(O.Outcome(O.ERROR, "pairing")), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += 600; r.pass_()
    assert f.calls == [] and len(prov(m, "TeslaBLE").calls) == 2 and m.desired.free(VIN).failingSince is None

def test_an_open_pairing_window_holds_a_paid_fallback_already_due():       # ledger L52: the channel answers
    m, r, v, c = setup(ble(O.Outcome(O.ERROR, "pairing")), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("chargingState", "Stopped", "FleetAPI"); m.desired.free(VIN).failure(c())                 # BLE failed a moment ago
    c.t += 30; m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()        # BLE answers inside the pairing window
    assert m.desired.record(VIN, CHARGING).lastOutcome == O.Outcome(O.ERROR, "pairing")
    for _ in range(10): c.t += 60; r.pass_()
    assert f.calls == [] and m.desired.free(VIN).failingSince is None

def test_unknown_ble_output_is_a_retryable_failure():                       # ledger L50 (rc 0, unknown output)
    m, r, v, c = setup(ble(O.Outcome(O.ERROR, "unknown"))); v.observe("chargeLimit", 90, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert m.desired.free(VIN).failingSince == c()
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert len(prov(m, "TeslaBLE").calls) == 2

def test_a_free_channel_that_cannot_reach_the_car_still_starts_the_paid_timer():   # carry (d), paid counted
    # A free provider sends with the limit known from another channel, so its
    # real failure marks the channel
    free = ScriptedProvider("FreeCloud", 4, False, [O.Outcome(O.UNREACHABLE)], tier=C.CLOUD_FREE)
    m, r, v, c = setup(free, fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    assert free.calls == [("setChargeLimit", VIN, 50)] and m.desired.free(VIN).failingSince == 1000.0 and f.calls == []
    c.t += PAID_AFTER[CHARGE_LIMIT] - 1; v.observe("chargeLimit", 90, "FleetAPI"); r.pass_(); assert f.calls == []
    c.t += 1; r.pass_(); assert cmds(f) == ["setChargeLimit"] and len(free.calls) == 1
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 1
    a = Attempt(VIN, CHARGE_LIMIT, C.SET_CHARGE_LIMIT, 60, "FreeCloud", False, None, False, c())
    assert r.beforeWrite(v, a, free, None)[1].kind == O.UNREACHABLE         # unknown limit: free never sends blind
    assert r.beforeWrite(v, a._replace(provider="FleetAPI"), f, None)[1].kind == O.UNREACHABLE   # nor paid

def test_failed_paid_call_retries_once_then_waits_for_a_newer_reading_or_a_new_value():   # Review Focus 2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); assert len(f.calls) == 1
    c.t += RETRY[CHARGE_LIMIT] - 1; r.pass_(); assert len(f.calls) == 1                      # not before the gap
    c.t += 1; r.pass_(); assert len(f.calls) == 2                                            # the one blind retry, fails again
    for _ in range(10): c.t += 600; r.pass_()
    assert len(f.calls) == 2                                                                 # exactly 2 paid calls for this desire
    v.observe("chargeLimit", 85, "FleetAPI"); c.t += 600; r.pass_(); assert len(f.calls) == 2     # the cap holds even on a new reading (I2)
    m.desired.set(VIN, CHARGE_LIMIT, 60, "policy", decision=True); r.pass_(); assert len(f.calls) == 3
    assert cmds(f) == names(f)                                                               # lowering: never a wake

def test_a_paid_retry_that_succeeds_is_not_repeated():
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"), O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    for _ in range(10): c.t += 600; r.pass_()
    assert len(f.calls) == 2 and v.believed("chargeLimit").value == 50 and m.heldLimits.get(VIN) == HeldLimit(90, 50)

def test_a_refused_or_not_sent_paid_outcome_gets_no_blind_retry():
    m, r, v, c = setup(fleet(O.Outcome(O.REFUSED, "412"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 1 and m.desired.record(VIN, CHARGE_LIMIT).excluded == {"FleetAPI": O.REFUSED}
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy")                           # a raise: wake first, held
    for _ in range(10): r.pass_(); c.t += 600
    rec = m.desired.record(VIN, CHARGE_LIMIT); assert cmds(f) == [] and rec.paidAt is None and not rec.paidRetryDue

def test_a_wake_held_outcome_is_not_counted_as_the_paid_call():
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"), O.Outcome(O.DONE), O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 60, "FleetAPI"); m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()
    assert names(f) == ["wake"] and m.desired.record(VIN, CHARGE_LIMIT).paidAt is None
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["wake", "wake", "setChargeLimit"]   # the desire's first paid call
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["wake", "wake", "setChargeLimit", "setChargeLimit"]   # its one blind retry, no wake
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 2

def test_a_not_sent_outcome_is_not_replanned_inside_retry():                # pre-flight H2
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(10): r.pass_()
    assert names(f) == ["wake"]; c.t += RETRY[CHARGING]; r.pass_(); assert names(f) == ["wake", "wake"]

def test_a_not_sent_refusal_lets_the_next_paid_provider_go_at_once():     # H2 with an excluding outcome
    for detail in ("signing required", "owner commands retired", "no command token"):
        first = fleet(O.Outcome(O.REFUSED, detail))
        last = ScriptedProvider("Fallback", 9, False, [O.Outcome(O.DONE)], tier=C.HA_BILLED)
        m, r, v, c = setup(first, last); v.observe("chargeLimit", 60, "FleetAPI")
        m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_(); r.pass_()
        assert cmds(prov(m, "FleetAPI")) == [] and cmds(prov(m, "Fallback")) == ["setChargeLimit"], detail
        assert m.desired.record(VIN, CHARGE_LIMIT).paidSends == 1             # the refusal spent none

def test_no_resubmit_loop_during_a_30_minute_wake_hold():                   # pre-flight H2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat"); submitted = 0
    for _ in range(1800): c.t += 1; submitted += r.pass_()
    assert submitted == 30 and names(f) == ["wake"] * 30 and cmds(f) == []      # one per RETRY[CHARGING]; 0 paid requests

def test_paid_retry_never_wakes_and_a_failed_wake_gets_no_blind_retry():   # pre-flight M8, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE), O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()                                 # a raise: wake + command
    c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert names(f) == ["wake", "setChargeLimit", "setChargeLimit"]   # retry: no wake
    for _ in range(10): c.t += 600; r.pass_()
    assert len(f.calls) == 3
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "asleep"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()                                 # the wake itself fails: "wake asleep"
    for _ in range(10): c.t += 600; r.pass_()
    # No blind command: each arm's follow-up wakes again, and a spent arm re-arms once (the real Fleet
    # provider holds each wake 30 min, a free online check until then: parity row 4b)
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert names(f) == ["wake"] * 4 and rec.lastOutcome.detail == "wake asleep"
    assert rec.paidSends == 2 and rec.rearmed                                                # spent: nothing more without a reading

def test_a_scheduled_charge_is_left_alone():                                # pre-flight H3, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    v.observe("scheduledChargingPending", True, "FleetAPI"); m.desired.set(VIN, CHARGING, True, "heartbeat")
    assert r.pass_() == 0 and f.calls == []
    m.config["config"]["respectVehicleSchedule"] = False; assert r.pass_() == 1 and names(f) == ["wake", "startCharging"]

def test_a_start_to_a_car_that_cannot_charge_is_left_alone():
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Disconnected", "FleetAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat"); assert r.pass_() == 0 and f.calls == []

def test_a_refused_fleet_wake_does_not_exclude_fleet_for_the_car():        # Task 8, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake refused"), O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); r.pass_()
    assert names(f) == ["wake"] and m.desired.record(VIN, CHARGE_LIMIT).excluded == {}
    c.t += 1; m.desired.set(VIN, CHARGE_LIMIT, 55, "policy", decision=True); r.pass_()
    assert names(f) == ["wake", "setChargeLimit"]                             # a lower needs no wake: Fleet still commands

def test_refused_excludes_the_provider_and_unsupported_falls_through():
    m, r, v, c = setup(ble(O.Outcome(O.REFUSED, "key")), fleet(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert rec.excluded == {"TeslaBLE": O.REFUSED} and m.desired.free(VIN).failingSince == 1000.0
    c.t += PAID_AFTER[CHARGE_LIMIT] + 1; r.pass_(); assert len(prov(m, "FleetAPI").calls) == 1
    m, r, v, c = setup(ble(O.Outcome(O.UNSUPPORTED, "no claim")), fleet(O.Outcome(O.DONE))); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    assert m.desired.record(VIN, CHARGE_LIMIT).excluded == {"TeslaBLE": O.UNSUPPORTED} and m.desired.free(VIN).failingSince is None
    r.pass_(); assert len(prov(m, "FleetAPI").calls) == 1                    # no free candidate left: paid at once (no gap)

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
    leave(v)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True)
    assert r.pass_() == 1 and prov(m, "TeslaBLE").calls == [] and m.desired.get(VIN, CHARGE_LIMIT) is None
    assert m.heldLimits.get(VIN) is None                                      # hand-set limit stands: nothing held
    for _ in range(5): assert r.pass_() == 0                                                   # real clock, no busy loop

def test_a_satisfied_outcome_without_a_stamp_is_not_resent_until_new_evidence():   # N1, paid repeat counted
    m, r, v, c = setup(fleet(O.Outcome(O.SATISFIED, "not_charging"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Charging", "FleetAPI")
    m.desired.set(VIN, CHARGING, False, "distributor"); r.pass_()
    for _ in range(5): r.pass_()
    assert names(f) == ["stopCharging"] and v.lastWrite("chargingState") is None                    # settled, unstamped, not repeated, never woken
    c.t += 10; v.observe("chargingState", "Starting", "FleetAPI"); assert r.pass_() == 0               # new evidence inside RETRY: still held
    c.t += RETRY[CHARGING]; assert r.pass_() == 1 and names(f) == ["stopCharging"] * 2                 # new evidence past RETRY: one more
    for _ in range(10): c.t += 600; v.observe("chargingState", "Charging", "FleetAPI"); r.pass_()
    assert names(f) == ["stopCharging"] * 2                                                           # and never a third (I2)

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
    r.submit = boom; assert r.pass_() == 0 and m.desired.record(VIN, CHARGE_LIMIT).inFlight is None
    r.submit = lambda attempt: False; assert r.pass_() == 0 and m.desired.record(VIN, CHARGE_LIMIT).inFlight is None

def test_the_thread_runs_a_pass_on_a_change_and_exits_on_stop():
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), clock=False); v.observe("chargeLimit", 90, "TeslaBLE"); b = prov(m, "TeslaBLE")
    r.start(); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    deadline = time.time() + 5
    while not b.calls and time.time() < deadline: time.sleep(0.01)
    r.stop(); r.join(3); assert b.calls == [("setChargeLimit", VIN, 50)] and not r.is_alive() and r.name == "VehicleReconciler" and r.daemon

# Fix round 1

def test_a_stop_set_while_a_start_is_in_flight_is_sent_on_the_next_pass():   # C1, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    start = f.startCharging
    def midFlight(vin): c.t += 1; m.desired.set(VIN, CHARGING, False, "distributor"); c.t += 2; return start(vin)
    f.startCharging = midFlight; m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()
    rec = m.desired.record(VIN, CHARGING); assert rec.settledAt is None and rec.paidAt is None and rec.inFlight is None
    assert r.pass_() == 0 and names(f) == ["wake", "startCharging"]          # the late start still spaces the stop (I5)
    c.t += MIN_SEND_GAP[CHARGING]; assert r.pass_() == 1 and names(f) == ["wake", "startCharging", "stopCharging"]
    for _ in range(10): c.t += 600; r.pass_()
    assert len(cmds(f)) == 2

def test_a_limit_changed_while_in_flight_sends_the_new_value():             # C1
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    send = f.setChargeLimit
    def midFlight(vin, limit):
        if limit == 50: m.desired.set(VIN, CHARGE_LIMIT, 60, "policy", decision=True)
        return send(vin, limit)
    f.setChargeLimit = midFlight; m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += MIN_SEND_GAP[CHARGE_LIMIT]
    assert not settled(m.desired, VIN, CHARGE_LIMIT) and r.pass_() == 1 and [x[2] for x in f.calls if x[0] != "wake"] == [50, 60]
    assert settled(m.desired, VIN, CHARGE_LIMIT) and m.heldLimits.get(VIN) == HeldLimit(90, 60)

def test_a_stop_set_after_a_start_s_wake_held_goes_on_the_first_pass():    # I1
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "wake held"), O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_(); assert names(f) == ["wake"]
    c.t += 5; v.observe("chargingState", "Charging", "TeslaMateVehicle"); m.desired.set(VIN, CHARGING, False, "distributor")
    assert r.pass_() == 1 and names(f) == ["wake", "stopCharging"]

def test_a_failing_paid_call_under_a_poll_pays_at_most_twice_an_hour():   # I2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    for _ in range(60):
        v.observe("chargeLimit", 90, "FleetAPI")                              # a poll every minute
        for _ in range(6): c.t += 10; r.pass_()
    assert len(cmds(f)) == 2

def test_a_satisfied_stop_with_a_stale_feed_pays_once_an_hour():          # I2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.SATISFIED, "not_charging"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Charging", "FleetAPI")
    m.desired.set(VIN, CHARGING, False, "distributor")
    for _ in range(60):
        v.observe("chargingState", "Charging", "TeslaMateVehicle")           # the feed still says Charging
        for _ in range(6): c.t += 10; r.pass_()
    assert cmds(f) == ["stopCharging"]

def test_the_blind_retry_and_a_fallback_buy_no_paid_read():                 # I3, paid reads counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); src = f.reads(90, cost=C.FLEET_DATA)
    v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    for _ in range(12 * 360): c.t += 10; r.pass_()
    assert len(cmds(f)) == 2 and src.fetched == 0                            # 500 then 500: 2 commands, no vehicle_data
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI"); src = f.reads(90, cost=C.FLEET_DATA)
    v.observe("chargeLimit", 90, "FleetAPI")
    b = prov(m, "TeslaBLE"); fail = b.setChargeLimit
    def slow(vin, limit): c.t += 20; return fail(vin, limit)                  # BLE takes 20 s to fail
    b.setChargeLimit = slow; m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    for _ in range(360): c.t += 10; r.pass_()
    assert cmds(f) == ["setChargeLimit"] and src.fetched == 0                # the fallback reads nothing: 320 s is fresh enough

# Fix round 2

def test_paid_sends_that_never_reached_the_car_rearm_on_a_reading_after_it_wakes():   # N1 ruling 1, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert len(cmds(f)) == 2
    for _ in range(60):                                                       # asleep an hour: only the TWC heartbeat reads
        v.observe("actualCurrent", 0, "Gen2TWC")
        for _ in range(6): c.t += 10; r.pass_()
    assert len(cmds(f)) == 2
    f.outcomes = [O.Outcome(O.UNREACHABLE, "408")]; v.observe("chargeLimit", 90, "FleetAPI"); c.t += 1; r.pass_()
    assert len(cmds(f)) == 3                                                  # a reading of the same value: the car never got it
    for _ in range(36): c.t += 100; r.pass_()
    assert len(cmds(f)) == 4                                                  # at most 2 per re-arm
    f.outcomes = [O.Outcome(O.DONE)]
    for _ in range(36): c.t += 100; v.observe("chargeLimit", 90, "FleetAPI"); r.pass_()
    assert len(cmds(f)) == 4                                                  # one re-arm per desire (I2)
    m.desired.set(VIN, CHARGE_LIMIT, 55, "policy", decision=True); r.pass_()
    assert len(cmds(f)) == 5 and v.believed("chargeLimit").value == 55        # a new value is a new desire

def test_a_start_wakes_the_car_for_the_limit_first():                      # N1 ruling 2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI")
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    v.observe("chargeLimit", 90, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    c.t += 7200; f.outcomes = [O.Outcome(O.DONE)]; m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(30): r.pass_(); c.t += 1
    assert names(f) == ["wake", "wake", "wake", "setChargeLimit", "wake", "startCharging"]   # a charging car's lower wakes: the wakes fail asleep, the limit waits
    assert v.lastWrite("chargeLimit").at < v.lastWrite("chargingState").at   # never charges on the old limit

def test_a_start_waits_for_an_unsettled_limit():                           # N1 ruling 2
    m, r, v, c = setup(ble(O.Outcome(O.UNREACHABLE)), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    v.observe("chargeLimit", 90, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()
    for _ in range(int(PAID_AFTER[CHARGE_LIMIT]) - 1): c.t += 1; r.pass_()
    assert f.calls == []                                                      # BLE down: no Fleet start at +60 ahead of the limit
    for _ in range(30): c.t += 1; r.pass_()
    assert names(f) == ["wake", "setChargeLimit", "wake", "startCharging"]   # the limit carries the start's wake

def test_a_limit_no_provider_can_deliver_never_holds_a_start():
    m, r, v, c = setup(ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)], caps={C.START_CHARGING: C.FLEET_COMMAND, C.WAKE: C.FLEET_WAKE}))
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); m.desired.set(VIN, CHARGING, True, "heartbeat")
    assert r.pass_() == 1 and names(f) == ["wake", "startCharging"]

def test_a_desire_set_during_a_pass_still_counts_its_paid_sends():         # N2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.ERROR, "500"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(now=c() - 0.01)
    for _ in range(12 * 36): c.t += 100; r.pass_()
    assert len(cmds(f)) == 2


# Task 10: one minimum send gap; only a reading that reached the car re-arms (c2); a refused limit is spent (c4)

def ha(*outs, caps=(C.SET_CHARGE_LIMIT, C.START_CHARGING, C.STOP_CHARGING)):
    # Billed, never wakes
    return ScriptedProvider("HomeAssistant", 6, False, list(outs), caps={cap: C.HA_BILLED for cap in caps})

def calls_named(p, name): return [x for x in p.calls if x[0] == name]

def test_min_send_gap_spaces_a_start_stop_flip_on_any_provider():          # replaces HA's 10 s gate
    for provider in (ha(O.Outcome(O.DONE)), ble(O.Outcome(O.DONE))):
        m, r, v, c = setup(provider); p = prov(m, provider.name); v.observe("chargingState", "Stopped", "FleetAPI")
        m.desired.set(VIN, CHARGING, True, "heartbeat"); assert r.pass_() == 1; m.desired.set(VIN, CHARGING, False, "heartbeat")
        c.t += 9; assert r.pass_() == 0; c.t += 1; assert r.pass_() == 1
        assert cmds(p) == ["startCharging", "stopCharging"]

def test_min_send_gap_holds_when_the_flip_lands_while_the_start_is_in_flight():   # I5
    m, r, v, c = setup(ha(O.Outcome(O.DONE))); h = prov(m, "HomeAssistant"); v.observe("chargingState", "Stopped", "FleetAPI")
    start = h.startCharging
    def midFlight(vin): m.desired.set(VIN, CHARGING, False, "distributor"); return start(vin)
    h.startCharging = midFlight; m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()
    assert m.desired.record(VIN, CHARGING).lastSentAt == c()                  # the replaced desire's send still stamps
    c.t += 9; assert r.pass_() == 0 and cmds(h) == ["startCharging"]
    c.t += 1; assert r.pass_() == 1 and cmds(h) == ["startCharging", "stopCharging"]

def test_amps_gap_is_fifteen_seconds():                                      # I6
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); b = prov(m, "TeslaBLE")
    m.desired.set(VIN, AMPS, 10, "distributor"); assert r.pass_() == 1
    m.desired.set(VIN, AMPS, 12, "distributor"); c.t += 14; assert r.pass_() == 0
    c.t += 1; assert r.pass_() == 1 and [x[2] for x in b.calls if x[0] == "setAmps"] == [10, 12]

def test_ha_republishing_the_limit_every_minute_does_not_rearm_paid_sends():   # c2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    for _ in range(60): c.t += 60; v.observe("chargeLimit", 90, "HomeAssistant"); r.pass_()
    assert len(cmds(f)) <= 2                                                 # 1 h of republishing: at most one send + one retry
    v.observe("chargeLimit", 90, "FleetAPI"); c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert len(cmds(f)) == 3   # a live read re-arms
    assert f.calls == calls_named(f, "setChargeLimit")                       # a lower: never a wake

def test_a_live_read_followed_by_an_ha_republish_still_rearms():            # I3, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_(); assert len(cmds(f)) == 2
    c.t += 600; v.observe("chargeLimit", 90, "FleetAPI"); c.t += 5; v.observe("chargeLimit", 90, "HomeAssistant")
    r.pass_(); assert len(cmds(f)) == 3

def test_a_changed_value_from_a_republishing_source_rearms():              # c2: a new value means the car answered
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    c.t += 600; v.observe("chargeLimit", 80, "TeslaMateVehicle"); r.pass_()
    assert m.desired.get(VIN, CHARGE_LIMIT).value == 50 and len(cmds(f)) == 3

def test_streaming_telemetry_with_fleet_408_is_bounded_per_desire():        # I2, user decision U2, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 90, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy")
    for _ in range(120): c.t += 30; v.observe("chargeLimit", 90, "FleetTelemetryMQTT"); r.pass_()
    assert len(cmds(f)) <= 4 and len(calls_named(f, "wake")) <= 1            # at most $0.004 per desire, not per RETRY

def test_streaming_telemetry_with_fleet_408_on_a_start_is_bounded_per_desire():   # I2 for start: wakes counted too
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(120): c.t += 30; v.observe("chargingState", "Stopped", "FleetTelemetryMQTT"); r.pass_()
    assert len(f.calls) <= 4                                                 # each failed wake is the attempt's one paid call

def test_lowered_limit_reaches_a_car_that_wakes_to_charge_on_teslamate():  # I4, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"), O.Outcome(O.UNREACHABLE, "408"), O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 90, "FleetAPI"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    assert len(cmds(f)) == 2
    c.t += 3600; v.observe("chargingState", "Charging", "TeslaMateVehicle"); r.pass_()
    assert len(cmds(f)) == 3 and calls_named(f, "wake") == []
    for _ in range(10): c.t += 600; v.observe("chargingState", "Charging", "TeslaMateVehicle"); r.pass_()
    assert len(cmds(f)) == 3                                                 # once per desire

def test_a_charging_reading_rearms_a_never_reached_limit_once_per_desire():   # I4 with Fleet still failing, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 90, "FleetAPI"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    for _ in range(20): c.t += 600; v.observe("chargingState", "Charging", "TeslaMateVehicle"); r.pass_()
    assert len(cmds(f)) == 4 and calls_named(f, "wake") == []

def test_a_refused_limit_does_not_block_the_start_forever():                # c4, paid counted
    m, r, v, c = setup(ha(O.Outcome(O.REFUSED, "403"), O.Outcome(O.DONE))); h = prov(m, "HomeAssistant")
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    v.observe("chargeLimit", 60, "HomeAssistant"); v.observe("chargingState", "Stopped", "HomeAssistant")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(5): c.t += 61; r.pass_()
    assert cmds(h).count("setChargeLimit") == 1 and cmds(h).count("startCharging") == 1

def test_a_refused_limit_with_a_paired_ble_whose_radio_is_down_still_blocks_the_start():   # M11
    down = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.UNREACHABLE, "timeout")])         # paired: ready, but not answering
    m, r, v, c = setup(down, ha(O.Outcome(O.REFUSED, "403"), O.Outcome(O.DONE))); h = prov(m, "HomeAssistant")
    v.observe("chargeLimit", 60, "HomeAssistant"); v.observe("chargingState", "Stopped", "HomeAssistant")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(60): c.t += 61; r.pass_()
    assert cmds(h) == ["setChargeLimit"] and "startCharging" not in cmds(down)  # BLE may deliver the limit once its radio is back
    assert m.desired.record(VIN, CHARGE_LIMIT).excluded == {"HomeAssistant": O.REFUSED}

def test_an_unpaired_ble_does_not_hold_the_start_of_a_car_ha_refused():    # review I2, paid counted
    unpaired = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.DONE)], readyFor=lambda vehicle, cap: False)
    m, r, v, c = setup(unpaired, ha(O.Outcome(O.REFUSED, "403"), O.Outcome(O.DONE))); h = prov(m, "HomeAssistant")
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    v.observe("chargeLimit", 60, "HomeAssistant"); v.observe("chargingState", "Stopped", "HomeAssistant")
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); m.desired.set(VIN, CHARGING, True, "heartbeat")
    for _ in range(5): c.t += 61; r.pass_()
    assert cmds(h) == ["setChargeLimit", "startCharging"] and unpaired.calls == []

def test_a_limit_held_unsent_by_a_refusal_does_not_block_the_start():     # c4, NOT_SENT refusals
    for detail in ("wake refused", "no command token"):
        f = fleet(O.Outcome(O.UNREACHABLE if detail == "wake refused" else O.REFUSED, detail))
        m, r, v, c = setup(f); f = prov(m, "FleetAPI"); limit = m.desired.record(VIN, CHARGE_LIMIT)
        m.desired.set(VIN, CHARGE_LIMIT, 80, "policy"); limit.lastOutcome = O.Outcome(O.UNREACHABLE, detail)
        assert r._limitSpent(v, limit)
    limit.lastOutcome = O.Outcome(O.UNREACHABLE, "wake held"); assert not r._limitSpent(v, limit)

def test_an_unreached_limit_whose_one_rearm_is_spent_no_longer_blocks_the_start():   # c4 with the I2 cap
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); limit = m.desired.record(VIN, CHARGE_LIMIT)
    limit.paidSends, limit.paidReached, limit.rearmed = 2, False, False; assert not r._limitSpent(v, limit)   # it can still ride the start's wake
    limit.rearmed = True; assert r._limitSpent(v, limit)

def test_a_limit_whose_ride_wake_failed_does_not_block_the_start_forever():   # c4 with the I2 cap, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI")
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    v.observe("chargeLimit", 90, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()   # asleep: the lower and its retry
    c.t += 7200; m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()   # the limit rides the start's wake, which fails
    assert names(f) == ["wake", "wake", "wake"] and m.desired.record(VIN, CHARGE_LIMIT).rearmed
    for _ in range(60):                                                       # an hour of TeslaMate republishing, unchanged
        c.t += 60; v.observe("chargeLimit", 90, "TeslaMateVehicle"); v.observe("chargingState", "Stopped", "TeslaMateVehicle"); r.pass_()
    # The start went with its own wake: bounded, not stalled. Each desire's I2 cap: the limit's 4 and the
    # start's 4, failed wakes re-arming once (this scripted provider has no wake hold; Fleet's spaces them 30 min)
    assert names(f)[3] == "wake" and len(f.calls) <= 8

def sleepy(f, v):
    # A Fleet car that answers 408 until it is woken
    f.awake = False
    def nxt(method, vin, value):
        f.calls.append((method, vin, value))
        if method == "wake":
            f.awake = True; return O.Outcome(O.DONE)
        if not f.awake: return O.Outcome(O.UNREACHABLE, "408")
        if method == "setChargeLimit": v.observe("chargeLimit", value, "FleetAPI")
        return O.Outcome(O.DONE)
    f._next = nxt

def test_a_lowered_limit_that_failed_once_rides_the_start_s_wake():       # review I1, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("batteryLevel", 40, "TeslaMateVehicle")   # below the limit: it charges
    v.observe("chargeLimit", 90, "FleetAPI"); v.observe("chargingState", "Stopped", "FleetAPI"); sleepy(f, v)
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()
    c.t += 30; m.desired.set(VIN, CHARGING, True, "heartbeat"); r.pass_()
    for _ in range(80): c.t += 30; r.pass_()
    assert names(f) == ["wake", "setChargeLimit", "wake", "startCharging"]
    assert v.believed("chargeLimit").value == 50 and v.lastWrite("chargeLimit").at < v.lastWrite("chargingState").at

def test_a_rearm_resets_the_blind_retry_and_keeps_the_cap():               # review I1, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI"); v.observe("chargeLimit", 60, "FleetAPI")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_()                 # a lower: no wake, its blind retry due
    c.t += RETRY[CHARGE_LIMIT]; v.observe("chargeLimit", 60, "FleetAPI"); r.pass_()   # a live read re-arms first
    rec = m.desired.record(VIN, CHARGE_LIMIT); assert rec.rearmed
    for _ in range(40): c.t += 300; v.observe("chargeLimit", 60, "FleetAPI"); r.pass_()
    assert len(cmds(f)) <= 4 and calls_named(f, "wake") == []

def test_an_unsupported_free_provider_falls_through_without_the_gap():    # review M1
    m, r, v, c = setup(ble(O.Outcome(O.UNSUPPORTED, "no claim")), fleet(O.Outcome(O.DONE))); f = prov(m, "FleetAPI")
    v.observe("chargingState", "Charging", "FleetAPI"); m.desired.set(VIN, CHARGING, False, "distributor")
    r.pass_(); assert m.desired.record(VIN, CHARGING).lastSentAt == 0         # nothing changed on the car: no gap
    assert r.pass_() == 1 and cmds(f) == ["stopCharging"]

def test_a_complete_reading_does_not_rearm_a_lowered_limit():              # review M3, paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.UNREACHABLE, "408"))); f = prov(m, "FleetAPI")
    v.observe("chargeLimit", 90, "FleetAPI"); m.desired.set(VIN, CHARGE_LIMIT, 50, "policy"); r.pass_(); c.t += RETRY[CHARGE_LIMIT]; r.pass_()
    for _ in range(20): c.t += 600; v.observe("chargingState", "Complete", "TeslaMateVehicle"); r.pass_()
    assert len(cmds(f)) == 2 and not m.desired.record(VIN, CHARGE_LIMIT).rearmed


# 12b fix round 1 (review M8; deleted-test audit rows 63, 68, 69, 71): Fleet,
# then Home Assistant, never both for one desire

import pytest

def ha(*outs):
    # As the real provider: billed, and no wake of its own
    from tests.unit.reconcile_helpers import COMMAND_CAPS
    caps = {k: d for k, d in COMMAND_CAPS.items() if k != C.WAKE}
    return ScriptedProvider("HomeAssistant", 6, False, list(outs), caps=caps, tier=C.HA_BILLED)

def want(m, v, aspect, value):
    if aspect == CHARGE_LIMIT: v.observe("chargeLimit", 90, "FleetAPI")
    if aspect == CHARGING: v.observe("chargingState", "Stopped", "FleetAPI")
    m.desired.set(VIN, aspect, value, "test")

ASPECTS = [(CHARGE_LIMIT, 50, 60), (CHARGING, True, True), (AMPS, 10, 11)]

@pytest.mark.parametrize("aspect, value, other", ASPECTS)
@pytest.mark.parametrize("fails", [O.Outcome(O.ERROR, "500"), O.Outcome(O.UNREACHABLE, "408"), O.Outcome(O.UNREACHABLE, "wake held"), O.Outcome(O.DONE)])
def test_fleet_then_ha_never_in_one_pass(aspect, value, other, fails):        # paid counted
    m, r, v, c = setup(fleet(fails), ha(O.Outcome(O.DONE))); f, h = prov(m, "FleetAPI"), prov(m, "HomeAssistant")
    want(m, v, aspect, value)
    for _ in range(60): r.pass_(); c.t += 61
    assert f.calls and h.calls == []                                          # a Fleet failure, hold or success never also bills HA

@pytest.mark.parametrize("aspect, value, other", ASPECTS)
def test_ha_is_reached_only_once_fleet_has_refused_or_is_unavailable(aspect, value, other):   # paid counted
    m, r, v, c = setup(fleet(O.Outcome(O.REFUSED, "403")), ha(O.Outcome(O.DONE))); f, h = prov(m, "FleetAPI"), prov(m, "HomeAssistant")
    want(m, v, aspect, value)
    for _ in range(60): r.pass_(); c.t += 61
    assert len(f.calls) == 1 and h.calls == []                                # the desire's one paid send went to Fleet
    m.desired.clear(VIN, aspect); m.desired.set(VIN, aspect, other, "test"); r.pass_()   # the next desire
    assert len(f.calls) == 1 and len(h.calls) == 1                            # Fleet excluded: HA, once
    m2, r2, v2, c2 = setup(fleet(O.Outcome(O.DONE)), ha(O.Outcome(O.DONE))); prov(m2, "FleetAPI").readyFor = lambda v, cap: False
    want(m2, v2, aspect, value); r2.pass_(); assert len(cmds(prov(m2, "HomeAssistant"))) == 1   # Fleet unavailable: HA at once


def test_a_restore_stamps_the_outside_value():                             # deleted-test audit row 16
    m, r, v, c = setup(ble(O.Outcome(O.DONE))); m.heldLimits.set(VIN, 90, 50); v.observe("chargeLimit", 50, "TeslaBLE")
    leave(v)
    m.desired.set(VIN, CHARGE_LIMIT, 90, "restore", restore=True); r.pass_()
    w = v.lastWrite("chargeLimit"); assert (w.value, w.source) == (90, "TeslaBLE") and prov(m, "TeslaBLE").calls[-1] == ("setChargeLimit", VIN, 90)


def _info(caplog):
    return [r.getMessage() for r in caplog.records if r.levelname == "INFO"]

def test_a_limit_raise_logs_the_wake_and_its_outcome(caplog):
    caplog.set_level("INFO"); m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE))); v.observe("chargeLimit", 50, "TeslaBLE")
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy"); r.pass_()
    assert "TeslaBLE wake for %s before setChargeLimit: done" % VIN in _info(caplog)
    assert not any("no wake" in x for x in _info(caplog))

def test_a_limit_raise_with_unknown_current_limit_wakes_and_reads_first(caplog):
    # An unknown limit is expected to charge: it wakes, and the read before the write finds the limit
    caplog.set_level("INFO"); m, r, v, c = setup(ble(O.Outcome(O.DONE)).reads(60)); m.desired.set(VIN, CHARGE_LIMIT, 70, "policy"); r.pass_()
    assert "TeslaBLE wake for %s before setChargeLimit: done" % VIN in _info(caplog)
    assert cmds(prov(m, "TeslaBLE")) == ["setChargeLimit"]

def test_a_stop_logs_neither_a_wake_nor_a_skip(caplog):
    caplog.set_level("INFO"); m, r, v, c = setup(ble(O.Outcome(O.DONE))); v.observe("chargingState", "Charging", "TeslaBLE")
    m.desired.set(VIN, CHARGING, False, "distributor"); r.pass_(); assert cmds(prov(m, "TeslaBLE")) == ["stopCharging"]
    assert not any(" wake " in x for x in _info(caplog))


def test_a_raise_never_wakes_a_car_its_point_offers_nothing(caplog):
    caplog.set_level("INFO")
    m, r, v, c = setup(ble(O.Outcome(O.DONE)))
    p = prov(m, "TeslaBLE")
    v.observe("chargeLimit", 50, "TeslaBLE")
    m.distributeEVSEPower()  # the car's point wants no power: offered 0
    assert m.offered[VIN] == 0
    m.desired.set(VIN, CHARGE_LIMIT, 80, "policy")
    r.pass_()
    assert names(p) == ["setChargeLimit"]  # sent, but without a wake
    assert "TeslaBLE no wake for %s before setChargeLimit: nothing offered" % VIN in [
        x.getMessage() for x in caplog.records
    ]
    m.setMaxAmpsToDivideAmongSlaves(32)
    v.observe("chargingState", "Stopped", "TeslaBLE")  # wants power now
    m.distributeEVSEPower()
    assert m.offered[VIN] > 0
    c.t += 60
    m.desired.set(VIN, CHARGE_LIMIT, 90, "policy")
    r.pass_()
    assert names(p)[1:3] == ["wake", "setChargeLimit"]  # offered: the raise wakes


def quietCar(battery, limit, *wants):
    # A BLE car plugged in with a battery level and limit, and what is wanted
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), battery=battery)
    v.observe("chargeLimit", limit, "TeslaBLE")
    for aspect, value, kw in wants:
        m.desired.set(VIN, aspect, value, "policy", **kw)
    r.pass_()
    return names(prov(m, "TeslaBLE")), m


def test_a_car_that_will_not_charge_is_never_woken_to_raise_its_limit(caplog):
    caplog.set_level("INFO")
    calls, m = quietCar(80, 50, (CHARGE_LIMIT, 70, {}))  # still at or below its battery
    assert calls == ["setChargeLimit"]
    assert "car won't charge: limit at or below battery" in "\n".join(
        x.getMessage() for x in caplog.records
    )


def test_a_car_that_will_not_charge_is_never_woken_to_lower_its_limit():
    assert quietCar(80, 90, (CHARGE_LIMIT, 70, {}))[0] == ["setChargeLimit"]


def test_a_car_that_will_still_charge_may_be_woken_to_lower_its_limit():
    assert quietCar(40, 90, (CHARGE_LIMIT, 70, {}))[0] == ["wake", "setChargeLimit"]


def test_a_car_wakes_for_a_lower_once_its_point_offers_power():
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE)), battery=40)
    v.observe("chargeLimit", 90, "TeslaBLE")
    m.distributeEVSEPower()  # nothing wanted: offered 0
    assert m.offered[VIN] == 0
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy")
    r.pass_()
    assert names(prov(m, "TeslaBLE")) == ["setChargeLimit"]
    m.setMaxAmpsToDivideAmongSlaves(32)
    v.observe("chargingState", "Stopped", "TeslaBLE")
    m.distributeEVSEPower()
    assert m.offered[VIN] > 0
    c.t += 60
    m.desired.set(VIN, CHARGE_LIMIT, 60, "policy")
    r.pass_()
    assert names(prov(m, "TeslaBLE"))[1:3] == ["wake", "setChargeLimit"]


def test_a_lift_never_wakes_a_car_full_at_its_limit():
    assert quietCar(60, 50, (AMPS, 32, {"lift": True}))[0] == ["setAmps"]


def test_a_pending_higher_limit_makes_the_car_expected_to_charge():
    calls, m = quietCar(60, 50, (CHARGE_LIMIT, 70, {}), (AMPS, 32, {"lift": True}))
    assert calls[:2] == ["wake", "setChargeLimit"] and "setAmps" in calls


def test_an_unknown_battery_level_is_expected_to_charge():
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), battery=None)
    v.observe("chargeLimit", 50, "TeslaBLE")
    m.desired.set(VIN, AMPS, 32, "lift", lift=True)
    r.pass_()
    assert names(prov(m, "TeslaBLE")) == ["wake", "setAmps"]


def test_an_unplugged_car_at_home_is_not_woken_to_lower_its_limit():
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), here=False, battery=40)
    placeAt(m, v)  # parked at home, never seen plugged in
    v.observe("pluggedIn", False, "Gen3TWC")
    v.observe("chargeLimit", 90, "TeslaBLE")
    c.t += 3600
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy")
    r.pass_()
    assert names(prov(m, "TeslaBLE")) == ["setChargeLimit"]


def test_a_release_with_nothing_held_leaves_the_known_limit_in_charge():
    calls, m = quietCar(60, 80, (CHARGE_LIMIT, RELEASE, {}), (AMPS, 32, {"lift": True}))
    assert "wake" in calls


def restarted(feed=True, asleep=True):
    # Just started, the car plugged in with its battery and limit unknown,
    # and a lift wanted; TeslaMate configured but not yet heard from
    from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle

    m, r, v, c = setup(ble(O.Outcome(O.DONE)), battery=None)
    v.observe("state", "asleep" if asleep else "online", "TeslaBLE")
    if feed:
        m.config["vehicle"] = {"TeslaMate": {"enabled": True}}
        tm = TeslaMateVehicle(m)
        tm.vehicles["7"] = v
        m.getModuleByName = {"TeslaMateVehicle": tm}.get
    else:
        tm = None
    m.desired.set(VIN, AMPS, 32, "lift", lift=True)
    r.pass_()
    return m, r, v, c, tm


def test_no_wake_before_the_feed_reports_after_a_start():
    m, r, v, c, tm = restarted()
    assert "wake" not in names(prov(m, "TeslaBLE"))
    c.t += 5  # the report is an observation after the hold
    tm.applyDataToVehicle("7", "charge_limit_soc", "50")
    tm.applyDataToVehicle("7", "battery_level", "60")  # full: nothing to wake for
    r.pass_()
    assert "wake" not in names(prov(m, "TeslaBLE"))


def test_a_wake_follows_the_feeds_first_report_of_a_car_that_will_charge():
    m, r, v, c, tm = restarted()
    c.t += 5
    tm.applyDataToVehicle("7", "charge_limit_soc", "50")
    tm.applyDataToVehicle("7", "battery_level", "40")
    r.pass_()
    assert "wake" in names(prov(m, "TeslaBLE"))


def test_a_silent_feed_is_waited_for_a_minute():
    m, r, v, c, tm = restarted()
    c.t += 55
    v.observe("pluggedIn", True, "Gen3TWC")  # the TWC's heartbeat
    r.pass_()
    assert "wake" not in names(prov(m, "TeslaBLE"))
    c.t += 10
    v.observe("pluggedIn", True, "Gen3TWC")
    r.pass_()
    assert "wake" in names(prov(m, "TeslaBLE"))


def test_no_feed_means_no_wait():
    m, r, v, c, tm = restarted(feed=False)
    assert names(prov(m, "TeslaBLE"))[:2] == ["wake", "setAmps"]


def test_an_awake_car_is_commanded_during_the_wait():
    m, r, v, c, tm = restarted(asleep=False)
    assert names(prov(m, "TeslaBLE")) == ["setAmps"]  # sent without a wake
