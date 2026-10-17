# tests/unit/test_control_tick.py: a ~1 s control tick runs the one
# distributor for every install with a Gen2 slave; flex is gone (Phase 4,
# Task 3). Real TWCMaster and Gen2TWC throughout.
import logging
import re
import time
from pathlib import Path
from unittest.mock import Mock, patch

from TWCManager.ControlTick import ControlTick
from TWCManager.EVSEInstance.ChargingPoint import ChargingPoint
from TWCManager.TWCMaster import TWCMaster
from tests.unit.gen2_helpers import tickThenBeat
from tests.unit.test_charging_points import VIN, VIN2, car, real, setAmps
from tests.unit.test_gen2twc_provider import CONFIG
from tests.unit.test_gen3twc_provider import vitals

LAUNCHER = Path(__file__).resolve().parents[2] / "lib" / "TWCManager" / "TWCManager.py"


def target(s):
    return s._evseTargetAmps


def pureGen2(feed, config=None, slaves=2):
    # A pure-Gen2 install: no TeslaAPI module (TeslaAPI disabled), so
    # VehicleEndpoints lists nothing; the slaves are charging and heard from
    cfg = dict(
        CONFIG,
        config=dict(
            CONFIG["config"],
            defaultVoltage=240,
            numberOfPhases=1,
            fakeMaster=1,
            displayMilliseconds=False,
            **(config or {})
        ),
    )
    m = TWCMaster(b"\x77\x77", cfg)
    m.queue_background_task = Mock()
    policy = Mock()
    policy.policyIsGreen.return_value = False
    iface = Mock()
    modules = {"Policy": policy}
    m.getModuleByName = lambda n: modules.get(n)
    m.registerModule({"name": "Dummy", "ref": iface, "type": "Interface"})
    m.getMaxAmpsToDivideAmongSlaves = lambda: feed
    for i in range(slaves):
        charging(m, actual=20.0, twcid=bytes([0x82, 0x44 + i]))
    return m


def charging(m, actual, twcid=b"\x82\x50"):
    s = m.newSlave(twcid, 80)
    s.heardFrom = True
    s.reportedState, s.reportedAmpsMax, s.reportedAmpsActual = 0x01, 32, actual
    return s


def test_teslaapi_disabled_install_uses_the_one_distributor():  # (b)
    m = pureGen2(feed=40)
    assert m.getModuleByName("TeslaAPI") is None
    m.controlPass()
    assert [target(s) for s in m.getSlaveTWCs()] == [20, 20]


def test_flex_settings_are_read_but_ignored():  # (c)
    m = pureGen2(
        feed=4,
        config={"greenEnergyFlexAmps": 3, "useFlexAmpsToStartCharge": True},
        slaves=0,
    )
    s = charging(m, actual=2.0)
    m.controlPass()
    assert target(s) == 0  # minAmpsPerTWC 6: below it is a stop
    assert not hasattr(m, "allowed_flex") and not hasattr(s, "useFlexAmpsToStartCharge")


def test_a_policy_with_allowed_flex_evaluates_without_error():  # (c)
    from TWCManager.Policy.Policy import Policy

    m = pureGen2(feed=0, slaves=0)
    green = {
        "name": "Track Green Energy",
        "match": [],
        "condition": [],
        "charge_amps": "value",
        "value": 10,
        "allowed_flex": 5,
    }
    m.config["policy"] = {"override": [green]}
    m.setMaxAmpsToDivideAmongSlaves = Mock()
    policy = Policy(m)
    policy.enforcePolicy(policy.getPolicyByName("Track Green Energy"))
    m.setMaxAmpsToDivideAmongSlaves.assert_called_once_with(10)
    assert not hasattr(m, "allowed_flex") and not hasattr(m, "setAllowedFlex")


def test_num_cars_charging_now_has_no_side_effects():
    m = pureGen2(feed=40, slaves=0)
    s = m.newSlave(b"\x82\x44", 80)
    m.queue_background_task = Mock()
    s.reportedAmpsActual = 10
    assert m.num_cars_charging_now() == 0
    assert s.isCharging == 0 and not m.queue_background_task.called
    m.observeChargingSessions()  # the serial thread's session tracking
    assert s.isCharging == 1 and m.num_cars_charging_now() == 1
    m.queue_background_task.assert_any_call(
        {"cmd": "getVehicleVIN", "slaveTWC": s.TWCID, "vinPart": 0}
    )


def test_get_status_does_not_start_a_session():  # HTTP thread
    m = pureGen2(feed=40, slaves=0)
    m.getModuleByName("Policy").active_policy = "Non Scheduled Charging"
    s = m.newSlave(b"\x82\x44", 80)
    s.reportedAmpsActual = 10
    assert m.getStatus()["carsCharging"] == 0
    assert s.isCharging == 0 and s.lastChargingStart == 0
    assert not m.queue_background_task.called


def test_restart_with_a_car_charging_never_offers_zero():  # Review Focus 1
    m = pureGen2(feed=40, slaves=0)
    m.settings["chargeRateControl"] = 1
    m.settings["spikeAmpsProactively"] = m.settings["spikeAmpsReactively"] = 0
    s = m.newSlave(b"\x82\x44", 80)  # its linkready
    s.protocolVersion = 2
    s.send_master_heartbeat()
    m.controlPass()  # not heard from yet: no offer
    assert not s.heardFrom and target(s) is None
    iface = tickThenBeat(m, s, 0x01, 40, 32)  # the first reply: charging at 32 A
    assert bytes(iface.send.call_args.args[0][6:15]) == bytes(9)  # no command, never 0 A
    tickThenBeat(m, s, 0x01, 40, 32)
    hb = iface.send.call_args.args[0][6:15]
    assert ((hb[1] << 8) + hb[2]) / 100 == 32


def test_a_gen3_only_install_is_controlled():  # (g) intended fix
    m = real(gen3=True)
    gen3 = m.modules["Gen3TWCs"]["ref"]
    gen3.setUnitTarget = Mock()
    v = car(m, VIN, "Charging")
    v.observe("pluggedIn", True, "TeslaMateVehicle")
    (u,) = gen3.devices()
    vitals(u, True, 10.0)
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    assert m.countSlaveTWC() == 0
    m.controlPass()
    gen3.setUnitTarget.assert_called_once_with(u.ID, 20 * 240)
    assert setAmps(m) == []


def test_a_vehicle_only_install_is_controlled():  # (h) intended fix
    # No Gen2 slave, no Gen3 unit: a car at home on no TWC still gets its rate
    m = real()
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    assert m.countSlaveTWC() == 0 and m.getModulesByType("Interface") == []   # no Gen2 interface
    m.controlPass()
    assert setAmps(m) == [{"vin": VIN, "amps": 20}]


def test_a_gen2_install_with_no_slave_linked_sends_no_amps():  # review I1
    # An RS-485 interface is loaded but no slave has linked (startup, a
    # timed-out slave, a dead adapter): the TWC might serve the car, so the
    # car gets no endpoint and no rate
    m = real()
    m.registerModule({"name": "RS485", "ref": Mock(), "type": "Interface"})
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    for _ in range(3):
        m.controlPass()
    assert setAmps(m) == [] and m.modules["VehicleEndpoints"]["ref"].allEVSEs == []
    s = m.newSlave(b"\x82\x44", 80)  # it links and replies: 00, 0 A
    m.controlPass()
    assert [p.ID for p in m.chargingPoints()] == ["Point-Gen2TWC-8244", "Point-" + VIN]


def test_a_pass_with_nothing_to_distribute_to_does_nothing():
    m = real()
    m.controlPass()
    assert setAmps(m) == [] and m.desired.keys() == []


def test_a_slave_deleted_mid_pass_does_not_break_the_pass():  # Review Focus 3
    m = pureGen2(feed=40)
    first, second = m.getSlaveTWCs()
    original = ChargingPoint.setTargetPower

    def spy(point, watts):
        if m.countSlaveTWC() == 2:
            m.deleteSlaveTWC(second.TWCID)
        return original(point, watts)

    with patch.object(ChargingPoint, "setTargetPower", spy):
        m.controlPass()
    assert m.getSlaveTWCs() == [first] and target(first) is not None


def test_get_slave_twcs_is_a_copy():
    m = pureGen2(feed=40)
    m.getSlaveTWCs().clear()
    assert m.countSlaveTWC() == 2


def test_an_unheard_slave_gets_no_offer():  # F9
    m = pureGen2(feed=40, slaves=1)
    quiet = m.newSlave(b"\x82\x60", 80)
    m.controlPass()
    assert target(quiet) is None and target(m.getSlaveTWCs()[0]) == 32


def waitFor(cond, timeout=5.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.005)


def test_a_raising_pass_is_logged_and_the_tick_continues(caplog):  # Review Focus 3
    m = pureGen2(feed=40)
    m.controlPass = Mock(side_effect=[RuntimeError, None, None, None, None, None])
    t = ControlTick(m, interval=0.01)
    assert t.daemon and t.name == "ControlTick"
    with caplog.at_level(logging.WARNING):
        t.start()
        waitFor(lambda: m.controlPass.call_count >= 3)
        t.stop()
    assert not t.is_alive()
    assert "RuntimeError" in caplog.text


def test_housekeeping_runs_ipc_and_expiry_in_every_mode():
    for mode in (1, 2, 3):
        m = pureGen2(feed=40)
        m.config["config"]["fakeMaster"] = mode
        ipc = Mock()
        m.getModuleByName = lambda n, ipc=ipc: ipc if n == "WebIPCControl" else None
        m.controlPass = Mock()
        m.lastVehicleExpiry = time.time() - 3601
        t = ControlTick(m)
        t.pass_()
        assert ipc.processIPC.call_count == 1
        m.queue_background_task.assert_called_once_with({"cmd": "expireVehicles"})
        assert m.controlPass.call_count == (1 if mode == 1 else 0)
        t.pass_()  # expiry is hourly; IPC once a tick
        assert ipc.processIPC.call_count == 2 and m.queue_background_task.call_count == 1


def test_the_serial_loop_no_longer_distributes_or_processes_ipc():
    src = LAUNCHER.read_text()
    loop = src[src.index('logger.info("Starting main event loop...")') :]
    loop = loop[: loop.index("except KeyboardInterrupt:")]
    for gone in ("distributeEVSEPower", "processIPC", "expireVehicles", "controlPass"):
        assert gone not in loop, gone
    assert "VehicleEndpoints" not in src  # registered by TWCMaster itself
    start = src.index("master.controlTick = ControlTick(master)")
    assert src.index("master.reconciler.start()") < start < src.index(
        "master.controlTick.start()"
    )
    shutdown = src[src.index("# Stop policy evaluation") :]
    assert re.search(
        r'getModuleByName\("Policy"\)\.stop\(\)\nmaster\.controlTick\.stop\(\)', shutdown
    )
    assert shutdown.index("master.controlTick.stop()") < shutdown.index(
        "master.reconciler.stop()"
    )
