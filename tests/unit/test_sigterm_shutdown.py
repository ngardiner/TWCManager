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
