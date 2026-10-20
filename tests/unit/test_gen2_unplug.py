# tests/unit/test_gen2_unplug.py: the Gen2 unplug rule through a real
# Gen2TWC's heartbeats. A Gen2 reports 00 both for an empty TWC and while a
# plugged car wakes, so only a 00 whose first heartbeat drops the slave's
# max amps from above 0 to 0, held a minute after a plugged run, and well
# after the link, is an unplug.
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
    # Linked at 03 with the car's 32 A, named by VIN, past the link grace
    beats(m, s, c, 10, 0x03, 32.0)
    m.vehicleRegistry.observeTwcVin("8244", v.vin)
    beats(m, s, c, 400, 0x03, 32.0)


def test_00_with_the_max_amps_drop_after_a_plugged_run_is_one_unplug(rig):
    m, v, c, s = rig
    named(m, v, c, s)
    beats(m, s, c, 50, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER  # not yet held a minute
    beats(m, s, c, 20, 0x00, 0.0)
    assert v.markAt("unplug") > P.NEVER and P.presence(m, v) == P.NOT_HOME
    assert v.value("twcVin") is None


def test_00_without_the_drop_on_the_first_00_heartbeat_is_never_an_unplug(rig):
    m, v, c, s = rig
    named(m, v, c, s)
    beats(m, s, c, 600, 0x00, 47.0)  # a waking car keeps its max
    assert v.markAt("unplug") == P.NEVER
    beats(m, s, c, 120, 0x03, 32.0)
    beats(m, s, c, 10, 0x00, 47.0)
    beats(m, s, c, 600, 0x00, 0.0)  # the drop comes a heartbeat late
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"


def test_an_idle_car_already_at_0_a_never_gives_a_twc_unplug(rig):
    m, v, c, s = rig
    beats(m, s, c, 10, 0x03, 0.0)
    m.vehicleRegistry.observeTwcVin("8244", v.vin)
    beats(m, s, c, 400, 0x03, 0.0)
    beats(m, s, c, 600, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"


def test_00_inside_link_grace_or_without_a_plugged_run_is_never_an_unplug(rig):
    m, v, c, s = rig
    beats(m, s, c, 10, 0x03, 32.0)
    m.vehicleRegistry.observeTwcVin("8244", v.vin)
    beats(m, s, c, 200, 0x03, 32.0)
    beats(m, s, c, 600, 0x00, 0.0)  # 210 s after the link
    assert v.markAt("unplug") == P.NEVER
    beats(m, s, c, 50, 0x03, 32.0)  # a short plugged run
    beats(m, s, c, 600, 0x00, 0.0)
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"


def test_onset_logs_the_slave_max_amps(rig, caplog):
    m, v, c, s = rig
    named(m, v, c, s)
    with caplog.at_level(logging.INFO):
        beats(m, s, c, 10, 0x00, 0.0)
        beats(m, s, c, 60, 0x03, 32.0)
        beats(m, s, c, 10, 0x00, 47.0)
    onsets = [r.getMessage() for r in caplog.records if "00 onset" in r.getMessage()]
    assert onsets == [
        "TWC 8244: 00 onset, max 32.00 A before, 0.00 A now: an unplug if it holds",
        "TWC 8244: 00 onset, max 32.00 A before, 47.00 A now: not an unplug",
    ]


def test_reported_once_per_00_run(rig, monkeypatch):
    m, v, c, s = rig
    unplugs = []
    monkeypatch.setattr(m, "_plugSession", lambda s, begin: begin or unplugs.append(s))
    named(m, v, c, s)
    beats(m, s, c, 900, 0x00, 0.0)
    assert unplugs == [s]
    beats(m, s, c, 120, 0x03, 32.0)
    beats(m, s, c, 900, 0x00, 0.0)
    assert unplugs == [s, s]


def test_a_re_link_starts_a_new_plugged_run(rig):
    m, v, c, s = rig
    named(m, v, c, s)
    assert m.newSlave(b"\x82\x44", 80) is s  # the TWC links again
    beats(m, s, c, 30, 0x03, 32.0)
    beats(m, s, c, 600, 0x00, 0.0)  # 30 s into the new run
    assert v.markAt("unplug") == P.NEVER and v.value("twcVin") == "8244"


def test_an_unnamed_session_outlasts_a_waking_00_and_departs_no_one(
    rig, monkeypatch, caplog
):
    m, v, c, s = rig
    jobs = []
    monkeypatch.setattr(m.reconciler, "submitJob", lambda *job: jobs.append(job[:2]))
    caplog.set_level(logging.INFO)
    beats(m, s, c, 400, 0x03, 32.0)
    beats(m, s, c, 20, 0x00, 32.0)  # a car waking
    beats(m, s, c, 60, 0x03, 32.0)
    assert jobs.count(("8244", "attribute")) == 1
    assert m.vehicleRegistry.vehicleAt("8244").isAnonymous
    beats(m, s, c, 120, 0x00, 0.0)  # it left
    assert m.vehicleRegistry.vehicleAt("8244") is None
    assert "anon:8244 departs" not in caplog.text


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
    assert len(calls) == 1 and "anon:8244" in m.offered
