# tests/unit/test_provider_outcomes_local.py
from unittest.mock import Mock
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import DesiredStore
from tests.unit.reconcile_helpers import ScriptedProvider
from tests.unit.test_teslable_pairing_status import make, VIN
from tests.unit.test_gen2twc_provider import make as make_gen2

def test_ble_outcomes_follow_tesla_control_output():
    ble, reg, master = make(); (p,) = ble.providers(); v = reg.getOrCreate(VIN, "t"); ble._claimUnproven(v)
    ble.sendCommand = Mock(return_value=None);        assert p.setChargeLimit(VIN, 80) == O.Outcome(O.UNREACHABLE, "no reply")
    ble.sendCommand = Mock(return_value="Command executed successfully"); assert p.setChargeLimit(VIN, 80).kind == O.DONE
    ble.sendCommand = Mock(return_value="Error: car could not execute command: already_set"); assert p.setChargeLimit(VIN, 80) == O.Outcome(O.SATISFIED, "already_set")
    ble.sendCommand = Mock(return_value="connection failed"); assert p.stopCharging(VIN) == O.Outcome(O.ERROR, "connection")
    assert ble.sendCommand.call_args.args == (VIN, "charging-stop")

def test_ble_key_refusal_is_refused_and_the_claim_follows_the_pairing_window():
    ble, reg, master = make(); (p,) = ble.providers(); v = reg.getOrCreate(VIN, "t"); ble._claimUnproven(v)
    master.getModuleByName = Mock(return_value=None)                                                      # no TeslaAPI: no schedule data (a Mock module's schedule would block the start)
    def refuse(vin, command, args=None): ble._keyRefusal.refused = True; ble._noteBleRefusal(vin, ble.KEY_REFUSED_TEXT); return None
    ble.sendCommand = refuse
    assert p.startCharging(VIN).kind == O.REFUSED and "TeslaBLE" not in reg.get(VIN).claims and p.ready(reg.get(VIN), C.START_CHARGING) is False
    ble._claimUnproven(v); ble._setBleStatus(VIN, "pairing", pairingSent=__import__("time").time())
    assert p.startCharging(VIN).kind == O.REFUSED and "TeslaBLE" in reg.get(VIN).claims                   # inside the window: not a refusal of the key

def test_ble_probe_is_one_non_waking_body_controller_query_that_proves_nothing():
    ble, reg, master = make(); (p,) = ble.providers(); v = reg.getOrCreate(VIN, "t"); ble._claimUnproven(v)
    ble._run_command_with_timeout = Mock(return_value=(b"", b"ok", 0)); ble.sendPrivateKey = Mock(return_value=True); ble._execute_with_retry = Mock(side_effect=AssertionError("no retry wrapper"))
    master.settings.setdefault("Vehicles", {})[VIN] = {"privKey": "k"}
    assert p.probe(VIN).kind == O.DONE and ble._run_command_with_timeout.call_args.args[0][-1] == "body-controller-state"
    assert reg.get(VIN).claims["TeslaBLE"].status == "unproven"                                           # no proof from a VCSEC answer
    ble._run_command_with_timeout = Mock(return_value=(None, None, None)); assert p.probe(VIN).kind == O.UNREACHABLE

def test_ble_schedule_block_is_satisfied_without_a_command():
    ble, reg, master = make(); (p,) = ble.providers(); ble._scheduleBlocksStart = lambda vin: True; ble.sendCommand = Mock()
    assert p.startCharging(VIN) == O.Outcome(O.SATISFIED, "scheduled") and ble.sendCommand.call_count == 0

def test_gen2_stop_and_start_outcomes_and_the_billed_stop_suppression():
    m, s = make_gen2(); m.desired = DesiredStore(); p = m.providerOrder.provider("Gen2TWC"); v = m.vehicleRegistry.getOrCreate(VIN, "t"); s.currentVIN = VIN.lower()
    assert p.stopCharging(VIN.lower()) == O.Outcome(O.UNSUPPORTED, "mode 1") and p.ready(v, C.START_CHARGING) is False
    m.settings["chargeStopMode"] = "2"
    assert p.stopCharging(VIN).kind == O.DONE and m.settings["respondToSlaves"] == 0 and p.ready(v, C.START_CHARGING) is False   # no Gen2 stop recorded yet
    m.desired.state(VIN).lastStopProvider = "Gen2TWC"
    assert p.ready(v, C.START_CHARGING) and p.startCharging(VIN).kind == O.DONE and m.settings["respondToSlaves"] == 1
    m.desired.state(VIN).lastStopProvider = "TeslaBLE"; assert p.ready(v, C.START_CHARGING) is False      # a BLE stop is undone over BLE
    fleet = Mock(local=False, name="FleetAPI"); assert m.providerOrder.suppressions.suppressed(fleet, C.STOP_CHARGING) and not m.providerOrder.suppressions.suppressed(fleet, C.START_CHARGING)
    m.settings["chargeStopMode"] = "1"; assert not m.providerOrder.suppressions.suppressed(fleet, C.STOP_CHARGING)
    assert p.stopCharging("5YJ3E1EA1KF000009") == O.Outcome(O.UNSUPPORTED, "no slave")

def test_mode_3_stop_is_sent_by_the_heartbeat_not_the_caller():
    from tests.unit.gen2_helpers import drive
    m, s = make_gen2(); m.desired = DesiredStore(); p = m.providerOrder.provider("Gen2TWC"); s.currentVIN = VIN; m.settings["chargeStopMode"] = "3"; m.sendStopCommand = Mock()
    assert p.stopCharging(VIN) == O.Outcome(O.DONE, "queued") and s.stopRequested and m.sendStopCommand.call_count == 0
    drive(m, s, amps_actual=12.0)                                                                         # the serial thread's own heartbeat
    m.sendStopCommand.assert_called_once_with(s.TWCID); assert not s.stopRequested

def test_dispatch_reads_outcomes_until_the_facade_goes():
    m, s = make_gen2(); v = m.vehicleRegistry.getOrCreate(VIN, "t")
    a = ScriptedProvider("TeslaBLE", 2, True, [O.Outcome(O.UNREACHABLE)]); b = ScriptedProvider("FleetAPI", 5, False, [O.Outcome(O.DONE)], tier=C.FLEET_COMMAND)
    m.providerOrder.register(a); m.providerOrder.register(b)
    assert m.providerOrder.dispatch(v, C.SET_AMPS, lambda p: p.setAmps(VIN, 10), amps=10, localRule=False).kind == O.DONE and len(a.calls) == 3 and len(b.calls) == 1
