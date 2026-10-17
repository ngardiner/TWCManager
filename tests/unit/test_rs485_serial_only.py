# tests/unit/test_rs485_serial_only.py: every RS-485 write goes through the
# master's outbox, which only the serial loop flushes (a write in the middle
# of a slave's reply corrupts both messages)
import ast
import os
import random
import re
from unittest.mock import Mock

import pytest

from TWCManager.TWCMaster import TWCMaster
from TWCManager.Vehicle.Registry.VIN import vinCheckDigitValid

LIB = os.path.join(os.path.dirname(__file__), "..", "..", "lib", "TWCManager")
LAUNCHER = os.path.join(LIB, "TWCManager.py")
CONFIG = {
    "config": {
        "debugOutputToFile": False,
        "subtractChargerLoad": False,
        "treatGenerationAsGridDelivery": False,
        "wiringMaxAmpsAllTWCs": 80,
        "maxAmpsAllowedFromGrid": None,
    }
}


def _read(path):
    with open(path) as f:
        return f.read()


def masterWithTwoSlaves():
    m = TWCMaster(b"\x77\x77", CONFIG)
    m.getModuleByName = Mock(return_value=None)
    iface = Mock()
    m.getModulesByType = lambda kind: (
        [{"name": "Dummy", "ref": iface, "type": "Interface"}]
        if kind == "Interface"
        else []
    )
    m.newSlave(b"\x82\x44", 80)
    m.newSlave(b"\x82\x45", 80)
    return m, iface


def test_http_start_and_stop_are_queued_not_sent():
    m, iface = masterWithTwoSlaves()
    m.sendStartCommand()
    m.sendStopCommand(b"\x82\x44")
    assert iface.send.call_count == 0
    assert m.flushRS485() == 3
    sent = [c.args[0][:2] for c in iface.send.call_args_list]
    assert sent == [b"\xfc\xb1", b"\xfc\xb1", b"\xfc\xb2"]
    assert iface.send.call_args_list[2].args[0][4:6] == b"\x82\x44"
    assert m.flushRS485() == 0


def test_vin_query_and_kwh_poll_are_queued():
    m, iface = masterWithTwoSlaves()
    m.getVehicleVIN(b"\x82\x44", 0)
    m.lastkWhPoll = 0
    m.getSlaveLifetimekWh()
    assert iface.send.call_count == 0 and m.flushRS485() == 3
    sent = [c.args[0][:2] for c in iface.send.call_args_list]
    assert sent == [b"\xfb\xee", b"\xfb\xeb", b"\xfb\xeb"]


# The only code that may write to the interface: the serial loop (module level
# of TWCManager.py) and what it calls on its own thread
ALLOWED = {
    ("TWCMaster.py", "flushRS485"),
    ("TWCMaster.py", "send_master_linkready1"),
    ("TWCMaster.py", "send_master_linkready2"),
    ("TWCMaster.py", "send_slave_linkready"),
    ("TWCSlave.py", "send_master_heartbeat"),
    ("TWCSlave.py", "send_slave_heartbeat"),
    ("TWCManager.py", None),
}


def _isInterface(node):
    # getInterfaceModule(), getModulesByType("Interface")[0]["ref"],
    # getModuleByName("RS485")
    while isinstance(node, ast.Subscript):
        node = node.value
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    name = node.func.attr
    args = [a.value for a in node.args if isinstance(a, ast.Constant)]
    return (
        name == "getInterfaceModule"
        or (name == "getModulesByType" and args == ["Interface"])
        or (name == "getModuleByName" and args == ["RS485"])
    )


def offenders():
    found = []
    for root, dirs, files in os.walk(LIB):
        if os.path.basename(root) == "Interface":
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            tree = ast.parse(_read(path))

            def walk(node, fn):
                for child in ast.iter_child_nodes(node):
                    inner = fn
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        inner = child.name
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "send"
                        and _isInterface(child.func.value)
                        and (name, fn) not in ALLOWED
                    ):
                        found.append(
                            "%s:%d" % (os.path.relpath(path, LIB), child.lineno)
                        )
                    walk(child, inner)

            walk(tree, None)
    return sorted(found)


def test_only_serial_thread_code_writes_rs485():
    assert offenders() == []


def test_the_serial_loop_flushes_the_outbox_before_its_own_messages():
    source = _read(LAUNCHER)
    loop = source.index("while True:\n    try:")
    flush = source.index("master.flushRS485()", loop)
    assert source.index("time.sleep(0.025)", loop) < flush
    assert flush < source.index('if config["config"]["fakeMaster"] == 1:', loop)


def test_shutdown_flushes_the_outbox():
    source = _read(LAUNCHER)
    drain = source.index("master.reconciler.drain(")
    close = source.index("master.getInterfaceModule().close()")
    assert drain < source.rindex("master.flushRS485()") < close


# The VIN check digit


def _baseRule(potentialVIN):
    # BASE's inline rule (TWCManager.py, 17-character branch), for the
    # cross-check below
    if len(potentialVIN) != 17:
        return False
    potentialVIN = potentialVIN.upper()
    check = potentialVIN[8]
    if check == "X":
        check = 10
    elif check.isdigit():
        check = int(check)
    else:
        return False
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    replaceValues = {
        "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
        "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
        "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
        "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9, "0": 0,
    }  # fmt: skip
    total = 0
    for digit, weight in zip(potentialVIN, weights):
        if digit not in replaceValues:
            return False
        total += replaceValues[digit] * weight
    return total % 11 == check


@pytest.mark.parametrize(
    "vin,ok",
    [
        ("5YJ3E1EA1KF000001", False),
        ("1M8GDM9AXKP042788", True),
        ("1M8GDM9A1KP042788", False),
        ("5YJ3E1EAIKF000001", False),
    ],
)
def test_check_digit_matches_the_old_inline_rule(vin, ok):
    assert vinCheckDigitValid(vin) is ok and vinCheckDigitValid(vin.lower()) is ok
    assert _baseRule(vin) is ok


def test_check_digit_agrees_with_the_base_rule_on_10k_random_vins():
    rng = random.Random(3779)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 -"
    valid = 0
    for i in range(10000):
        vin = "".join(rng.choice(alphabet) for _ in range(17))
        if i % 2:
            # Half with a correct check digit, so the True path is exercised
            for check in "0123456789X":
                candidate = vin[:8] + check + vin[9:]
                if _baseRule(candidate):
                    vin = candidate
                    break
        valid += _baseRule(vin)
        assert vinCheckDigitValid(vin) is _baseRule(vin), vin
    assert valid > 500
    for vin in ("", "1M8GDM9AXKP04278", "1M8GDM9AXKP0427888"):
        assert vinCheckDigitValid(vin) is False


# The VIN rx path, run from the launcher's source (it starts at import)


def _vinRxBlock():
    source = _read(LAUNCHER)
    text = lambda node: ast.get_source_segment(source, node)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.If) and "potentialVIN" in text(node):
            if "msgMatch" in text(node.test) and any(
                "VINData[vinPart]" in text(n) for n in node.body
            ):
                return ast.Module(body=[node], type_ignores=[])
    raise AssertionError("VIN rx block not found")


def _receiveVINPart(slave, part, text):
    msg = b"\xfd" + part + b"\x82\x44" + text.encode().ljust(7, b"\x00") + b"\x00"
    master = Mock()
    master.getSlaveByID.return_value = slave
    ns = {
        "msgMatch": re.search(rb"\A\xfd(\xee|\xef|\xf1)(..)(.+?).\Z", msg, re.DOTALL),
        "foundMsgMatch": False,
        "master": master,
        "logger": Mock(),
        "logging": __import__("logging"),
        "hex_str": lambda b: b.hex(),
        "vinCheckDigitValid": vinCheckDigitValid,
    }
    exec(compile(_vinRxBlock(), LAUNCHER, "exec"), ns)
    return master


def _slave():
    s = Mock()
    s.TWCID = b"\x82\x44"
    s.VINData = ["", "", ""]
    s.currentVIN = ""
    s.lastVINQuery = 123
    s.vinQueryAttempt = 2
    return s


def test_an_all_empty_vin_still_stops_querying():
    s = _slave()
    master = _receiveVINPart(s, b"\xf1", "")
    assert (s.lastVINQuery, s.vinQueryAttempt, s.currentVIN) == (0, 0, "")
    assert master.queue_background_task.call_count == 0
    assert master.recordVehicleVIN.call_count == 0


def test_a_valid_vin_is_recorded_and_a_bad_one_requeried():
    s = _slave()
    s.VINData = ["1m8gdm9", "aXKP042", ""]
    master = _receiveVINPart(s, b"\xf1", "788")
    assert s.currentVIN == "1M8GDM9AXKP042788"
    master.recordVehicleVIN.assert_called_once_with(s)
    s = _slave()
    s.VINData = ["1M8GDM9", "A1KP042", ""]
    master = _receiveVINPart(s, b"\xf1", "788")
    assert s.currentVIN == "" and master.recordVehicleVIN.call_count == 0
    master.queue_background_task.assert_called_once_with(
        {"cmd": "getVehicleVIN", "slaveTWC": s.TWCID, "vinPart": 0}
    )
