"""
TeslaAPIEVSE must not raise when the Fleet API reports a car as unplugged.

Tesla returns null for charger_voltage and charger_actual_current while the
car is unplugged. TeslaAPI copies them with charge.get(key, default), which
yields None when the key is present with a null value. When a car left its
TWC with a last charging_state of "Stopped", TeslaAPIController listed it,
the distributor asked for maxPower, and currentVoltage compared None < 90.
The TypeError aborted every main-loop pass before the TWC heartbeat was sent.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from TWCManager.EVSEInstance.TeslaAPIEVSE import TeslaAPIEVSE

CONFIG = {"defaultVoltage": 230, "numberOfPhases": 3, "wiringMaxAmpsPerTWC": 16}


def unplugged_vehicle(**overrides):
    # Attribute values as TeslaAPI stores them after a vehicle_data poll of an
    # unplugged car whose last charge session TWCManager stopped.
    fields = dict(
        VIN="5YJSA1E2XFF000001",
        chargingState="Stopped",
        voltage=None,
        actualCurrent=None,
        availableCurrent=None,
        phases=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def make_evse(vehicle):
    master = Mock()
    master.config = {"config": CONFIG}
    return TeslaAPIEVSE(vehicle, Mock(), master)


def test_stopped_unplugged_car_is_offered_power():
    # Why the distributor reaches the power properties at all.
    assert make_evse(unplugged_vehicle()).wantsToCharge


def test_null_voltage_falls_back_to_default():
    assert make_evse(unplugged_vehicle()).currentVoltage == [230, 230, 230]


def test_null_actual_current_reads_as_zero():
    evse = make_evse(unplugged_vehicle())
    assert evse.currentAmps == 0
    assert evse.currentPower == 0


def test_power_limits_for_unplugged_car():
    evse = make_evse(unplugged_vehicle())
    # 5 A minimum and the 16 A wiring limit across three phases at 230 V.
    assert evse.minPower == 5 * 230 * 3
    assert evse.maxPower == 16 * 230 * 3


@pytest.mark.parametrize("voltage", [0, 2, 89])
def test_implausibly_low_voltage_still_falls_back(voltage):
    evse = make_evse(unplugged_vehicle(voltage=voltage))
    assert evse.currentVoltage == [230, 230, 230]


def test_reported_voltage_is_used_while_charging():
    evse = make_evse(
        unplugged_vehicle(
            chargingState="Charging", voltage=240, phases=1, actualCurrent=10
        )
    )
    assert evse.currentVoltage == [240, 0, 0]
    assert evse.currentPower == 10 * 240
