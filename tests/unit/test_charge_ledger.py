import pytest

from TWCManager.Vehicle.ChargeLedger import ChargeLedger, Snapshot

VIN = "5YJ3E1EA0KF000001"
T0 = 1793500000.0
WALL = 3.6e6 / 0.93


def wall(kwh, secs):
    return kwh * WALL / secs


def never(vin):
    return 0


def test_a_session_starts_from_its_energy_when_no_step_was_seen():
    l = ChargeLedger(clock=lambda: T0 + 400)
    l.observe(VIN, Snapshot(T0, "Charging", 1.02, 11376.0))
    start = T0 - 1.02 * 3.6e6 / (11376.0 * 0.93)
    assert l.wattsBetween(start, T0, lambda v: 0) == pytest.approx(11376.0, rel=0.01)


def test_a_session_read_after_a_slowdown_starts_from_the_fastest_rate():
    # Box, 2026-11-03: 11.7 kW 09:42-09:55, then 1.3 kW until the first read
    # at 10:00:28 found 2.38 kWh; at the read's rate the start fell at 07:52
    l = ChargeLedger(clock=lambda: T0)
    l.observe(VIN, Snapshot(T0, "Charging", 2.38, 1200.0), ceiling=11520.0)
    start = T0 - 2.38 * 3.6e6 / (11520.0 * 0.93)
    assert l.wattsBetween(T0 - 7200, start, never) == 0
    assert l.wattsBetween(start, T0, never) == pytest.approx(11520.0, rel=0.01)


def test_a_load_step_sets_the_start():
    l = ChargeLedger(clock=lambda: T0); l.observe(VIN, Snapshot(T0, "Charging", 0.82, 11328.0), startHint=T0 - 300)
    assert l.wattsBetween(T0 - 300, T0, lambda v: 0) == pytest.approx(0.82 * 3.6e6 / 0.93 / 300)


def test_energy_between_snapshots_is_spread_across_them():
    l = ChargeLedger(clock=lambda: T0 + 743)
    l.observe(VIN, Snapshot(T0, "Charging", 1.02, 11376.0), startHint=T0)
    assert l.observe(VIN, Snapshot(T0 + 743, "Charging", 3.28, 11400.0))
    assert l.wattsBetween(T0, T0 + 743, never) == pytest.approx(wall(2.26, 743))


def test_the_closing_snapshot_ends_it_by_energy_and_trims_the_belief():
    l = ChargeLedger(clock=lambda: T0 + 900)
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0)
    # 0.5 kWh at 11520 W wall lasts 0.5 * WALL / 11520 s
    end = T0 + 0.5 * WALL / 11520.0
    assert l.wattsBetween(end + 10, end + 20, lambda v: 11520) == pytest.approx(11520)
    assert l.observe(VIN, Snapshot(T0 + 800, "Complete", 1.5, 0.0))
    assert not l.isOpen(VIN)
    assert l.wattsBetween(end + 10, end + 20, lambda v: 11520) == 0
    assert l.wattsBetween(T0, end, never) == pytest.approx(11520)


def test_the_end_never_passes_the_closing_stamp():
    l = ChargeLedger(clock=lambda: T0 + 500)
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0)
    l.observe(VIN, Snapshot(T0 + 60, "Stopped", 3.0, 0.0))
    assert l.wattsBetween(T0, T0 + 60, never) == pytest.approx(11520)
    assert l.wattsBetween(T0 + 60, T0 + 500, lambda v: 11520) == 0


def test_without_a_closing_snapshot_it_ends_at_the_last_charging_stamp():
    l = ChargeLedger(clock=lambda: T0 + 1000)
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0)
    l.observe(VIN, Snapshot(T0 + 300, "Charging", 2.0, 11520.0))
    assert l.wattsBetween(T0 + 300, T0 + 1000, lambda v: 11520) == pytest.approx(11520 * 700 / 700)
    assert l.end(VIN)
    assert not l.isOpen(VIN)
    assert l.wattsBetween(T0 + 300, T0 + 1000, lambda v: 11520) == 0
    assert l.wattsBetween(T0, T0 + 300, never) == pytest.approx(wall(1.0, 300))
    assert not l.end(VIN)


def test_an_energy_drop_while_charging_is_a_new_session():
    l = ChargeLedger(clock=lambda: T0 + 700)
    l.observe(VIN, Snapshot(T0, "Charging", 5.0, 11520.0), startHint=T0 - 100)
    l.observe(VIN, Snapshot(T0 + 300, "Charging", 0.5, 11520.0))
    assert l.isOpen(VIN)
    # Nothing is credited for the drop's span; the new session backfills its own energy
    first = T0 + 300 - 0.5 * 3.6e6 / (11520.0 * 0.93)
    assert l.wattsBetween(T0, first, never) == 0
    assert l.wattsBetween(first, T0 + 300, never) == pytest.approx(11520, rel=0.001)
    assert l.wattsBetween(T0 - 100, T0, never) == pytest.approx(wall(5.0, 100))


def test_a_snapshot_without_energy_backfills_nothing_before_its_stamp():
    l = ChargeLedger(clock=lambda: T0 + 100)
    l.observe(VIN, Snapshot(T0, "Charging", None, 11520.0))
    assert l.isOpen(VIN)
    assert l.wattsBetween(T0 - 3600, T0, lambda v: 11520) == 0
    assert l.wattsBetween(T0, T0 + 100, lambda v: 11520) == pytest.approx(11520)


def test_an_old_or_repeated_snapshot_changes_nothing():
    l = ChargeLedger(clock=lambda: T0 + 400)
    assert l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0 - 100)
    before = l.toJSON()
    assert not l.observe(VIN, Snapshot(T0, "Charging", 2.0, 11520.0))
    assert not l.observe(VIN, Snapshot(T0 - 50, "Complete", 3.0, 0.0))
    assert l.toJSON() == before and l.isOpen(VIN)


def test_the_ledger_round_trips_with_a_session_open():
    l = ChargeLedger(clock=lambda: T0 + 600)
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0 - 100)
    r = ChargeLedger.fromJSON(l.toJSON(), clock=lambda: T0 + 600)
    assert r.isOpen(VIN) and r.toJSON() == l.toJSON()
    assert r.observe(VIN, Snapshot(T0 + 300, "Charging", 2.0, 11520.0))
    assert r.wattsBetween(T0, T0 + 300, never) == pytest.approx(wall(1.0, 300))
    assert not ChargeLedger.fromJSON({}).toJSON() and not ChargeLedger.fromJSON(None).toJSON()


def test_segments_older_than_two_days_are_pruned():
    l = ChargeLedger(clock=lambda: T0)
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0 - 100)
    l.end(VIN)
    l.prune(T0 + ChargeLedger.KEEP_SECS - 50)
    assert l.wattsBetween(T0 - 100, T0, never) > 0
    l.prune(T0 + ChargeLedger.KEEP_SECS + 50)
    # The counter stays known: a charge resumed later adds only its growth
    assert l.wattsBetween(T0 - 100, T0, never) == 0 and l.toJSON()[VIN]["segments"] == []
    assert l.observe(VIN, Snapshot(T0 + 1, "Disconnected", 0.0, 0.0))
    l.prune(T0 + ChargeLedger.KEEP_SECS + 50); assert l.toJSON() == {}


def test_two_cars_sum():
    l = ChargeLedger(clock=lambda: T0 + 100)
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0 - 100)
    l.observe("OTHER", Snapshot(T0, "Charging", 0.5, 5760.0), startHint=T0 - 100)
    assert l.wattsBetween(T0 - 100, T0, never) == pytest.approx(wall(1.5, 100))
    assert l.wattsBetween(T0, T0 + 100, lambda v: 1000 if v == VIN else 200) == pytest.approx(1200)


def test_last_stamp_is_the_open_sessions_newest_snapshot():
    l = ChargeLedger(clock=lambda: T0)
    assert l.lastStamp(VIN) is None
    l.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0)); assert l.lastStamp(VIN) == T0
    l.observe(VIN, Snapshot(T0 + 300, "Charging", 2.0, 11520.0)); assert l.lastStamp(VIN) == T0 + 300
    l.observe(VIN, Snapshot(T0 + 600, "Complete", 2.5, 0.0)); assert l.lastStamp(VIN) is None    # closed


def test_load_swaps_state_into_the_same_ledger():
    src = ChargeLedger(); src.observe(VIN, Snapshot(T0, "Charging", 1.0, 11520.0), startHint=T0 - 100)
    l = ChargeLedger(); l.observe("other", Snapshot(T0, "Charging", 1.0, 100.0), startHint=T0 - 10)
    l.load(src.toJSON())
    assert l.toJSON() == src.toJSON() and l.isOpen(VIN) and not l.isOpen("other")
    l.load(None); assert not l.toJSON()


def night(l, energy=3.3, drift=0.12):
    # A charge that ended, the car left plugged in: its counter drifts down after
    l.observe(VIN, Snapshot(T0, "Charging", energy, 11520.0), startHint=T0 - 1000)
    l.observe(VIN, Snapshot(T0 + 100, "Complete", energy - drift, 0.0))
    return T0 + 86400


def test_a_resumed_charge_counts_only_what_it_added():
    l = ChargeLedger(clock=lambda: T0 + 86400)
    at = night(l)
    l.observe(VIN, Snapshot(at, "Charging", 4.18, 11520.0))
    start = at - 1.0 * 3.6e6 / (11520.0 * 0.93)
    assert l.wattsBetween(start - 100, at, never) == pytest.approx(wall(1.0, at - start + 100))
    assert l.wattsBetween(T0 + 100, start - 100, never) == 0


def test_a_resumed_charge_with_a_step_spreads_only_what_it_added():
    l = ChargeLedger(clock=lambda: T0 + 86400)
    at = night(l)
    l.observe(VIN, Snapshot(at, "Charging", 3.48, 11520.0), startHint=at - 300)
    assert l.wattsBetween(at - 300, at, never) == pytest.approx(wall(0.3, 300))


def test_a_slow_resume_under_an_unseen_drift_is_the_same_plug_in():
    l = ChargeLedger(clock=lambda: T0 + 86400)
    at = night(l, drift=0.0)
    l.observe(VIN, Snapshot(at, "Charging", 3.25, 1440.0), startHint=at - 300)
    assert l.wattsBetween(T0 + 100, at, never) == 0
    l.observe(VIN, Snapshot(at + 300, "Charging", 3.35, 1440.0))
    assert l.wattsBetween(at, at + 300, never) == pytest.approx(wall(0.1, 300))


def test_a_reading_further_under_is_an_unseen_unplug():
    l = ChargeLedger(clock=lambda: T0 + 86400)
    at = night(l, drift=0.0)
    l.observe(VIN, Snapshot(at, "Charging", 2.7, 11520.0), startHint=at - 1200)
    assert l.wattsBetween(at - 1200, at, never) == pytest.approx(wall(2.7, 1200))


def test_unplugging_resets_the_count():
    l = ChargeLedger(clock=lambda: T0 + 86400)
    at = night(l)
    assert l.observe(VIN, Snapshot(at - 500, "Disconnected", 0.0, 0.0))
    l.observe(VIN, Snapshot(at, "Charging", 3.5, 11520.0), startHint=at - 1200)
    assert l.wattsBetween(at - 1200, at, never) == pytest.approx(wall(3.5, 1200))


def test_a_new_session_never_reaches_back_past_the_last_one():
    l = ChargeLedger(clock=lambda: T0 + 400)
    l.observe(VIN, Snapshot(T0, "Charging", 2.0, 11520.0), startHint=T0 - 100)
    l.observe(VIN, Snapshot(T0 + 100, "Stopped", 2.0, 0.0))
    # A drop in the counter, an unplug no snapshot saw: its own energy, but after the last
    l.observe(VIN, Snapshot(T0 + 300, "Charging", 0.9, 11520.0))
    assert l.wattsBetween(T0 - 100, T0, never) == pytest.approx(wall(2.0, 100))
    assert l.wattsBetween(T0, T0 + 300, never) == pytest.approx(wall(0.9, 300))


def test_the_last_energy_survives_a_restart():
    l = ChargeLedger(clock=lambda: T0 + 86400)
    at = night(l)
    l.prune(at)
    r = ChargeLedger.fromJSON(l.toJSON(), clock=lambda: T0 + 86400)
    r.observe(VIN, Snapshot(at, "Charging", 3.48, 11520.0), startHint=at - 300)
    assert r.wattsBetween(at - 300, at, never) == pytest.approx(wall(0.3, 300))
