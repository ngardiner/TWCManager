# tests/unit/test_gen2_idle_minimum.py
#
# An idle TWC gets a distributor target of 0 and counts nothing against the
# budget. On the wire TWCSlave offers it 0 when no car charges (a Gen2 treats
# 0 as its hardware minimum) and minAmpsTWCSupports when another car charges
# with enough power per car; these tests use the real TWCMaster and TWCSlave.
from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.EVSEController.VehicleEndpoints import VehicleEndpoints
from TWCManager.TWCMaster import TWCMaster
from tests.unit.test_gen2twc_provider import CONFIG
from tests.unit.test_provider_order import fleet

VIN = "5YJ3E1EA1KF000001"


def real(minAmps, ranges=None, feed=40):
    config = dict(
        CONFIG,
        config=dict(
            CONFIG["config"],
            defaultVoltage=240,
            numberOfPhases=1,
            fakeMaster=1,
            minAmpsPerTWC=minAmps,
            wiringMaxAmpsAllTWCs=80,
            wiringMaxAmpsPerTWC=32,
            displayMilliseconds=False,
        ),
    )
    if ranges:
        config["controller.Gen2TWCs"] = {"ranges": ranges}
    m = TWCMaster(b"\x77\x77", config)
    m.settings["chargeRateControl"] = 1
    m.queue_background_task = Mock()
    carapi = Mock()
    carapi.enabled.return_value = True
    policy = Mock()
    policy.policyIsGreen.return_value = False
    iface = Mock()
    modules = {"TeslaAPI": carapi, "Policy": policy}
    m.getModuleByName = lambda n: modules.get(n)
    m.registerModule(
        {"name": "VehicleEndpoints", "ref": VehicleEndpoints(m), "type": "EVSEController"}
    )
    m.registerModule({"name": "Dummy", "ref": iface, "type": "Interface"})
    m.providerOrder.register(fleet())
    m.getMaxAmpsToDivideAmongSlaves = lambda: feed
    return m, iface


def heartbeat(s, state, maxA, actA):
    mx, ac = int(maxA * 100), int(actA * 100)
    s.receive_slave_heartbeat(
        bytearray([state, mx >> 8, mx & 0xFF, ac >> 8, ac & 0xFF, 0, 0])
    )
    s.send_master_heartbeat()


def wireAmps(iface):
    hb = iface.send.call_args.args[0][6:]
    return ((hb[1] << 8) + hb[2]) / 100


def offers(s, iface, beats=10, spike=True):
    # The wire offers over a run of heartbeats, the clock run past each 5 s
    # step. With the 6 A-limit workaround on, a car that draws nothing is
    # spiked above the offer (pre-existing TWCSlave behaviour); off, the
    # offer itself goes out.
    s.master.settings["spikeAmpsProactively"] = int(spike)
    s.master.settings["spikeAmpsReactively"] = int(spike)
    sent = []
    for _ in range(beats):
        s.timeLastAmpsOfferedChanged -= 10
        heartbeat(s, 0x04, 0, 0)
        sent.append(wireAmps(iface))
    return sent


def idleSlave(m, twcid=b"\x82\x44"):
    s = m.newSlave(twcid, 80)
    s.protocolVersion = 1
    s.reportedState, s.reportedAmpsMax, s.reportedAmpsActual = 0x04, 0, 0
    return s


def point(m, s):
    return next(p for p in m.chargingPoints() if s in p.endpoints)


def test_idle_twc_sends_zero_which_the_twc_treats_as_its_hardware_minimum():
    # minAmpsPerTWC is 12. The distributor's target for an idle TWC is 0 and
    # the heartbeat sends 0 (spike or no spike). A Gen2 TWC treats an offer of
    # 0 as its hardware minimum, so it still sees a car plug in.
    m, iface = real(minAmps=12)
    s = idleSlave(m)
    assert s.minAmpsToOffer() == 12
    m.distributeEVSEPower()
    assert s._evseTargetAmps == 0
    assert point(m, s).minPower == s.convertAmpsToWatts(12, s.currentVoltage)
    assert set(offers(s, iface)) == {0}
    assert set(offers(s, iface, spike=False)) == {0}


def test_idle_twc_offered_6a_on_wire_when_other_car_charges():
    # Another car is charging with at least minAmpsPerTWC of power per car:
    # the idle TWC's target is 0, but the wire offer is minAmpsTWCSupports
    m, iface = real(minAmps=12, feed=40)
    chargingSlave(m, b"\x82\x45", VIN)
    idle = idleSlave(m)
    m.distributeEVSEPower()
    assert idle._evseTargetAmps == 0
    assert idle.minAmpsTWCSupports == 6
    assert set(offers(idle, iface, spike=False)) == {6}


def test_range_override_governs_allocation_not_the_idle_offer():
    m, iface = real(minAmps=12, ranges={"8244": {"minAmps": 8}})
    s = idleSlave(m)
    assert s.minAmps == 8 and s.minAmpsToOffer() == 8
    m.distributeEVSEPower()
    assert s._evseTargetAmps == 0
    assert point(m, s).minPower == s.convertAmpsToWatts(8, s.currentVoltage)


def test_override_never_goes_below_what_the_twc_supports():
    m, iface = real(minAmps=12, ranges={"8244": {"minAmps": 2}})
    s = idleSlave(m)
    s.minAmpsTWCSupports = 6
    assert s.minAmps == 6 and s.minAmpsToOffer() == 6


def test_min_amps_is_min_amps_to_offer_without_an_override():
    m, iface = real(minAmps=12)
    s = idleSlave(m)
    s.minAmpsTWCSupports = 6
    assert s.minAmps == 12 == s.minAmpsToOffer()
    s.minAmpsTWCSupports = 16
    assert s.minAmps == 16 == s.minAmpsToOffer()


def chargingAndIdle(feed):
    m, iface = real(minAmps=12, feed=feed)
    busy = m.newSlave(b"\x82\x45", 80)
    busy.protocolVersion = 1
    busy.currentVIN = VIN
    busy.reportedState, busy.reportedAmpsMax, busy.reportedAmpsActual = 0x01, 32, 20
    v = m.vehicleRegistry.getOrCreate(VIN, "TeslaAPI")
    v.attach(
        "TeslaAPI",
        SimpleNamespace(VIN=VIN, atHome=True, syncSource="TeslaAPI", pluggedIn=None, locate=lambda: None),
    )
    v.observe("chargingState", "Charging", "TeslaAPI")
    idle = idleSlave(m)
    m.distributeEVSEPower()
    return busy._evseTargetAmps, idle._evseTargetAmps


def test_idle_minimum_does_not_starve_a_charging_twc():
    # One TWC charging, one idle, minAmpsPerTWC 12: the charging car keeps
    # its full offer, whatever the budget; the idle TWC's offer (0, its
    # hardware minimum on the wire) takes nothing from it
    assert chargingAndIdle(feed=40) == (32, 0)
    assert chargingAndIdle(feed=50) == (32, 0)


def test_start_request_follows_the_range_override():
    # Concern rulings, item 3: a TWC offering its override minimum (8 A,
    # below minAmpsPerTWC 12) still asks a paused car to start
    m, iface = real(minAmps=12, ranges={"8244": {"minAmps": 8}})
    s = idleSlave(m)
    s.protocolVersion = 2
    s.currentVIN = "5YJ3E1EA1KF000001"
    m.startCarsCharging = Mock()
    s.lastAmpsOffered = 8
    s.send_master_heartbeat()
    m.startCarsCharging.assert_called_once_with("5YJ3E1EA1KF000001")
    s.lastAmpsOffered = 7
    s.send_master_heartbeat()
    assert m.startCarsCharging.call_count == 1


def chargingSlave(m, twcid, vin):
    s = m.newSlave(twcid, 80)
    s.protocolVersion = 1
    s.currentVIN = vin
    s.reportedState, s.reportedAmpsMax, s.reportedAmpsActual = 0x01, 32, 20
    v = m.vehicleRegistry.getOrCreate(vin, "TeslaAPI")
    v.attach(
        "TeslaAPI",
        SimpleNamespace(VIN=vin, atHome=True, syncSource="TeslaAPI", pluggedIn=None, locate=lambda: None),
    )
    v.observe("chargingState", "Charging", "TeslaAPI")
    return s


def test_idle_minimum_is_not_counted_against_the_budget():
    # One car takes the whole 32 A budget; the idle TWC neither blocks nor
    # shrinks that, and is not gated on what is left
    m, iface = real(minAmps=12, feed=32)
    busy = chargingSlave(m, b"\x82\x45", VIN)
    idle = idleSlave(m)
    m.distributeEVSEPower()
    assert (busy._evseTargetAmps, idle._evseTargetAmps) == (32, 0)


def test_idle_minimum_ignores_the_controller_budget():
    m, iface = real(minAmps=12, feed=200)
    m.config["config"]["wiringMaxAmpsAllTWCs"] = 33
    busy = chargingSlave(m, b"\x82\x45", VIN)
    idle = idleSlave(m)
    m.distributeEVSEPower()
    assert (busy._evseTargetAmps, idle._evseTargetAmps) == (32, 0)


def test_idle_twc_gets_zero_when_policy_budget_is_zero():
    m, iface = real(minAmps=12, feed=0)
    idle = idleSlave(m)
    m.distributeEVSEPower()
    assert idle._evseTargetAmps == 0


def test_next_cycle_rebalances_when_the_idle_twcs_car_wants_power():
    m, iface = real(minAmps=12, feed=32)
    busy = chargingSlave(m, b"\x82\x45", VIN)
    idle = idleSlave(m)
    m.distributeEVSEPower()
    assert busy._evseTargetAmps + idle._evseTargetAmps == 32
    # The idle TWC's car plugs in and wants power
    idle.currentVIN = "5YJ3E1EA1KF000002"
    idle.reportedState, idle.reportedAmpsMax, idle.reportedAmpsActual = 0x01, 32, 20
    v = m.vehicleRegistry.getOrCreate(idle.currentVIN, "TeslaAPI")
    v.attach(
        "TeslaAPI",
        SimpleNamespace(
            VIN=idle.currentVIN, atHome=True, syncSource="TeslaAPI", pluggedIn=None,
            locate=lambda: None,
        ),
    )
    v.observe("chargingState", "Charging", "TeslaAPI")
    m.distributeEVSEPower()
    assert busy._evseTargetAmps == idle._evseTargetAmps == 16
    assert busy._evseTargetAmps + idle._evseTargetAmps <= 32


def test_charging_point_below_min_amps_per_twc_gets_a_stop():
    # A 10 A budget is under minAmpsPerTWC 12: the charging TWC is offered 0,
    # not a partial amount
    m, iface = real(minAmps=12, feed=10)
    busy = chargingSlave(m, b"\x82\x45", VIN)
    m.distributeEVSEPower()
    assert busy._evseTargetAmps == 0
    # Two cars, 20 A: the 10 A fair share is under the floor, so that car is
    # stopped and the other takes the rest, never a partial amount
    m, iface = real(minAmps=12, feed=20)
    a = chargingSlave(m, b"\x82\x45", VIN)
    b = chargingSlave(m, b"\x82\x46", "5YJ3E1EA1KF000002")
    m.distributeEVSEPower()
    assert sorted([a._evseTargetAmps, b._evseTargetAmps]) == [0, 20]
