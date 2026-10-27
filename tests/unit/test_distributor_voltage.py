"""
distributeEVSEPower budgets with the voltage the TWCs measure.

The budget used to be amps * defaultVoltage * numberOfPhases, while
Gen2TWC.setTargetPower() converts the watts back with the voltage the TWC
measures. With defaultVoltage set to the 400 V line-to-line voltage of a
three-phase supply (every other use treats it as per-phase) and a TWC
measuring ~240 V per phase, a 16 A Charge Now or schedule offered
16 * 400 / 240 = 26.7 A, capped at the wiring limit, and green energy
offered 1.67x the surplus.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.EVSEInstance.Gen2TWC import Gen2TWC
from TWCManager.TWCMaster import TWCMaster
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

THREE_PHASE_400 = {"defaultVoltage": 400, "numberOfPhases": 3}


class _Point(EVSEInstance):
    # A point drawing at three phases of 240 V, with the TWC's voltage
    wantsToCharge, isReadOnly, vehicleKey, ID = True, False, "p", "p"
    maxPower, minPower, controllers = 100000, 1200, ["Gen2TWCs"]
    currentVoltage, reportedAmpsActual = [240] * 3, 10

    def __init__(self):
        self.setTargetPower, self.localEndpoints = Mock(), [self]


def _evse():
    return _Point()


def _twc(volts):
    return SimpleNamespace(voltsPhaseA=volts, voltsPhaseB=volts, voltsPhaseC=volts)


def _master(evses, amps, slaves, config):
    m = TWCMaster.__new__(TWCMaster)
    m.config = {"config": dict({"wiringMaxAmpsAllTWCs": 80}, **config)}
    m.vehicleRegistry = VehicleRegistry(m.config)
    ctrl = MagicMock()
    ctrl.maxPower = 100000
    # A non-Gen2 controller makes the distributor run.
    m.getModulesByType = lambda t: [
        {"name": "Gen2TWCs", "ref": ctrl},
        {"name": "VehicleEndpoints", "ref": ctrl},
    ]
    m.chargingPoints = lambda: evses
    m.getMaxAmpsToDivideAmongSlaves = lambda: amps
    m.getSlaveTWCs = lambda: slaves
    return m


def test_budget_uses_measured_voltage():
    e = _evse()
    _master([e], 16, [_twc(240)], THREE_PHASE_400).distributeEVSEPower()
    e.setTargetPower.assert_called_once_with(16 * 240 * 3)


def test_budget_falls_back_to_default_voltage_without_measurements():
    e = _evse()
    _master([e], 16, [_twc(0)], THREE_PHASE_400).distributeEVSEPower()
    e.setTargetPower.assert_called_once_with(16 * 400 * 3)


def test_policy_amps_reach_the_twc_unchanged():
    # The real Gen2TWC conversion back to amps: 16 A in, 16 A out.
    twc = Gen2TWC.__new__(Gen2TWC)
    twc.voltsPhaseA = twc.voltsPhaseB = twc.voltsPhaseC = 240
    e = _evse()
    e.setTargetPower.side_effect = twc.setTargetPower
    _master([e], 16, [twc], THREE_PHASE_400).distributeEVSEPower()
    assert twc.pointAmps == 16


def test_the_wiring_cap_converts_at_the_measured_voltage():
    # defaultVoltage set to the 400 V line-to-line voltage while the TWCs
    # measure 240 V per phase: at defaultVoltage the 48 A wiring would budget
    # 80 A of TWC current. Two TWCs together stay within the wiring
    twcs = []
    for _ in range(2):
        twc = Gen2TWC.__new__(Gen2TWC)
        twc.voltsPhaseA = twc.voltsPhaseB = twc.voltsPhaseC = 240
        twcs.append(twc)
    evses = []
    for twc in twcs:
        e = _evse()
        e.setTargetPower.side_effect = twc.setTargetPower
        evses.append(e)
    config = dict(THREE_PHASE_400, wiringMaxAmpsAllTWCs=48)
    _master(evses, 80, twcs, config).distributeEVSEPower()
    assert sum(t.pointAmps for t in twcs) <= 48
