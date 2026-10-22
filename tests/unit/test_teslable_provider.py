# tests/unit/test_teslable_provider.py
from unittest.mock import Mock
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Reconcile import Outcome as O
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
    ble.sendCommand = Mock(return_value="Command executed successfully")
    assert p.setAmps(VIN.lower(), 12.6).kind == O.DONE
    ble.sendCommand.assert_called_once_with(VIN, "charging-set-amps", 12)
    ble.sendCommand = Mock(return_value=None)
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "no reply")
    ble.sendCommand.assert_called_once_with(VIN, "wake")


def test_read_readiness_does_not_consult_command_policy():
    ble, reg, master = make()
    v = reg.getOrCreate(VIN, "t")
    ble._claimUnproven(v)
    master.getModuleByName = Mock(
        side_effect=AssertionError("must not look up a module")
    )
    (p,) = ble.providers()
    assert p.ready(v, C.READ_CHARGE) is True


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


def test_reads_order_at_the_local_tier():
    ble, reg, master = make()
    master.settings = {}
    o = ProviderOrder(master)
    for p in ble.providers():
        o.register(p)
    (p,) = o.ordered(C.READ_CHARGE)
    assert p.name == "TeslaBLE" and p.cost(C.READ_CHARGE) == C.LOCAL
