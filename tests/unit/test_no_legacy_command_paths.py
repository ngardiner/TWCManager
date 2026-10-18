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


def gitGrep(*args):
    # Matching lines ("path:line:text") for a git grep run from the repo root
    return subprocess.run(
        ["git", "grep", "-n", *args], capture_output=True, text=True, cwd=REPO
    ).stdout.splitlines()


def test_no_retired_allocator_names_remain():
    # No -w: a word match never sees "Gen2TWCs(self)"
    assert gitGrep("-E", r"Gen2TWCs\(|VehicleEndpoints\(|\(EVSEController\)", "--", "lib/TWCManager") == []
    names = "_evseTargetAmps|pointOwnsAmps|spikeAmpsProactively|spikeAmpsReactively|getSpikeAmps|setSpikeAmps|spikeAmpsToCancel6ALimit|set_last_amps_offered|stopRequested|_queueChargeRate|_setCarAmps|APIcontrol"
    assert gitGrep("-wE", names, "--", "lib/TWCManager", "html", "tests/integration", "tests/API", "docs") == []


def test_no_flex_or_dampening_code_remains():
    # lib only: the docs keep an "ignored" note
    names = "allowed_flex|getAllowedFlex|setAllowedFlex|useFlexAmpsToStartCharge|lastAmpsDesired|timeLastAmpsDesiredFlipped|dampenChanges"
    assert gitGrep("-wE", names, "--", "lib/TWCManager") == []


def test_one_distributor_entry():
    # distributeEVSEPower is called from TWCMaster.controlPass and nowhere else
    import ast

    callers = []
    for path in (REPO / "lib").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "distributeEVSEPower":
                    callers.append((path.name, fn.name))
    assert callers == [("TWCMaster.py", "controlPass")]
