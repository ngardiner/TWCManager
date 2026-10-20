from unittest.mock import Mock

import pytest

from TWCManager.TWCMaster import TWCMaster
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Gen2TWCProvider import Gen2TWCProvider
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import CHARGING

VIN = "5YJ3E1EA1KF000001"
CONFIG = {
    "config": {
        "debugOutputToFile": False,
        "subtractChargerLoad": False,
        "treatGenerationAsGridDelivery": False,
        "wiringMaxAmpsAllTWCs": 80,
        "wiringMaxAmpsPerTWC": 32,
        "maxAmpsAllowedFromGrid": None,
        "minAmpsPerTWC": 6,
    }
}


def make(config=CONFIG):
    m = TWCMaster(b"\x77\x77", config)  # bytes, as the launcher passes it
    m.getModuleByName = Mock(return_value=None)
    s = m.newSlave(b"\x82\x44", 80)  # slaveTWCs is keyed by bytes
    return m, s


def withMinAmpsPerTWC(amps):
    return dict(CONFIG, config=dict(CONFIG["config"], minAmpsPerTWC=amps))


def test_master_registers_gen2_provider_and_read_order():
    m, s = make()
    assert [p.name for p in m.providerOrder.providers()] == ["Gen2TWC"]
    assert isinstance(m.providerOrder.provider("Gen2TWC"), Gen2TWCProvider)
    assert m.vehicleRegistry.providerOrder is m.providerOrder


def test_ranges_follow_the_device_and_wiring():
    m, s = make(withMinAmpsPerTWC(5))
    s.minAmpsTWCSupports = 5
    assert (s.minAmps, s.maxOfferAmps) == (5, 32)
    s.minAmpsTWCSupports = 6  # never below what the TWC supports
    assert s.minAmps == 6
    s.maxAmps = 24
    assert s.maxOfferAmps == 24
    m2, s2 = make(
        dict(
            CONFIG,
            **{"controller.Gen2TWCs": {"ranges": {"8244": {"minAmps": 6, "maxAmps": 16}}}}
        )
    )
    assert (s2.minAmps, s2.maxOfferAmps) == (6, 16) and s2.minPower == 6 * 240
    assert s2.maxPower == 16 * 240


def test_vehicle_key_keeps_last_vin_while_plugged_state():  # ledger I5
    m, s = make()
    reg = m.vehicleRegistry
    assert s.vehicleKey is None and s.occupied is None
    s.currentVIN = VIN.lower()
    assert s.vehicleKey == VIN and s.occupied is True
    s.currentVIN = ""
    s.lastVIN = VIN
    s.reportedState = 0x03
    assert s.vehicleKey == VIN and s.occupied is True
    s.reportedState = 0x00
    assert s.vehicleKey is None and s.occupied is None
    reg.beginPlugSession("8244")
    assert s.vehicleKey == "anon:8244"
    assert s.twcid == "8244"


def test_provider_readiness_and_range_per_vehicle():
    m, s = make(withMinAmpsPerTWC(5))
    p = m.providerOrder.provider("Gen2TWC")
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    assert p.ready(v, C.SET_AMPS) is False and p.ampsRange(v) is None
    s.currentVIN = VIN
    assert p.ready(v, C.SET_AMPS) and p.ampsRange(v) == (6, 32)
    assert p.covers(v, 6) and not p.covers(v, 4)
    s.minAmpsTWCSupports = 5
    assert p.ampsRange(v) == (5, 32) and p.covers(v, 5)
    m.settings["chargeRateControl"] = "2"
    assert p.ready(v, C.SET_AMPS) is False


# Mode 3 hands the rate to the vehicle API below 6 A (TWCSlave's literal
# handoff), and the TWC never offers below its own minimum, so the provider
# covers a target exactly when TWCSlave carries it itself
@pytest.mark.parametrize(
    "minAmpsPerTWC, supports",
    [
        (6, 6),  # protocol 2
        (5, 5),  # protocol 1: TWCSlave hands 5 A to the API
        (6, 1),  # 3-phase
        (1, 1),  # 3-phase, minAmpsPerTWC 1
    ],
)
def test_mode_3_floor_is_where_the_twc_carries_the_rate(minAmpsPerTWC, supports):
    m, s = make(withMinAmpsPerTWC(minAmpsPerTWC))
    p = m.providerOrder.provider("Gen2TWC")
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    s.currentVIN = VIN
    s.minAmpsTWCSupports = supports
    # Mode 1 keeps the plain range: the floor the heartbeat offers
    assert p.ampsRange(v) == (max(minAmpsPerTWC, supports), 32)
    m.settings["chargeRateControl"] = "3"
    assert p.ampsRange(v) == (6, 32)
    assert p.covers(v, 6) and not p.covers(v, 5)


def test_min_amps_to_offer():
    m, s = make(withMinAmpsPerTWC(5))
    s.minAmpsTWCSupports = 6
    assert s.minAmpsToOffer() == 6
    s.minAmpsTWCSupports = 1
    assert s.minAmpsToOffer() == 5
    # Without minAmpsPerTWC the default config's 6 applies
    config = dict(CONFIG, config=dict(CONFIG["config"]))
    del config["config"]["minAmpsPerTWC"]
    m2, s2 = make(config)
    s2.minAmpsTWCSupports = 1
    assert s2.minAmpsToOffer() == 6


@pytest.mark.parametrize("mode", ["1", "3"])
def test_amps_range_never_none_while_set_amps_ready(mode):
    # A None range while ready would read as "no local path" and bill Fleet
    m, s = make()
    p = m.providerOrder.provider("Gen2TWC")
    m.settings["chargeRateControl"] = mode
    known = m.vehicleRegistry.getOrCreate(VIN, "t")
    assert p.ready(known, C.SET_AMPS) is False and p.ampsRange(known) is None
    anon = m.vehicleRegistry.beginPlugSession("8244")
    assert p.ready(anon, C.SET_AMPS) and p.ampsRange(anon) is not None
    m.vehicleRegistry.endPlugSession("8244")
    s.lastVIN = VIN
    readyStates = []
    for state in (0x00, 0x01, 0x03):
        s.reportedState = state
        if p.ready(known, C.SET_AMPS):
            readyStates.append(state)
            assert p.ampsRange(known) is not None
        else:
            assert p.ampsRange(known) is None
    assert readyStates == [0x01, 0x03]
    s.currentVIN = VIN
    assert p.ready(known, C.SET_AMPS) and p.ampsRange(known) is not None


def test_disruptive_stop_declared_very_high_cost_and_off_by_default():
    m, s = make()
    p = m.providerOrder.provider("Gen2TWC")
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    s.currentVIN = VIN
    assert p.cost(C.STOP_CHARGING) == C.DISRUPTIVE and p.ready(v, C.STOP_CHARGING) is False
    assert p.stopCharging(VIN) == O.Outcome(O.UNSUPPORTED, "mode 1")
    m.settings["chargeStopMode"] = "2"
    assert p.ready(v, C.STOP_CHARGING) and p.stopCharging(VIN) == O.Outcome(
        O.DONE, "respond"
    )
    assert m.settings["respondToSlaves"] == 0
    m.settings["respondToSlaves"] = 1
    m.stopCarsCharging(VIN)
    # Desired state only: the reconciler delivers it through this provider
    assert m.desired.get(VIN, CHARGING).value is False
    assert m.settings["respondToSlaves"] == 1


def test_mode_3_stop_is_queued_for_the_serial_loop():
    m, s = make()
    p = m.providerOrder.provider("Gen2TWC")
    s.currentVIN = VIN
    m.settings["chargeStopMode"] = "3"
    m.getModulesByType = Mock(
        side_effect=AssertionError("no write off the serial thread")
    )
    # Queued for the serial loop; nothing is written here
    assert p.stopCharging(VIN) == O.Outcome(O.DONE, "queued")
    assert [msg[:2] for msg in m._outbox] == [b"\xfc\xb2"]
