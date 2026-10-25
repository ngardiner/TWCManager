"""Gen2TWC.currentVoltage falls back to defaultVoltage on every configured
phase, so a 3-phase TWC that reports no voltage is not offered 3x the amps."""

from types import SimpleNamespace

import pytest

from TWCManager.EVSEInstance.Gen2TWC import Gen2TWC


def voltage(phases, a=0, b=0, c=0):
    twc = SimpleNamespace(
        voltsPhaseA=a,
        voltsPhaseB=b,
        voltsPhaseC=c,
        configConfig={"defaultVoltage": 230, "numberOfPhases": phases},
    )
    return Gen2TWC.currentVoltage.fget(twc)


@pytest.mark.parametrize(
    "phases,expected",
    [(1, [230, 0, 0]), (2, [230, 230, 0]), (3, [230, 230, 230])],
)
def test_default_fills_configured_phases(phases, expected):
    assert voltage(phases) == expected


def test_measured_voltage_wins():
    assert voltage(3, 241, 239, 240) == [241, 239, 240]
