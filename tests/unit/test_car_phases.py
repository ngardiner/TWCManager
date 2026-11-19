"""A three-phase charging point serving a car that draws on one phase prices
its amps at one phase's voltage: its load is not overstated threefold, and a
watts target asks the car for the amps that deliver it."""

import pytest

from TWCManager.Vehicle.Phases import carPhases, maskPhases
from tests.unit.reconcile_helpers import gen2Slave
from tests.unit.test_limit_reconciler import VIN, setup


def threePhaseTwc(m, amps=16.0):
    slave = gen2Slave(m, "8244", amps=amps)
    slave.voltsPhaseA = slave.voltsPhaseB = slave.voltsPhaseC = 230
    slave.currentVIN = VIN
    m.vehicleRegistry.observeTwcVin("8244", VIN)
    m.getRealPowerFactor = lambda amps: 1
    return slave


def report(v, **fields):
    for field, value in fields.items():
        v.observe(field, value, "TeslaBLE")


def test_a_single_phase_car_on_a_three_phase_twc_is_priced_on_one_phase():
    m, r, v, c = setup()
    slave = threePhaseTwc(m)
    report(v, phases=1)
    assert slave.currentVoltage == [230, 0, 0]
    assert slave.getCurrentChargerLoad() == pytest.approx(16 * 230)
    assert m.getChargerLoad() == pytest.approx(16 * 230)
    # A 3.68 kW target is 16 A on one phase, not 5.3 A
    assert slave.convertWattsToAmps(16 * 230) == pytest.approx(16)
    assert slave.maxPower == pytest.approx(slave.maxOfferAmps * 230)


def test_a_three_phase_car_or_an_unknown_one_keeps_every_phase():
    m, r, v, c = setup()
    slave = threePhaseTwc(m)
    assert slave.currentVoltage == [230, 230, 230]
    report(v, phases=3)
    assert slave.currentVoltage == [230, 230, 230]


def test_a_phase_count_read_before_the_car_last_left_is_ignored():
    m, r, v, c = setup()
    slave = threePhaseTwc(m)
    report(v, phases=1)
    c.t += 10
    v.mark("driveStart", c.t)
    c.t += 10
    assert carPhases(v) is None and slave.currentVoltage == [230, 230, 230]


def test_power_over_voltage_and_current_outranks_a_reported_two():
    # charger_phases has read 2 for a car drawing on three
    m, r, v, c = setup()
    report(v, phases=2, chargerPower=11, voltage=230, actualCurrent=16)
    assert carPhases(v) == 3
    report(v, phases=3, chargerPower=4, voltage=230, actualCurrent=16)
    assert carPhases(v) == 1


def test_a_reported_two_alone_or_too_little_current_says_nothing():
    m, r, v, c = setup()
    report(v, phases=2)
    assert carPhases(v) is None
    # 0.6 kW rounds to 1 kW: at 5 A of 120 V that reads as two phases
    report(v, phases=1, chargerPower=1, voltage=120, actualCurrent=5)
    assert carPhases(v) == 1


@pytest.mark.parametrize(
    "voltage,phases,expected",
    [
        ([230, 230, 230], 1, [230, 0, 0]),
        ([230, 230, 230], None, [230, 230, 230]),
        ([240, 0, 0], 3, [240, 0, 0]),
        ([0, 230, 230], 1, [0, 230, 0]),
    ],
)
def test_mask_keeps_the_first_live_phases(voltage, phases, expected):
    assert maskPhases(voltage, phases) == expected


def test_a_gen3_unit_asks_a_single_phase_car_for_one_phases_amps():
    from tests.unit.test_gen3twc_provider import VIN1, make, vitals

    m, ctl, p = make()
    (u,) = ctl.devices()
    u._phases = 3
    car = m.vehicleRegistry.getOrCreate(VIN1, "TeslaMateVehicle")
    car.observe("pluggedIn", True, "TeslaMateVehicle")
    vitals(u, True, 0.0)
    assert u.vehicleKey == VIN1 and u.currentVoltage == [240, 240, 240]
    car.observe("phases", 1, "TeslaMateVehicle")
    assert u.currentVoltage == [240, 0, 0]
    ctl.setUnitTarget(u.ID, 16 * 240)
    # The headroom serves 16 A on its one phase, not 16 / 3
    assert u.fuseAmps - ctl.solve() == pytest.approx(16)


def gen3Vitals(unit, a, b, c, phases=3):
    import json
    from unittest.mock import MagicMock, patch

    unit._phases = phases
    body = {"vehicle_connected": True, "vehicle_current_a": max(a, b, c), "grid_v": 230.0,
            "evse_state": "charging", "currentA_a": a, "currentB_a": b, "currentC_a": c}
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(body).encode()
    with patch("urllib.request.urlopen", return_value=resp):
        assert unit.refreshVitals() is True


def gen3():
    from tests.unit.test_gen3twc_provider import make

    m, ctl, p = make()
    (u,) = ctl.devices()
    return m, ctl, u


@pytest.mark.parametrize(
    "a,b,c,expected",
    [
        (16.0, 0.3, 0.2, [230, 0, 0]),       # a car the unit cannot name, on one phase
        (16.0, 15.8, 16.1, [230, 230, 230]),
        (0.0, 16.0, 0.0, [0, 230, 0]),       # the phase that carries it, not the first
        (16.0, 16.0, 0.2, [230, 230, 0]),    # a two-phase charger
        (0.2, 0.4, 0.2, [230, 230, 230]),    # idle readings say nothing
    ],
)
def test_a_three_phase_gen3_counts_the_phases_it_measures_carrying_current(a, b, c, expected):
    m, ctl, u = gen3()
    gen3Vitals(u, a, b, c)
    assert u.currentVoltage == expected


def test_a_single_phase_gen3_is_never_measured():
    # 240 V split-phase carries one phase's current on two legs
    m, ctl, u = gen3()
    gen3Vitals(u, 32.0, 32.0, 0.0, phases=1)
    assert u.drawingPhases() is None and u.currentVoltage == [230, 0, 0]


def test_the_measured_phases_outrank_the_cars_report():
    from tests.unit.test_gen3twc_provider import VIN1

    m, ctl, u = gen3()
    car = m.vehicleRegistry.getOrCreate(VIN1, "TeslaMateVehicle")
    car.observe("pluggedIn", True, "TeslaMateVehicle")
    gen3Vitals(u, 0.2, 0.3, 0.2)
    car.observe("phases", 1, "TeslaMateVehicle")
    assert u.vehicleKey == VIN1 and u.currentVoltage == [230, 0, 0]    # idle: the car says
    gen3Vitals(u, 16.0, 16.0, 16.0)
    assert u.currentVoltage == [230, 230, 230]                         # drawing: the unit says
