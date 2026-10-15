import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Regression test for a live incident: `systemctl restart` hung 90 s until
# SIGKILL because a module left a non-daemon threading.Timer running, which
# Python joins at interpreter exit. TWCManager now ends with os._exit(0).

BOOT = """
import runpy, threading
threading.Timer(3600, lambda: None).start()  # non-daemon, like a buggy module
runpy.run_module("TWCManager.TWCManager", run_name="__main__")
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_config(path):
    text = ""
    with open(path) as f:
        for line in f:
            if line.lstrip().startswith(("//", "#")):
                text += "\n"
            else:
                text += line.replace("\t", " ").split("#")[0]
    return yaml.safe_load(text)


def _retarget(node, tmp):
    """Point any /tmp/twcmanager path at tmp; give listenPort a free port."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "listenPort":
                node[k] = _free_port()
            else:
                node[k] = _retarget(v, tmp)
    elif isinstance(node, list):
        return [_retarget(v, tmp) for v in node]
    elif isinstance(node, str) and node.startswith("/tmp/twcmanager"):
        return str(tmp) + node[len("/tmp/twcmanager") :]
    return node


def test_sigterm_exits_promptly_despite_non_daemon_thread(tmp_path):
    if os.path.exists("/etc/twcmanager/config.json"):
        pytest.skip("/etc/twcmanager/config.json exists; cannot control config")

    cfg = _retarget(
        _load_config(os.path.join(REPO, "etc", "twcmanager", ".testconfig.json")),
        tmp_path,
    )
    # Avoid a 10 s connect timeout to a MySQL server that isn't there.
    cfg["logging"]["MySQL"]["enabled"] = False
    # Keep the run hermetic: no MQTT clients dialing 127.0.0.1:1883.
    for section in ("control", "sources", "status"):
        cfg[section]["MQTT"]["enabled"] = False
    (tmp_path / "config.json").write_text(json.dumps(cfg))

    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    lib = os.path.join(REPO, "lib")
    env["PYTHONPATH"] = lib + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    proc = subprocess.Popen(
        [sys.executable, "-c", BOOT],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []

    def reader():
        for line in proc.stdout:
            lines.append(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    def output():
        return "".join(lines)

    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            if "Starting main event loop" in output():
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if "Starting main event loop" not in output():
            proc.kill()
            pytest.fail("main loop never started; output:\n" + output())

        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            t.join(5)
            pytest.fail("shutdown hung after SIGTERM; output:\n" + output())
        t.join(5)
        out = output()
        assert rc == 0, "exit code %r; output:\n%s" % (rc, out)
        assert "Exiting after background tasks complete" in out, out
        assert "Shutdown complete" in out, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def test_main_loop_exits_when_a_handler_swallows_the_interrupt():
    # Backstop: SIGTERM also sets a flag that the catch-all checks, so an
    # except clause that fails while KeyboardInterrupt is in flight can't
    # turn the shutdown into a logged error.
    with open(os.path.join(REPO, "lib", "TWCManager", "TWCManager.py")) as f:
        source = f.read()
    handler = source[source.index("def sigterm_to_keyboard_interrupt") :]
    assert "shutdownRequested = True" in handler[: handler.index("signal.signal(")]
    catch_all = source[source.index('logger.info("Unhandled Exception:"') :]
    assert "if shutdownRequested:" in catch_all[: catch_all.index("time.sleep(5)")]


def test_vehicle_drains_leave_twenty_seconds_for_settings():
    # TWCManager.py runs at import: the helper is compiled from its source
    import ast

    path = os.path.join(REPO, "lib", "TWCManager", "TWCManager.py")
    source = open(path).read()
    fn = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name == "shutdownTimeouts"
    )
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), path, "exec"), ns)
    f = ns["shutdownTimeouts"]
    # 60 s at most for both drains, and 20 s always left for saveSettings
    assert f(1000.0, 1080.0) == 1060.0
    assert f(1000.0, 1050.0) == 1030.0
    assert f(1000.0, 1200.0) == 1060.0
    stop = source.index("master.reconciler.stop()")
    first = source.index("master.reconciler.drain(")
    second = source.index("master.vehicleCommandQueue.drain_high_priority(")
    assert stop < first < second
    assert "shutdownTimeouts(time.time(), shutdownDeadline)" in source[stop:first]
    assert source.index("master.reconciler.start()") > source.index(
        "master.vehicleCommandQueue.start()"
    )
