# A ~1 s control tick runs the one distributor for every install, makes
# each Gen2 offer and runs the start/stop gates; flex is not supported.
# Real TWCMaster and Gen2TWC throughout.
import inspect
import json
import logging
import re
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import serial

from TWCManager.ControlTick import ControlTick
from TWCManager.EVSEInstance.ChargingPoint import ChargingPoint
from TWCManager.EVSEInstance.VehicleEndpoint import vehicleEndpoints
from TWCManager.Interface.RS485 import RS485
from TWCManager.TWCMaster import TWCMaster
from TWCManager.TWCSlave import Offer, TWCSlave
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from tests.unit.gen2_helpers import tickThenBeat
from tests.unit.test_charging_points import VIN, VIN2, car, real, setAmps
from tests.unit.test_gen2twc_provider import CONFIG
from tests.unit.test_gen3twc_provider import vitals

LAUNCHER = Path(__file__).resolve().parents[2] / "lib" / "TWCManager" / "TWCManager.py"


def target(s):
    return s.pointAmps


def pureGen2(feed, config=None, slaves=2):
    # A pure-Gen2 install: no TeslaAPI module (TeslaAPI disabled), so no
    # car is listed; the slaves are charging and heard from
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


def test_teslaapi_disabled_install_uses_the_one_distributor():
    # Without TeslaAPI the water-fill distributor still runs
    m = pureGen2(feed=40)
    assert m.getModuleByName("TeslaAPI") is None
    m.controlPass()
    assert [target(s) for s in m.getSlaveTWCs()] == [20, 20]


def test_flex_settings_are_read_but_ignored():
    # Flex settings load without error and give no extra amps
    m = pureGen2(
        feed=4,
        config={"greenEnergyFlexAmps": 3, "useFlexAmpsToStartCharge": True},
        slaves=0,
    )
    s = charging(m, actual=2.0)
    m.controlPass()
    assert target(s) == 0  # minAmpsPerTWC 6: below it is a stop
    assert not hasattr(m, "allowed_flex") and not hasattr(s, "useFlexAmpsToStartCharge")


def test_a_policy_with_allowed_flex_evaluates_without_error():
    # A policy's allowed_flex key is no longer read
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


def test_restart_with_a_car_charging_never_offers_zero():
    m = pureGen2(feed=40, slaves=0)
    m.settings["chargeRateControl"] = 1
    s = m.newSlave(b"\x82\x44", 80)  # its linkready
    s.protocolVersion = 2
    s.send_master_heartbeat()
    m.controlPass()  # not heard from yet: no offer
    assert s.heardFrom is False and s.offer is None
    iface = tickThenBeat(m, s, 0x01, 40, 32)  # the first reply: charging at 32 A
    assert s.offer.amps == 32 and s.heardFrom is True
    for _ in range(2):
        hb = iface.send.call_args.args[0][6:15]
        assert hb[0] == 0x09 and ((hb[1] << 8) + hb[2]) / 100 == 32  # never 0 A
        tickThenBeat(m, s, 0x01, 40, 32)


def test_heartbeat_before_the_first_tick_sends_no_change():
    m = pureGen2(feed=40, slaves=0)
    s = m.newSlave(b"\x82\x44", 80)
    s.protocolVersion, s.reportedAmpsActual = 2, 32.0
    assert s.offer is None
    s.send_master_heartbeat()
    iface = m.getModulesByType("Interface")[0]["ref"]
    assert bytes(iface.send.call_args.args[0][6:15]) == bytes(9)


def test_a_gen3_only_install_is_controlled():
    # With no Gen2 slave, Gen3 units are still given a target
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


def test_a_vehicle_only_install_is_controlled():
    # No Gen2 slave, no Gen3 unit: a car at home on no TWC still gets its rate
    m = real()
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    assert (
        m.countSlaveTWC() == 0 and m.getModulesByType("Interface") == []
    )  # no Gen2 interface
    m.controlPass()
    assert setAmps(m) == [{"vin": VIN, "amps": 20}]


def defaultGen2Link(m):
    # The shipped config: the Gen2 link on /dev/ttyUSB0, where nothing is
    m.config["interface"] = {"RS485": {"enabled": True, "port": "/dev/ttyUSB0"}}
    gone = serial.serialutil.SerialException("no such device")
    with patch.object(serial, "serial_for_url", side_effect=gone):
        link = RS485(m)
    m.registerModule({"name": "RS485", "ref": link, "type": "Interface"})
    assert not link.connected


def test_a_gen3_only_install_with_default_config_is_controlled():
    m = real(gen3=True)
    defaultGen2Link(m)
    gen3 = m.modules["Gen3TWCs"]["ref"]
    gen3.setUnitTarget = Mock()
    car(m, VIN, "Charging")  # away from the idle Gen3, on a mobile connector
    (u,) = gen3.devices()
    vitals(u, False, 0.0, state="idle")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    m.controlPass()
    gen3.setUnitTarget.assert_called_once_with(u.ID, 0)
    assert setAmps(m) == [{"vin": VIN, "amps": 20}]


def test_a_vehicle_only_install_with_default_config_is_controlled():
    m = real()
    defaultGen2Link(m)
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    m.controlPass()
    assert setAmps(m) == [{"vin": VIN, "amps": 20}]


def test_a_gen2_install_with_no_slave_linked_sends_no_amps():
    # The RS-485 link is open but no slave has linked (startup, a timed-out
    # slave): the TWC might serve the car, so the car gets no endpoint and
    # no rate
    m = real()
    m.registerModule({"name": "RS485", "ref": Mock(), "type": "Interface"})
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    for _ in range(3):
        m.controlPass()
    assert setAmps(m) == [] and vehicleEndpoints(m) == []
    s = m.newSlave(b"\x82\x44", 80)  # it links and replies: 00, 0 A
    m.controlPass()
    assert [p.ID for p in m.chargingPoints()] == ["Point-Gen2TWC-8244", "Point-" + VIN]


def test_a_pass_with_nothing_to_distribute_to_does_nothing():
    m = real()
    m.controlPass()
    assert setAmps(m) == [] and m.desired.keys() == []


def test_a_slave_deleted_mid_pass_does_not_break_the_pass():
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


def test_an_unheard_slave_gets_no_offer():
    m = pureGen2(feed=40, slaves=1)
    quiet = m.newSlave(b"\x82\x60", 80)
    m.controlPass()
    assert quiet.offer is None and m.getSlaveTWCs()[0].offer.amps == 32


def waitFor(cond, timeout=5.0):
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.005)


def test_a_raising_pass_is_logged_and_the_tick_continues(caplog):
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


def failingTick(m):
    m.controlPass = Mock(side_effect=RuntimeError("boom"))
    return ControlTick(m)


def test_a_failing_pass_logs_its_traceback_first_and_every_60th(caplog):
    m = pureGen2(feed=40)
    t = failingTick(m)
    with caplog.at_level(logging.WARNING, logger="TWCManager"):
        for _ in range(120):
            t.pass_()
    traced = [r.getMessage() for r in caplog.records if r.exc_info]
    assert traced == [
        "Control tick: controlPass failed (%d in a row)" % n for n in (1, 60, 120)
    ]
    assert "boom" in caplog.text


def test_offers_are_withdrawn_after_30_failed_passes_and_resume(caplog):
    m = pureGen2(feed=40)
    quiet = m.newSlave(b"\x82\x60", 80)
    m.controlPass()
    working = m.controlPass
    t = failingTick(m)
    with caplog.at_level(logging.INFO, logger="TWCManager"):
        for _ in range(29):
            t.pass_()
        assert [s.offer.amps for s in m.getSlaveTWCs()[:2]] == [20, 20]
        for _ in range(6):
            t.pass_()
        assert [s.offer for s in m.getSlaveTWCs()[:2]] == [Offer(0, 0)] * 2
        assert quiet.offer is None
        m.controlPass = working
        t.pass_()
        t.pass_()
    assert [s.offer.amps for s in m.getSlaveTWCs()[:2]] == [20, 20]
    levels = [(r.levelno, r.getMessage()) for r in caplog.records]
    errors = [msg for lvl, msg in levels if lvl == logging.ERROR]
    resumed = [msg for lvl, msg in levels if "offers resume" in msg]
    assert len(errors) == 1 and "30 failed passes" in errors[0]
    assert resumed == ["Control tick: controlPass succeeded; offers resume"]
    assert t.failures == 0


def test_a_withdrawal_targets_gen3_units_and_cars_zero():
    m = real(gen3=True)
    gen3 = m.modules["Gen3TWCs"]["ref"]
    gen3.setUnitTarget = Mock()
    v = car(m, VIN, "Charging")
    v.observe("pluggedIn", True, "TeslaMateVehicle")
    (u,) = gen3.devices()
    vitals(u, True, 10.0)
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    m.controlPass()
    gen3.setUnitTarget.reset_mock()
    m.withdrawOffers()
    gen3.setUnitTarget.assert_called_once_with(u.ID, 0)

    m = real()
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    m.controlPass()
    assert setAmps(m) == [{"vin": VIN, "amps": 20}]
    m.withdrawOffers()
    assert setAmps(m) == []


def test_a_withdrawal_leaves_no_car_offered_power():
    m = real()
    car(m, VIN, "Charging")
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    m.controlPass()
    assert m.offered[VIN] > 0
    m.withdrawOffers()
    assert m.offered[VIN] == 0 and not any(m.offered.values())


def test_a_withdrawal_zeroes_the_slaves_even_if_points_cannot_be_built():
    m = pureGen2(feed=40)
    m.controlPass()
    m.chargingPoints = Mock(side_effect=RuntimeError)
    with pytest.raises(RuntimeError):
        m.withdrawOffers()
    assert [s.offer for s in m.getSlaveTWCs()] == [Offer(0, 0)] * 2


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
        assert (
            ipc.processIPC.call_count == 2 and m.queue_background_task.call_count == 1
        )


def test_the_serial_loop_no_longer_distributes_or_processes_ipc():
    src = LAUNCHER.read_text()
    loop = src[src.index('logger.info("Starting main event loop...")') :]
    loop = loop[: loop.index("except KeyboardInterrupt:")]
    for gone in ("distributeEVSEPower", "processIPC", "expireVehicles", "controlPass"):
        assert gone not in loop, gone
    start = src.index("master.controlTick = ControlTick(master)")
    assert (
        src.index("master.reconciler.start()")
        < start
        < src.index("master.controlTick.start()")
    )
    shutdown = src[src.index("# Stop policy evaluation") :]
    assert re.search(
        r'getModuleByName\("Policy"\)\.stop\(\)\nmaster\.controlTick\.stop\(\)',
        shutdown,
    )
    assert shutdown.index("master.controlTick.stop()") < shutdown.index(
        "master.reconciler.stop()"
    )


# Offers are computed on the tick; charging points own car amps


def test_the_lift_survives_a_session_end_racing_the_tick():
    # The serial thread only counts sessions and the tick only records the
    # session it lifted, so a session end between passes is never lost
    m = real()
    m.getMaxAmpsToDivideAmongSlaves = lambda: 20
    v = m.vehicleRegistry.getOrCreate(VIN, "OwnerAPI")
    s = charging(m, actual=10.0)
    s.currentVIN, s.plugSessions, s.liftedSession = VIN, 3, 3
    v.observe("twcVin", s.twcid, "TWC")  # confirmed here
    assert s.vehicleRateRaised
    m.controlPass()
    assert m.desired.get(VIN, AMPS) is None  # lifted this session already
    s.isCharging, s.chargingDroppedBelowThresholdTime = 1, time.time() - 10
    s.reportedState, s.reportedAmpsActual = 0x04, 0.0
    m.observeChargingSessions()  # the serial thread ends session 3
    assert s.plugSessions == 4 and not s.vehicleRateRaised
    m.controlPass()
    d = m.desired.get(VIN, AMPS)
    assert d.lift and d.value == 32 and s.liftedSession == 4


def test_no_offer_or_cancel_for_an_unheard_slave():
    m = pureGen2(feed=40, slaves=0)
    m.cancelStopCarsCharging = Mock()
    heard = charging(m, actual=10.0)
    heard.currentVIN = VIN
    quiet = m.newSlave(b"\x82\x60", 80)
    quiet.currentVIN = VIN2
    m.controlPass()
    assert quiet.offer is None and heard.offer.amps == 32
    m.cancelStopCarsCharging.assert_called_once_with(VIN)


def test_no_stop_cancel_during_the_mode_2_silence():
    m = pureGen2(feed=40, slaves=0)
    m.cancelStopCarsCharging = Mock()
    s = charging(m, actual=10.0)
    s.currentVIN = VIN
    m.settings["respondToSlaves"] = 0
    m.settings["respondToSlavesExpiry"] = time.time() + 60
    m.controlPass()
    assert s.offer is None and not m.cancelStopCarsCharging.called
    m.settings["respondToSlavesExpiry"] = time.time() - 1
    m.controlPass()
    assert s.offer.amps == 32
    m.cancelStopCarsCharging.assert_called_once_with(VIN)


def test_offers_and_their_status_hold_during_the_mode_2_silence():
    m = pureGen2(feed=40, slaves=1)
    status = Mock()
    m.registerModule({"name": "Status", "ref": status, "type": "Status"})
    (s,) = m.getSlaveTWCs()
    m.controlPass(now=1000)
    held = (s.offer, s.lastAmpsOffered, s.timeLastAmpsOfferedChanged)
    status.reset_mock()
    m.settings["respondToSlaves"] = 0
    m.settings["respondToSlavesExpiry"] = 2000
    m.getMaxAmpsToDivideAmongSlaves = lambda: 10
    m.controlPass(now=1010)
    assert (s.offer, s.lastAmpsOffered, s.timeLastAmpsOfferedChanged) == held
    assert not status.setStatus.called


def test_wiring_cap_is_rechecked_when_others_draw_more():
    # The wiring cap applies to every offer, not just changed ones
    m = pureGen2(feed=40, config={"wiringMaxAmpsAllTWCs": 48})
    s, other = m.getSlaveTWCs()
    m.controlPass()
    assert s.offer.amps == 20  # 20 drawn elsewhere: 40 of 48
    other.reportedAmpsActual = 40.0
    m.controlPass()
    assert s.offer.amps == 8  # the same target, re-capped: 48 - 40


def test_green_policy_does_not_hold_a_car_below_the_minimum():
    # A share below the minimum stops the car at once; it is not held at the minimum
    m = pureGen2(feed=10, slaves=0)
    m.getModuleByName("Policy").policyIsGreen.return_value = True
    s = charging(m, actual=10.0)
    s.isCharging = 1
    m.controlPass()
    assert s.offer.amps == 10
    m.getMaxAmpsToDivideAmongSlaves = lambda: 3
    m.controlPass()
    assert s.offer.amps == 0  # the stop rule at once, never a held 6 A


def test_spike_settings_are_ignored():
    # The spike settings load but never raise an offer
    m = pureGen2(feed=12, slaves=0)
    m.settings["spikeAmpsProactively"] = m.settings["spikeAmpsReactively"] = 1
    s = charging(m, actual=6.0)
    sent = []
    for _ in range(5):
        s.timeLastAmpsOfferedChanged -= 10
        hb = tickThenBeat(m, s, 0x01, 6, 6.0).send.call_args.args[0][6:15]
        sent.append(((hb[1] << 8) + hb[2]) / 100)
    assert sent == [12] * 5  # never the 16 or 21 A spike


def test_no_car_amps_code_left_in_the_slave():
    for name in (
        "_setCarAmps",
        "_clearCarAmps",
        "_carRateConfirmed",
        "_queueChargeRate",
        "_requestVehicleRateRaise",
        "_anonCars",
        "APIcontrol",
    ):
        assert not hasattr(TWCSlave, name), name
    assert "APIcontrol" not in inspect.getsource(TWCSlave)


def test_receive_writes_no_offer():
    src = inspect.getsource(TWCSlave.receive_slave_heartbeat)
    for gone in ("offer", "lastAmpsOffered =", "desired."):
        assert gone not in src, gone


def test_a_twc_with_no_car_key_rates_and_lifts_nothing():  # was TestVehicleChargeRateDesired
    for mode in (1, 2):
        m = real(mode=mode)
        m.getMaxAmpsToDivideAmongSlaves = lambda: 20
        s = charging(m, actual=10.0)
        assert s.vehicleKey is None
        m.controlPass()
        assert s.offer.amps > 0 and m.desired.keys() == [] and not s.vehicleRateRaised


# The start/stop gates run on the tick


def test_the_heartbeat_decides_no_charging():
    src = inspect.getsource(TWCSlave.send_master_heartbeat)
    for gone in ("CarsCharging", "StartCharging"):
        assert gone not in src, gone


def test_no_gate_acts_before_the_first_offer():
    # A reply alone decides nothing: the gates wait for the tick's offer
    m = pureGen2(feed=40, slaves=0)
    m.withdrawStartCharging = Mock()
    s = charging(m, actual=0.0)
    s.protocolVersion, s.currentVIN = 2, VIN
    assert s.offer is None and s.chargeGate(time.time(), VIN) is None
    s.heardFrom = False
    m.controlPass()
    assert s.offer is None and not m.withdrawStartCharging.called


def test_retired_settings_keys_load_and_are_ignored(tmp_path):
    # Flex and the 6 A spike settings still load from old files; nothing
    # reads them. 10 A over two cars is 5 A each, below the 6 A minimum,
    # where flex let both charge
    def offers(retired):
        extra = {"greenEnergyFlexAmps": 4, "useFlexAmpsToStartCharge": True}
        m = pureGen2(
            feed=10,
            config=dict(
                extra if retired else {}, settingsPath=str(tmp_path / str(retired))
            ),
        )
        (tmp_path / str(retired)).mkdir()
        spike = {"spikeAmpsProactively": 1, "spikeAmpsReactively": 1}
        (tmp_path / str(retired) / "settings.json").write_text(
            json.dumps(dict(spike if retired else {}, chargeRateControl=1))
        )
        carapi, lookup = Mock(enabled=Mock(return_value=False)), m.getModuleByName
        carapi.migrateSavedTokens.return_value = False
        m.getModuleByName = lambda n: carapi if n == "TeslaAPI" else lookup(n)
        m.loadSettings()
        assert ("spikeAmpsProactively" in m.settings) is retired
        m.controlPass()
        return [(s.offer.amps, s.pointAmps) for s in m.getSlaveTWCs()]

    # One car gets the 10 A, the other a stop: no flex share for either
    assert offers(True) == offers(False) == [(0, 0), (10, 10)]
