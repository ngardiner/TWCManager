"""
Presence.pick: which car, if any, a plug at a point that reads no VIN
belongs to. The point offers 47 A and delivers 0 A unless a test says
otherwise; a reached car answered BLE, a plugged one read its charge state
over BLE.
"""

from TWCManager.Vehicle import Presence as P
from tests.unit.reconcile_helpers import placeAt
from tests.unit.test_presence import VIN2, home, later
from tests.unit.test_limit_reconciler import VIN


def cars():
    m, r, a, c = home()
    later(c, 1000)  # the cars' plug readings below are new, not just now
    return m, a, m.vehicleRegistry.getOrCreate(VIN2, "t"), c


def reach(v):
    v.observe("bleReach", True, "TeslaBLE")


def plugged(v, pilot=47, actual=0, state="Charging"):
    reach(v)
    for field, value in (
        ("chargingState", state),
        ("availableCurrent", pilot),
        ("actualCurrent", actual),
    ):
        if value is not None:
            v.observe(field, value, "TeslaBLE")


def pick(m, offered=47, actual=0):
    v = P.pick(m, offered, actual)
    return v and v.vin


def test_tolerance_is_one_amp():
    assert P.PILOT_MATCH_TOLERANCE == 1 and P.PLUGGED_JUST_NOW == 120


def test_single_candidate_inside_radius_wins():
    m, a, b, c = cars()
    placeAt(m, a)
    m.vehicleRegistry.forget(VIN2)
    assert pick(m) == VIN


def test_ble_pilot_match_picks_one_of_two():
    m, a, b, c = cars()
    plugged(a)
    reach(b)
    assert pick(m) == VIN


def test_two_pilot_matches_are_ambiguous():
    m, a, b, c = cars()
    plugged(a)
    plugged(b, pilot=46.5)
    assert pick(m) is None


def test_pilot_mismatch_means_another_charger():
    m, a, b, c = cars()
    plugged(a, pilot=32)
    assert pick(m) is None


def test_fast_charger_is_never_the_twc():
    # Plugged in just now, but on a fast charger: no guess, and nothing
    # places it here
    m, a, b, c = cars()
    a.observe("fastChargerPresent", True, "FleetAPI")
    a.observe("pluggedIn", True, "FleetAPI")
    assert pick(m) is None


def test_ble_reachable_beats_ble_silent_inside_radius():
    m, a, b, c = cars()
    reach(a)
    placeAt(m, b)
    assert pick(m) == VIN


def test_read_unplugged_is_never_a_candidate():
    m, a, b, c = cars()
    plugged(a, pilot=None, state="Disconnected")
    placeAt(m, b)
    assert pick(m) == VIN2
    m.vehicleRegistry.forget(VIN2)
    assert pick(m) is None


def test_actual_current_must_match_while_drawing():
    # The point delivers 30 A; the car's pilot matches but it draws 12 A, so
    # it is not matched, and with another car reached nobody stands out
    m, a, b, c = cars()
    plugged(a, actual=12)
    reach(b)
    assert pick(m, 47, 30) is None
    later(c)
    plugged(a, actual=30.4)
    assert pick(m, 47, 30) == VIN


def test_plugged_read_without_pilot_falls_through_to_reach():
    m, a, b, c = cars()
    plugged(a, pilot=None)
    assert pick(m) == VIN
    reach(b)
    assert pick(m) is None
