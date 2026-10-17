"""
The charge-limit decision rules (Reconcile/Rules.decideChargeLimit), ported
from presence/2-charge-limits with the saved record as a HeldLimit: nothing
saved is None, "we hold none" (-1) is held=None, and a restore (-1) is
restore=True with the outside limit as the desired value.
"""

import pytest

from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit
from TWCManager.Vehicle.Reconcile.Rules import (
    ChargeLimitDecision,
    decideChargeLimit,
    needsLiveRead,
)

H = HeldLimit
NONE = None
R = "restore"


def call(limit, current, saved):
    # A restore's desired value is the outside limit it puts back
    restore = limit == R
    desired = (saved.outside if saved else 90) if restore else limit
    return decideChargeLimit(desired, restore, current, saved)


# fmt: off
# (id, limit, current, saved,
#  send, saveNow, saveOnSuccess, settled)
CASES = [
    # Apply, current known.
    ("first apply, nothing saved", 70, 90, NONE, 70, None, H(90, 70), False),
    ("first apply after a restore (-1)", 70, 85, H(90, None),
     70, None, H(85, 70), False),
    ("our limit still set; policy changes it", 60, 70, H(90, 70),
     60, None, H(90, 60), False),
    ("hand-set while held is the new normal", 70, 80, H(90, 70),
     70, None, H(80, 70), False),
    ("already at target, nothing held", 70, 70, NONE,
     None, H(70, 70), None, True),
    ("already at target, ours held", 70, 70, H(90, 70),
     None, H(90, 70), None, True),
    ("already at target, set by hand", 70, 70, H(90, 60),
     None, H(70, 70), None, True),
    # Restore, current known.
    ("restore while ours is set", R, 70, H(90, 70),
     90, None, H(90, None), False),
    ("restore, hand-set limit stands", R, 85, H(90, 70),
     None, H(85, None), None, True),
    ("restore, ours set and equal to normal", R, 90, H(90, 90),
     None, H(90, None), None, True),
    ("restore with nothing held (-1)", R, 85, H(90, None),
     None, H(85, None), None, True),
    ("restore with nothing saved", R, 85, NONE, None, None, None, True),
    # Current unknown (no source knows it): nothing is sent by any provider,
    # free or paid, and nothing is decided; the reconciler retries.
    ("unknown, apply", 70, None, H(90, 70), None, None, None, False),
    ("unknown, first apply", 70, None, NONE, None, None, None, False),
    ("unknown, restore", R, None, H(90, 70), None, None, None, False),
    ("unknown, restore, nothing saved", R, None, NONE, None, None, None, False),
]
# fmt: on


@pytest.mark.parametrize(
    "limit,current,saved,send,saveNow,saveOnSuccess,settled",
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
def test_rules(limit, current, saved, send, saveNow, saveOnSuccess, settled):
    d = call(limit, current, saved)
    assert isinstance(d, ChargeLimitDecision)
    assert (d.send, d.saveNow, d.saveOnSuccess, d.settled) == (
        send,
        saveNow,
        saveOnSuccess,
        settled,
    )
    assert isinstance(d.reason, str) and d.reason


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_invariants(case):
    _, limit, current, saved = case[:4]
    d = call(limit, current, saved)
    # A save happens either now (no send) or after a successful send.
    assert d.saveNow is None or d.send is None
    assert d.saveOnSuccess is None or d.send is not None
    if d.send is not None:
        assert d.saveOnSuccess is not None and not d.settled
    if d.settled:
        assert d.send is None


def test_reasons():
    assert call(70, 70, NONE).reason == "already at 70%; nothing sent"
    assert call(R, 90, H(90, 90)).reason == "already at 90%; nothing to restore"
    assert call(R, 85, H(90, 70)).reason == "set by hand to 85%; nothing to restore"
    assert call(R, 85, NONE).reason == "no saved normal limit; nothing to restore"
    assert (
        call(70, None, NONE).reason == "could not read the current limit; nothing sent"
    )
    assert "hand-set 80%" in call(70, 80, H(90, 70)).reason


def test_held_values_are_clamped_to_50_100():  # ledger L28
    d = decideChargeLimit(30, False, 70, None)
    assert (d.send, d.saveOnSuccess) == (50, H(70, 50))
    d = decideChargeLimit(120, False, 70, H(90, 70))
    assert (d.send, d.saveOnSuccess) == (100, H(90, 100))
    # A migrated outside limit out of range is put back clamped
    d = decideChargeLimit(120, True, 70, H(120, 70))
    assert (d.send, d.saveOnSuccess) == (100, H(100, None))
    d = decideChargeLimit(20, True, 70, H(20, 70))
    assert (d.send, d.saveOnSuccess) == (50, H(50, None))



@pytest.mark.parametrize(
    "restore,telemetry,saved,expected",
    [
        (True, 80, H(90, 50), True),  # stale or hand-set: read live
        (True, 50, H(90, 50), False),  # ours is still set: trust it
        (True, None, H(90, 50), False),  # no telemetry value
        (True, 80, H(90, None), False),  # no limit held: nothing hinges on it
        (True, 80, NONE, False),  # nothing saved
        (False, 80, H(90, 50), False),  # applies keep telemetry
    ],
)
def test_needs_live_read(restore, telemetry, saved, expected):
    assert needsLiveRead(restore, telemetry, saved) is expected
