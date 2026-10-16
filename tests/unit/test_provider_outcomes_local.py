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
    assert p.startCharging(VIN) == O.Outcome(O.ERROR, "pairing") and "TeslaBLE" in reg.get(VIN).claims    # inside the window: not a refusal of the key; retryable

def test_ble_schedule_block_is_satisfied_without_a_command():
    ble, reg, master = make(); (p,) = ble.providers(); ble._scheduleBlocksStart = lambda vin: True; ble.sendCommand = Mock()
    assert p.startCharging(VIN) == O.Outcome(O.SATISFIED, "scheduled") and ble.sendCommand.call_count == 0

def test_gen2_stop_and_start_outcomes_and_the_billed_stop_suppression():
    m, s = make_gen2(); m.desired = DesiredStore(); p = m.providerOrder.provider("Gen2TWC"); v = m.vehicleRegistry.getOrCreate(VIN, "t"); s.currentVIN = VIN.lower()
    assert p.stopCharging(VIN.lower()) == O.Outcome(O.UNSUPPORTED, "mode 1") and p.ready(v, C.START_CHARGING) is False
    m.settings["chargeStopMode"] = "2"; p.summary(v); assert p.ready(v, C.START_CHARGING) is False and m.desired.keys() == []   # a read never creates desired state
    m.settings["chargeStopMode"] = "1"
    m.settings["chargeStopMode"] = "2"
    assert p.stopCharging(VIN).kind == O.DONE and m.settings["respondToSlaves"] == 0 and p.ready(v, C.START_CHARGING) is False   # no Gen2 stop recorded yet
    m.desired.state(VIN).lastStopProvider = "Gen2TWC"
    assert p.ready(v, C.START_CHARGING) and p.startCharging(VIN).kind == O.DONE and m.settings["respondToSlaves"] == 1
    m.desired.state(VIN).lastStopProvider = "TeslaBLE"; assert p.ready(v, C.START_CHARGING) is False      # a BLE stop is undone over BLE
    fleet = Mock(local=False, SUPPRESSED=None, name="FleetAPI"); assert m.providerOrder.suppressions.suppressed(fleet, C.STOP_CHARGING) and m.providerOrder.suppressions.suppressed(fleet, C.START_CHARGING)
    m.settings["chargeStopMode"] = "3"; assert m.providerOrder.suppressions.suppressed(fleet, C.STOP_CHARGING) and not m.providerOrder.suppressions.suppressed(fleet, C.START_CHARGING)
    m.settings["chargeStopMode"] = "1"; assert not m.providerOrder.suppressions.suppressed(fleet, C.STOP_CHARGING)
    assert p.stopCharging("5YJ3E1EA1KF000009") == O.Outcome(O.UNSUPPORTED, "no slave")

def test_mode_3_stop_is_sent_by_the_heartbeat_not_the_caller():
    from tests.unit.gen2_helpers import drive
    m, s = make_gen2(); m.desired = DesiredStore(); p = m.providerOrder.provider("Gen2TWC"); s.currentVIN = VIN; m.settings["chargeStopMode"] = "3"; m.sendStopCommand = Mock()
    assert p.stopCharging(VIN) == O.Outcome(O.DONE, "queued") and s.stopRequested and m.sendStopCommand.call_count == 0
    drive(m, s, amps_actual=12.0)                                                                         # the serial thread's own heartbeat
    m.sendStopCommand.assert_called_once_with(s.TWCID); assert not s.stopRequested

# --- Fix round 1 ---
import logging
from TWCManager.Vehicle import TeslaBLE as bleModule

REFUSAL = b"Error: vehicle rejected request: your public key has not been paired with the vehicle"


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG); self.records = []
    def emit(self, record):
        self.records.append(record)


def _wire(ble, stderr, rc):
    ble._run_command_with_timeout = Mock(return_value=(b"", stderr, rc)); ble.maxRetries = 0


def test_a_failing_command_logs_no_raw_output_at_info_or_above():
    ble, reg, master = make(); (p,) = ble.providers(); _wire(ble, b"SECRET-MARKER connection failed", 1)
    h = _Records(); bleModule.logger.addHandler(h); level = bleModule.logger.level; bleModule.logger.setLevel(logging.DEBUG)
    try:
        assert p.stopCharging(VIN) == O.Outcome(O.UNREACHABLE, "no reply")
    finally:
        bleModule.logger.removeHandler(h); bleModule.logger.setLevel(level)
    loud = [r.getMessage() for r in h.records if r.levelno >= logging.INFO]
    assert loud and not any("SECRET-MARKER" in msg for msg in loud)
    assert any("connection" in msg and "charging-stop" in msg for msg in loud)                         # command, code and category only
    assert any("SECRET-MARKER" in r.getMessage() for r in h.records if r.levelno == logging.DEBUG)


def test_key_refusal_inside_the_pairing_window_is_a_retryable_error_and_keeps_the_claim():
    import time
    ble, reg, master = make(); (p,) = ble.providers(); v = reg.getOrCreate(VIN, "t"); ble._claimUnproven(v)
    ble._setBleStatus(VIN, "pairing", pairingSent=time.time()); _wire(ble, REFUSAL, 1)
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.ERROR, "pairing") and "TeslaBLE" in reg.get(VIN).claims


def test_key_refusal_outside_the_pairing_window_is_refused_and_drops_the_claim():
    ble, reg, master = make(); (p,) = ble.providers(); ble._setBleStatus(VIN, "paired"); _wire(ble, REFUSAL, 1)
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.REFUSED, "key") and "TeslaBLE" not in reg.get(VIN).claims


def test_an_open_breaker_half_opens_after_recovery_and_a_successful_command_closes_it():
    ble, reg, master = make(); (p,) = ble.providers(); v = reg.getOrCreate(VIN, "t"); ble._claimUnproven(v)
    for _ in range(ble.circuit_breaker.failure_threshold):
        ble.circuit_breaker.record_failure(VIN)
    _wire(ble, b"ok", 0); assert ble.circuit_breaker.is_open(VIN)
    assert p.setAmps(VIN, 10) == O.Outcome(O.UNREACHABLE, "no reply") and ble._run_command_with_timeout.call_count == 0   # open: nothing sent
    ble.circuit_breaker.failure_times[VIN] -= ble.circuit_breaker.recovery_timeout + 1                                   # recovery passed: half-open
    assert not ble.circuit_breaker.is_open(VIN) and ble.circuit_breaker.failures[VIN] >= ble.circuit_breaker.failure_threshold
    assert p.setAmps(VIN, 10).kind == O.DONE and ble._run_command_with_timeout.call_count == 1 and ble.circuit_breaker.failures[VIN] == 0
    ble.circuit_breaker.record_failure(VIN); assert not ble.circuit_breaker.is_open(VIN)                                 # closed: one failure no longer trips it


def test_bare_twcslave_has_no_stop_requested():
    from TWCManager.TWCSlave import TWCSlave
    assert TWCSlave.stopRequested is False
