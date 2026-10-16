import json
import threading
from unittest.mock import Mock, patch

from TWCManager.TWCMaster import TWCMaster

CONFIG = {
    "config": {
        "debugOutputToFile": False,
        "subtractChargerLoad": False,
        "treatGenerationAsGridDelivery": False,
        "wiringMaxAmpsAllTWCs": 80,
        "maxAmpsAllowedFromGrid": None,
        "settingsPath": None,
    }
}


class CountingLock:
    """A real RLock that counts context-manager entries (Mock cannot be used with `with`)."""

    def __init__(self):
        self._lock = threading.RLock()
        self.enters = 0

    def __enter__(self):
        self.enters += 1
        return self._lock.__enter__()

    def __exit__(self, *exc):
        return self._lock.__exit__(*exc)

    def _is_owned(self):
        return self._lock._is_owned()


def make(tmp_path):
    cfg = {"config": dict(CONFIG["config"], settingsPath=str(tmp_path))}
    m = TWCMaster("AB", cfg)
    api = Mock()
    api.fleetSlot = Mock(accessToken="", refreshToken="", expireTime=0, source="pasted")
    api.ownerSlot = Mock(accessToken="", refreshToken="", expireTime=0, source="pasted")
    m.getModuleByName = Mock(side_effect=lambda n: api if n == "TeslaAPI" else None)
    return m


def test_settings_lock_is_reentrant(tmp_path):
    m = make(tmp_path)
    with m.settingsLock:
        with m.settingsLock:
            m.settings["x"] = 1
    assert m.settings["x"] == 1


def test_save_serialises_under_lock_and_writes_outside(tmp_path):
    m = make(tmp_path)
    seen = {}
    real_dumps = json.dumps

    def dumps(obj, *a, **k):
        seen["dumps_locked"] = m.settingsLock._is_owned()
        return real_dumps(obj, *a, **k)

    real_open = open

    def opener(path, *a, **k):
        if str(path).endswith(".tmp"):
            seen["write_locked"] = m.settingsLock._is_owned()
        return real_open(path, *a, **k)

    with patch("TWCManager.TWCMaster.json.dumps", dumps), patch(
        "builtins.open", opener
    ):
        m.saveSettings()
    assert seen == {"dumps_locked": True, "write_locked": False}
    assert json.load(open(tmp_path / "settings.json"))["chargeStopMode"] == "1"


def test_off_thread_master_writers_take_the_lock(tmp_path):
    m = make(tmp_path)
    m.queue_background_task = Mock()
    m.settingsLock = CountingLock()
    m.heldLimits.set("5YJ3E1EA1KF000001", 90, 80)
    m.heldLimits.remove("5YJ3E1EA1KF000001")
    m.setChargeNowAmps(10)
    m.setHomeLat(1.0)
    assert m.settingsLock.enters == 4


def test_load_reads_file_outside_lock_and_assigns_under_it(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"chargeStopMode": "2"}))
    m = make(tmp_path)
    seen = {}
    real_open = open

    def opener(path, *a, **k):
        if str(path).endswith("settings.json"):
            seen["open_locked"] = m.settingsLock._is_owned()
        return real_open(path, *a, **k)

    with patch("builtins.open", opener):
        m.loadSettings()
    assert seen == {"open_locked": False} and m.settings["chargeStopMode"] == "2"
