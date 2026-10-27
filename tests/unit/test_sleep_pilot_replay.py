# A car asleep on a Gen2 TWC that holds its 6 A sleep pilot, read over BLE
# under that pilot, must still be offered its share and woken for Scheduled
# Charging. On our own TWC the pilot is our last offer, not the car's limit.
import pytest

from TWCManager.Vehicle.Reconcile import Belief as B
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING
from tests.unit.test_limit_reconciler import VIN, ble, cmds, prov, setup

WIRING = dict(wiringMaxAmpsAllTWCs=48, wiringMaxAmpsPerTWC=48, minAmpsPerTWC=6)


def test_a_car_read_under_the_sleep_pilot_is_offered_its_share_and_woken(caplog):
    caplog.set_level("INFO")
    # A restart: the registry holds nothing of the car but what follows
    m, r, v, c = setup(ble(O.Outcome(O.DONE), O.Outcome(O.DONE)), here=False)
    m.config["config"].update(WIRING)
    m.settings["chargeRateControl"] = 1
    (s,) = m.getSlaveTWCs()
    s.protocolVersion, s.minAmpsTWCSupports, s.voltsPhaseA = 2, 6, 243
    s.heardFrom = True
    # The slave was made before the box's wiring was set
    s.config, s.configConfig, s.wiringMaxAmps = m.config, m.config["config"], 48
    # The car's BLE read while the TWC held its 6 A sleep pilot: not
    # charging, so its voltage reads near 0 and is priced at the default
    for field, value in (
        ("availableCurrent", 6),
        ("voltage", 2),
        ("chargingState", "Stopped"),
        ("chargeLimit", 50),
        ("batteryLevel", 50),
        ("pluggedIn", True),
    ):
        v.observe(field, value, "TeslaBLE")
    # The car goes back to sleep and the TWC names it at 03 with 6 A
    v.observe("state", "asleep", "TeslaBLE")
    v.observe("twcVin", s.twcid, "TWC")
    s.reportedState, s.reportedAmpsMax = 0x03, 6.0
    # Midnight: Scheduled Charging's 48 A and its 70 % limit
    m.setMaxAmpsToDivideAmongSlaves(48)
    m.controlPass(now=c())
    assert s.pointAmps == pytest.approx(48)
    assert s.offer.amps == 48 and m.offered[VIN] > 0
    assert m.desired.get(VIN, CHARGING).value is True  # the start
    m.desired.set(VIN, CHARGE_LIMIT, 70, "policy")
    assert B.whyQuiet(m, v) is None  # the raise may wake the car
    r.pass_()
    assert "setChargeLimit" in cmds(prov(m, "TeslaBLE"))
    assert "TeslaBLE wake for %s before setChargeLimit: done" % VIN in [
        x.getMessage() for x in caplog.records
    ]


def test_a_car_left_at_a_lowered_request_is_lifted_above_its_own_maximum():
    # A 32 A car on 48 A wiring: the lift to the wiring maximum still goes
    # to the car, which clamps it to its own 32 A
    m, r, v, c = setup(ble(O.Outcome(O.DONE)), here=False)
    m.config["config"].update(WIRING)
    m.settings["chargeRateControl"] = 1
    (s,) = m.getSlaveTWCs()
    s.protocolVersion, s.minAmpsTWCSupports, s.heardFrom = 2, 6, True
    s.config, s.configConfig, s.wiringMaxAmps = m.config, m.config["config"], 48
    for field, value in (
        ("chargeCurrentRequestMax", 32),
        ("chargeCurrentRequest", 10),
        ("chargingState", "Stopped"),
        ("pluggedIn", True),
    ):
        v.observe(field, value, "TeslaBLE")
    v.observe("twcVin", s.twcid, "TWC")
    s.reportedState, s.reportedAmpsMax = 0x03, 6.0
    m.setMaxAmpsToDivideAmongSlaves(48)
    m.controlPass(now=c())
    assert m.desired.get(VIN, AMPS).lift
    r.pass_()
    assert ("setAmps", VIN, 48) in prov(m, "TeslaBLE").calls
