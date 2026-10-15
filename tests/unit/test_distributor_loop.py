"""distributeEVSEPower visits every EVSE and water-fills in ascending maxPower."""

from unittest.mock import MagicMock

from TWCManager.TWCMaster import TWCMaster


def _evse(max_power, min_power=1200):
    e = MagicMock()
    e.wantsToCharge = True
    e.isReadOnly = False
    e.maxPower = max_power
    e.minPower = min_power
    e.controllers = ["Gen2TWCs"]
    return e


def _master(evses, amps):
    m = TWCMaster.__new__(TWCMaster)
    m.config = {"config": {"defaultVoltage": 240, "numberOfPhases": 1}}
    ctrl = MagicMock()
    ctrl.maxPower = 100000
    # A non-Gen2 controller makes the distributor run.
    m.getModulesByType = lambda t: [
        {"name": "Gen2TWCs", "ref": ctrl},
        {"name": "VehicleEndpoints", "ref": ctrl},
    ]
    m.getDedupedEVSEs = lambda: evses
    m.getMaxAmpsToDivideAmongSlaves = lambda: amps
    return m


def _targets(evses):
    for e in evses:
        e.setTargetPower.assert_called_once()
    return [e.setTargetPower.call_args.args[0] for e in evses]


def test_distributor_visits_every_evse():
    evses = [_evse(7000 + i) for i in range(3)]
    _master(evses, 100).distributeEVSEPower()
    for e in evses:
        e.setTargetPower.assert_called_once()


def test_water_fill_enough_for_all():
    # 24000 W: 3000 (cap), then 21000/2 -> 5000 (cap), then 16000 -> 7000 (cap)
    evses = [_evse(7000), _evse(3000), _evse(5000)]
    _master(evses, 100).distributeEVSEPower()
    assert _targets(evses) == [7000, 3000, 5000]


def test_water_fill_shares_split_when_scarce():
    # 12000 W: 3000 (cap), then 9000/2 = 4500 (< 5000 cap), then 4500
    evses = [_evse(3000), _evse(5000), _evse(7000)]
    _master(evses, 50).distributeEVSEPower()
    assert _targets(evses) == [3000, 4500, 4500]


def test_below_minimums_small_evses_get_zero_last_gets_rest():
    # 2000 W: 666 and 1000 are below the 1500 W minimum -> 0; the last gets 2000
    evses = [_evse(3000, 1500), _evse(3001, 1500), _evse(3002, 1500)]
    m = _master(evses, 2000 / 240)
    m.distributeEVSEPower()
    t = _targets(evses)
    assert t[0] == 0 and t[1] == 0
    assert abs(t[2] - 2000) < 1e-6
