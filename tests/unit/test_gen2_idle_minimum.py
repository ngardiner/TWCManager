# tests/unit/test_gen2_idle_minimum.py
#
# An idle Gen2 TWC is offered its minimum so it can detect a plug-in. The
# minimum the distributor budgets (point.minPower) must be what the real
# heartbeat sends: TWCSlave zeroes any offer below minAmpsToOffer() while no
# car charges, so a lower minimum would go out as 0 A.
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
    m.queue_vehicle_command = Mock()
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


def test_idle_twc_is_offered_min_amps_per_twc_on_the_wire():
    # The shipped config's minAmpsPerTWC is 12
    m, iface = real(minAmps=12)
    s = idleSlave(m)
    m.distributeEVSEPower()
    assert s._evseTargetAmps == 12
    assert point(m, s).minPower == s.convertAmpsToWatts(12, s.currentVoltage)
    assert all(amps > 0 for amps in offers(s, iface)), "zeroed on the wire"
    assert set(offers(s, iface, spike=False)) == {12}


def test_range_override_is_what_the_heartbeat_sends():
    # A per-TWC minimum below minAmpsPerTWC is honoured on the wire too
    m, iface = real(minAmps=12, ranges={"8244": {"minAmps": 8}})
    s = idleSlave(m)
    assert s.minAmps == 8 and s.minAmpsToOffer() == 8
    m.distributeEVSEPower()
    assert s._evseTargetAmps == 8
    assert point(m, s).minPower == s.convertAmpsToWatts(8, s.currentVoltage)
    assert all(amps > 0 for amps in offers(s, iface)), "zeroed on the wire"
    assert set(offers(s, iface, spike=False)) == {8}


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
        SimpleNamespace(VIN=VIN, atHome=True, syncSource="TeslaAPI", pluggedIn=None),
    )
    v.observe("chargingState", "Charging", "TeslaAPI")
    idle = idleSlave(m)
    m.distributeEVSEPower()
    return busy._evseTargetAmps, idle._evseTargetAmps


def test_idle_minimum_does_not_starve_a_charging_twc():
    # One TWC charging, one idle, minAmpsPerTWC 12: the charging car keeps
    # its full offer; the idle TWC gets its minimum only from what is left
    assert chargingAndIdle(feed=40) == (32, 0)
    assert chargingAndIdle(feed=50) == (32, 12)
