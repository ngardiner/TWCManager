# tests/unit/test_gen2_unplug.py: a Gen2 status edge asks for a check of the
# car, through a real Gen2TWC's heartbeats. The status codes are the car's
# talk with the TWC, not the plug: a 00, a max that drops to 0 inside one, or
# a plug state change only asks the car what changed. Here the car has no
# provider, so nothing can check it; test_command_path_parity.py has the
# checks that answer.
import logging
import time

import pytest

from TWCManager.Vehicle import Presence as P
from tests.unit.gen2_helpers import drive
from tests.unit.test_limit_reconciler import VIN, setup


@pytest.fixture
def rig(monkeypatch):
    m, r, v, c = setup(here=False)
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    return m, v, c, s


def beats(m, s, c, seconds, state, maxA, actual=0.0, step=10):
    # One heartbeat per step, the first one step from now
    for _ in range(int(seconds / step)):
        c.t += step
        drive(m, s, actual, state=state, amps_max=maxA)


def named(m, v, c, s):
    # Linked at 03 with the car's 32 A, named by VIN
    beats(m, s, c, 10, 0x03, 32.0)
    m.vehicleRegistry.observeTwcVin("8244", v.vin)
    beats(m, s, c, 400, 0x03, 32.0)


@pytest.fixture
def checks(rig, monkeypatch):
    # When each plug check is queued; no job runs
    m, v, c, s = rig
    out = []

    def submit(key, kind, run):
        if kind == "plugCheck":
            out.append(c.t)

    monkeypatch.setattr(m.reconciler, "submitJob", submit)
    return out


def test_a_status_edge_asks_for_one_check_after_the_wait(rig, checks):
    m, v, c, s = rig
    named(m, v, c, s)
    edge = c.t + 10
    beats(m, s, c, 10, 0x00, 0.0)
    assert checks == []  # a live feed reports first
    beats(m, s, c, 600, 0x00, 0.0)
    assert checks == [edge + s.CHECK_WAIT]


@pytest.mark.parametrize(
    "runs, edges",
    [
        ([(0x03, 32.0), (0x00, 32.0)], 1),  # a 00
        ([(0x03, 47.0), (0x00, 47.0), (0x00, 0.0)], 2),  # and its max dropping
        ([(0x03, 32.0), (0x00, 0.0), (0x03, 32.0)], 2),  # a plug again
        ([(0x03, 32.0), (0x09, 32.0), (0x01, 32.0), (0x03, 6.0)], 0),
        ([(0x00, 0.0), (0x00, 47.0)], 0),  # a restart at 00, the max rising
    ],
)
def test_which_heartbeats_are_edges(rig, checks, runs, edges):
    m, v, c, s = rig
    m.vehicleRegistry.observeTwcVin("8244", v.vin)
    for state, maxA in runs:
        beats(m, s, c, 120, state, maxA)
    assert len(checks) == edges


def test_a_car_awake_at_its_limit_flickering_00_09_is_checked_once(rig, checks):
    # Box, 2026-10-20 13:57-14:11: 09 at 47 A and 00 at 46 A every 18 s,
    # drawing nothing, for 14 minutes
    m, v, c, s = rig
    named(m, v, c, s)
    for _ in range(int(14 * 60 / 36)):
        beats(m, s, c, 18, 0x09, 47.0, step=2)
        beats(m, s, c, 18, 0x00, 46.0, step=2)
    assert len(checks) == 1
    beats(m, s, c, 60, 0x03, 6.0)  # asleep again: the next event is checked
    beats(m, s, c, 60, 0x00, 47.0)
    assert len(checks) == 2


def test_edges_while_the_car_draws_current_run_no_check(rig, checks):
    m, v, c, s = rig
    named(m, v, c, s)
    for _ in range(10):
        beats(m, s, c, 20, 0x01, 32.0, actual=16.0)
        beats(m, s, c, 20, 0x00, 32.0, actual=16.0)
        beats(m, s, c, 20, 0x00, 0.0, actual=16.0)
    assert checks == []


def test_a_check_due_while_the_car_draws_current_is_skipped(rig, caplog):
    m, v, c, s = rig
    named(m, v, c, s)
    caplog.set_level(logging.INFO)
    beats(m, s, c, 10, 0x00, 0.0)  # the edge, at 0 A
    beats(m, s, c, 60, 0x00, 0.0, actual=16.0)  # drawing when the check runs
    assert "checking %s" % VIN in caplog.text
    assert "plugged in:" not in caplog.text


def test_every_00_at_0_a_is_checked(rig, checks):
    m, v, c, s = rig
    named(m, v, c, s)
    beats(m, s, c, 60, 0x00, 47.0)  # woken: checked once
    beats(m, s, c, 60, 0x09, 47.0)
    beats(m, s, c, 60, 0x00, 47.0)  # the same event: not again
    beats(m, s, c, 60, 0x00, 0.0)  # the max drops: always
    assert len(checks) == 2


def test_an_edge_on_an_empty_twc_checks_nothing(rig, checks):
    # The plug-in side begins the session and names it
    m, v, c, s = rig
    beats(m, s, c, 60, 0x00, 0.0)
    beats(m, s, c, 120, 0x03, 32.0)
    assert checks == [] and s.vehicleKey == "anon:8244"


def test_nothing_can_check_the_car_so_it_stays(rig, caplog):
    m, v, c, s = rig
    named(m, v, c, s)
    caplog.set_level(logging.INFO)
    beats(m, s, c, 600, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"
    assert "TWC 8244: 03 → 00, max 32.00 → 0.00 A: checking %s" % VIN in caplog.text
    assert "TWC 8244: plugged in: {'%s': None}" % VIN in caplog.text


@pytest.mark.parametrize(
    "answers, at, unplugged",
    [
        ([None, False], [0, 1], True),  # the cloud learns of the wake later
        ([None, None], [0, 1], False),  # once per edge
        ([False], [0], True),
        ([True], [0], False),
    ],
)
def test_an_edge_no_check_answered_is_checked_once_more(
    rig, monkeypatch, answers, at, unplugged
):
    m, v, c, s = rig
    named(m, v, c, s)
    asked, left = [], list(answers)

    def check(master, car, since):
        asked.append(c.t - since)
        return left.pop(0) if left else None

    monkeypatch.setattr(P, "checkPlug", check)
    beats(m, s, c, 600, 0x00, 0.0)
    waits = [s.CHECK_WAIT, s.RECHECK_WAIT]
    assert asked == [waits[i] for i in at]
    assert (m.vehicleRegistry.vehicleAt("8244") is None) is unplugged


def unnamed(m, c, s):
    # Plugged in and left unnamed: the attribution job is left pending
    beats(m, s, c, 400, 0x03, 32.0)
    assert s.vehicleKey == "anon:8244"


def test_an_unnamed_session_nothing_can_check_stays(rig):
    m, v, c, s = rig
    unnamed(m, c, s)
    beats(m, s, c, 600, 0x00, 0.0)
    assert m.vehicleRegistry.vehicleAt("8244").isAnonymous
    assert v.markAt("unplug") == P.NEVER


def test_an_unnamed_session_with_no_candidate_ends_at_the_check(rig):
    # A car no account knows: nothing else may be on the TWC
    m, v, c, s = rig
    unnamed(m, c, s)
    m.vehicleRegistry.forget(VIN)
    beats(m, s, c, 60, 0x00, 0.0)
    assert m.vehicleRegistry.vehicleAt("8244") is None


def test_an_anonymous_point_finds_its_cars_once_per_pass(rig, monkeypatch):
    from TWCManager.EVSEInstance import ChargingPoint

    m, v, c, s = rig
    beats(m, s, c, 20, 0x03, 32.0)
    assert s.vehicleKey == "anon:8244"
    calls = []
    real = ChargingPoint.candidates
    monkeypatch.setattr(
        ChargingPoint, "candidates", lambda *a: calls.append(a) or real(*a)
    )
    m.controlPass()
    assert len(calls) == 1
