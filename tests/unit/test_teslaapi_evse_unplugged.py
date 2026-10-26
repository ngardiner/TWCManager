"""
A vehicle endpoint must not raise when the Fleet API reports a car as unplugged.

Tesla returns null for charger_voltage and charger_actual_current while the
car is unplugged, and TeslaAPI records them as None. When a car left its TWC
with a last charging_state of "Stopped", the distributor still listed it and
asked for maxPower. In 1.4.0 currentVoltage compared None < 90, and the
TypeError aborted every main-loop pass before the TWC heartbeat was sent
(#713). These cases now run against VehicleEndpoint, which replaced
TeslaAPIEVSE and no longer reports the car's current at all.
"""

import pytest

from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint
from tests.unit.test_provider_order import fleet
from tests.unit.test_vehicle_endpoint import VIN1, car, master


def endpoint(**values):
    m = master({}, fleet())
    m.config = {
        "config": {
            "defaultVoltage": 230,
            "numberOfPhases": 3,
            "wiringMaxAmpsPerTWC": 16,
        }
    }
    v = car(m, VIN1)
    # Values as TeslaAPI records them after a poll of an unplugged car whose
    # last charge session TWCManager stopped
    fields = dict(
        chargingState="Stopped",
        voltage=None,
        actualCurrent=None,
        availableCurrent=None,
        phases=None,
    )
    fields.update(values)
    for key, value in fields.items():
        v.observe(key, value, "TeslaAPI")
    return VehicleEndpoint(v, m.providerOrder, m)


def test_stopped_unplugged_car_is_offered_power():
    # Why the distributor reaches the power properties at all.
    assert endpoint().wantsToCharge


def test_null_voltage_falls_back_to_default():
    assert endpoint().currentVoltage == [230, 230, 230]


def test_power_limits_for_unplugged_car():
    e = endpoint()
    # 5 A minimum and the 16 A wiring limit across three phases at 230 V.
    assert e.minPower == 5 * 230 * 3
    assert e.maxPower == 16 * 230 * 3


@pytest.mark.parametrize("voltage", [0, 2, 89])
def test_implausibly_low_voltage_still_falls_back(voltage):
    assert endpoint(voltage=voltage).currentVoltage == [230, 230, 230]


def test_reported_voltage_is_used_while_charging():
    e = endpoint(chargingState="Charging", voltage=240, phases=1, actualCurrent=10)
    assert e.currentVoltage == [240, 0, 0]
    assert e.maxPower == 16 * 240
