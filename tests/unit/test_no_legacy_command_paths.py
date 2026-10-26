import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]


def grep(pattern, exclude=()):
    # Every tracked file under lib/ that matches, code and comments alike
    out = subprocess.run(
        ["git", "grep", "-l", pattern, "--", "lib"],
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout.split()
    return [path for path in out if path not in exclude]


def test_legacy_paths_and_dead_code_are_gone():
    assert grep("VehiclePriority\\|queue_vehicle_command\\|calculateModulePriority\\|getModuleByPriority\\|targetAmps\\|_DISPATCH\\|def attempts") == []
    assert grep("def car_api_charge\\|def applyChargeLimit\\|def setChargeRate\\|def apply_charge_limit\\|def car_api_available\\|stopAskingToStartCharging\\|NormalChargeLimit\\|_heldLimitKey") == []
    assert grep("def dispatch\\|lastChargeLimitApplied\\|stopTryingToApplyLimit\\|getLastStartOrStopChargeTime\\|_execute_with_retry\\|BLECircuitBreaker\\|class RateLimits\\|ReadSource") == []
    # Vehicle.summary/summaries feed a template (pre-flight I10)
    assert grep("_attemptSummary\\|_desiredSummary\\|def summary", exclude=["lib/TWCManager/Vehicle/Registry/Vehicle.py", "lib/TWCManager/Vehicle/Registry/VehicleRegistry.py"]) == []


def test_unread_declarations_and_orphans_are_gone():   # slim Task 13
    assert grep("READ_LIST\\|READ_ONLINE\\|READ_PLUG\\|READ_VIN\\|READ_AMPS\\|READ_LOCATION\\|TelemetryProvider\\|sharedControl\\|passive=\\|vehicleAttachment\\|commandPolicyOverridden") == []
    assert grep("def validate_integer\\|def validate_string\\|def validate_choice\\|def validate_required_fields\\|def is_safe_string\\|def startAllCharging\\|def stopAllCharging") == []
    assert grep("def getSendServerTime\\|def setSendServerTime\\|def getMasterSign\\|def setkWhDelivered\\|def delete_background_task") == []
    assert grep("def pointFor\\|def fresherThanWrite\\|def syncTimestamp\\|def energySites\\|def unitTargets\\|def headroom\\|LANE_DETAILS\\|\\blane\\b") == []
