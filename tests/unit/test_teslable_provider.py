# tests/unit/test_teslable_provider.py
from unittest.mock import Mock
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from tests.unit.test_teslable_pairing_status import make, VIN


def test_provider_is_local_free_and_ready_for_claimed_vins():
    ble, reg, master = make()
    (p,) = ble.providers()
    assert (p.name, p.local, p.cost(C.SET_AMPS), p.module) == (
        "TeslaBLE",
        True,
        C.LOCAL,
        ble,
    )
    v = reg.getOrCreate(VIN, "t")
    assert p.ready(v, C.SET_AMPS) is False
    ble._claimUnproven(v)
    assert p.ready(v, C.SET_AMPS) and p.ready(v, C.READ_CHARGE)
    v.dropClaim("TeslaBLE")
    assert p.ready(v, C.WAKE) is False
    assert p.ready(reg.beginPlugSession("8244"), C.SET_AMPS) is False


def test_amps_range_is_one_to_vehicle_max():
    ble, reg, master = make()
    (p,) = ble.providers()
    v = reg.getOrCreate(VIN, "t")
    master.config = {"config": {"wiringMaxAmpsPerTWC": 48}}
    assert p.ampsRange(v) == (1, 48)
    # A car read under a 6 A pilot can still be lifted to the wiring max
    v.observe("availableCurrent", 6, "TeslaBLE")
    assert p.ampsRange(v) == (1, 48) and p.covers(v, 48)
    v.observe("chargeCurrentRequestMax", 16, "TeslaBLE")
    assert p.ampsRange(v) == (1, 16)


def test_a_vehicle_rate_above_the_cars_maximum_still_reaches_it():
    # The car clamps a lift to its own maximum; the range only steers a
    # rate the local rule places
    ble, reg, master = make()
    (p,) = ble.providers()
    order = ProviderOrder(master)
    order.register(p)
    v = reg.getOrCreate(VIN, "t")
    ble._claimUnproven(v)
    master.config = {"config": {"wiringMaxAmpsPerTWC": 48}}
    v.observe("chargeCurrentRequestMax", 32, "TeslaBLE")
    assert order.candidates(v, C.SET_AMPS, amps=48, localRule=False) == [p]
    assert order.candidates(v, C.SET_AMPS, amps=48) == []


def test_commands_call_the_module_by_vin():
    ble, reg, master = make()
    (p,) = ble.providers()
    ble.setChargeRate = Mock(return_value=True)
    ble.wakeVehicle = Mock(return_value=False)
    assert p.setAmps(VIN, 12.6) is True
    ble.setChargeRate.assert_called_once_with(12, VIN)
    assert p.wake(VIN) is False
    ble.wakeVehicle.assert_called_once_with(VIN)


def test_read_source_no_longer_consults_command_policy():
    ble, reg, master = make()
    v = reg.getOrCreate(VIN, "t")
    ble._claimUnproven(v)
    master.getModuleByName = Mock(
        side_effect=AssertionError("must not look up VehiclePriority")
    )
    assert ble.readSource.canRead(v) is True and ble.readSource.provider == "TeslaBLE"


def test_amps_range_is_never_none_while_ready():  # Task 2 review constraint
    # A None range would read as "no local path" and let Fleet bill
    ble, reg, master = make()
    (p,) = ble.providers()
    v = reg.getOrCreate(VIN, "t")
    ble._claimUnproven(v)
    assert p.ready(v, C.SET_AMPS) is True
    assert p.ampsRange(v) == (1, 32)  # no wiring max configured
    v.observe("chargeCurrentRequestMax", 0, "TeslaBLE")
    assert p.ampsRange(v) == (1, 32)  # 0 falls through to the wiring max
    assert p.covers(v, 1)
    master.config = {}
    assert p.ampsRange(v) == (1, 32)


def test_read_source_orders_at_the_local_tier():
    ble, reg, master = make()
    master.settings = {}
    o = ProviderOrder(master)
    for p in ble.providers():
        o.register(p)
    other = Mock(provider="", order=0)
    assert o.orderReadSources([other, ble.readSource]) == [ble.readSource, other]
