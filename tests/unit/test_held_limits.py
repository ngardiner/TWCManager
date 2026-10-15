import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit, HeldLimits
from tests.unit.test_gen2twc_provider import CONFIG, make, VIN
from TWCManager.TWCMaster import TWCMaster

VIN2 = "5YJ3E1EA1KF000002"
VIN3 = "5YJ3E1EA1KF000003"


def test_legacy_charge_limits_migrate():  # Review Focus 3
    m, s = make()
    m.settings["chargeLimits"] = {
        VIN: (80, -1),
        VIN2.lower(): [90, 50],
        "12345": 85,
        "bad": "x",
    }
    m.heldLimits.load()
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)
    assert m.heldLimits.get(VIN2) == HeldLimit(90, 50)
    assert "chargeLimits" not in m.settings
    assert m.settings["heldLimits"][VIN2] == {"outside": 90, "held": 50}
    assert m.settings["heldLimits"]["id:12345"] == {"outside": 85, "held": None}
    # the interim wrapper still answers by ID
    assert m.getNormalChargeLimit(12345) == (True, 85, -1)
    m.heldLimits.adoptLegacyId(12345, VIN3)
    assert m.heldLimits.get(VIN3) == HeldLimit(85, None)
    assert "id:12345" not in m.settings["heldLimits"]
    assert m.heldLimits.holding() == [VIN2]


def test_set_remove_and_wrappers_keep_working():
    m, s = make()
    m.heldLimits.load()
    m.heldLimits.set(VIN, 80, 50)
    assert m.getNormalChargeLimit(VIN) == (True, 80, 50)
    m.heldLimits.set(VIN, 80, None)
    # wrapper keeps the old tuple until Task 10
    assert m.getNormalChargeLimit(VIN) == (True, 80, -1)
    m.heldLimits.remove(VIN)
    assert m.heldLimits.get(VIN) is None
    assert m.getNormalChargeLimit(VIN) == (False, None, None)


def test_believed_prefers_a_newer_read_over_our_write():
    m, s = make()
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    v.observe("chargeLimit", 90, "TeslaAPI")
    assert v.believed("chargeLimit").value == 90
    v.noteWrite("chargeLimit", 50, "TeslaBLE")
    # our write is newer than the read
    assert v.believed("chargeLimit").value == 50
    v.observe("chargeLimit", 80, "TeslaMateVehicle", at=v.lastWrite("chargeLimit").at + 1)
    assert v.believed("chargeLimit").value == 80
    v.observe("chargingState", "Charging", "Gen2TWC", at=v.lastWrite("chargeLimit").at + 2)
    assert v.believed("chargingState", exclude={"Gen2TWC"}) is None
    assert v.latestFrom("chargingState").source == "Gen2TWC"


def test_latest_from_skips_excluded_sources():
    m, s = make()
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    v.observe("chargingState", "Stopped", "TeslaAPI", at=100.0)
    v.observe("chargingState", "Charging", "Gen2TWC", at=200.0)
    assert v.latestFrom("chargingState", exclude={"Gen2TWC"}).value == "Stopped"
    assert v.believed("chargingState", exclude={"Gen2TWC"}).value == "Stopped"
    assert v.latestFrom("batteryLevel") is None and v.believed("batteryLevel") is None


def test_trusted_with_exclude_reads_past_the_excluded_source():
    m, s = make()
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    v.observe("chargeLimit", 90, "TeslaAPI", at=100.0)
    v.noteWrite("chargeLimit", 50, "TeslaBLE")
    w = v.lastWrite("chargeLimit").at
    v.observe("chargeLimit", 70, "TeslaAPI", at=w + 1)
    v.observe("chargeLimit", 60, "Gen2TWC", at=w + 2)
    assert v.trusted("chargeLimit").source == "Gen2TWC"
    assert v.trusted("chargeLimit", exclude={"Gen2TWC"}).value == 70
    # every source excluded: nothing is trusted, not the store's latest
    assert v.trusted("chargeLimit", exclude={"Gen2TWC", "TeslaAPI"}) is None


# Migration safety


def legacySettings():
    # An old settings.json: BLE stored VIN keys, TeslaAPI numeric IDs; JSON
    # turned the (outside, applied) tuples into lists.
    return {
        "chargeStopMode": "1",
        "chargeLimits": {
            VIN: [80, -1],
            VIN2: [90, 50],
            "1492931337": [75, 60],
            "1492931338": [85, 0],
            "1492931339": 70,
            VIN3.lower(): [80, None],
            "999": [80, 30],
            "998": ["80", 50],
            "997": [True, 50],
            "996": [80],
        },
    }


def fileMaster(tmp_path, blob):
    (tmp_path / "settings.json").write_text(json.dumps(blob))
    m = TWCMaster("AB", {"config": dict(CONFIG["config"], settingsPath=str(tmp_path))})
    # loadSettings hands saved tokens to TeslaAPI; no car is listed yet.
    api = Mock(carApiVehicles=[])
    m.getModuleByName = Mock(side_effect=lambda n: api if n == "TeslaAPI" else None)
    m.queue_background_task = Mock()
    # loadSettings queues saves of its own; count the store's alone.
    m.heldLimits._save = Mock()
    return m


def test_realistic_settings_file_migrates(tmp_path, caplog):
    m = fileMaster(tmp_path, legacySettings())
    with caplog.at_level(logging.WARNING):
        m.loadSettings()
    held = m.settings["heldLimits"]
    assert held == {
        VIN: {"outside": 80, "held": None},
        VIN2: {"outside": 90, "held": 50},
        "id:1492931337": {"outside": 75, "held": 60},
        "id:1492931338": {"outside": 85, "held": None},
        "id:1492931339": {"outside": 70, "held": None},
        VIN3: {"outside": 80, "held": None},
    }
    assert "chargeLimits" not in m.settings
    m.heldLimits._save.assert_called_once_with()
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 4 and all("chargeLimits" in r.getMessage() for r in warned)
    assert m.heldLimits.vins() == sorted([VIN, VIN2, VIN3])
    assert m.heldLimits.holding() == [VIN2]
    # the store survives a JSON round trip unchanged
    assert json.loads(json.dumps(m.settings))["heldLimits"] == held


def test_migration_is_idempotent_and_a_rerun_loses_nothing(tmp_path):
    m = fileMaster(tmp_path, legacySettings())
    m.loadSettings()
    first = json.loads(json.dumps(m.settings["heldLimits"]))
    m.heldLimits._save.reset_mock()
    # a second load in the same run is a no-op
    m.heldLimits.load()
    assert m.settings["heldLimits"] == first
    m.heldLimits._save.assert_not_called()
    # a restart from the saved (migrated) file changes nothing either
    saved = dict(m.settings)
    m2 = fileMaster(tmp_path, json.loads(json.dumps(saved)))
    m2.loadSettings()
    assert m2.settings["heldLimits"] == first
    m2.heldLimits._save.assert_not_called()


def test_crash_before_the_save_reruns_the_same_migration(tmp_path):
    # The queued save never ran: the file still has only chargeLimits.
    m = fileMaster(tmp_path, legacySettings())
    m.loadSettings()
    m2 = fileMaster(tmp_path, legacySettings())
    m2.loadSettings()
    assert m2.settings["heldLimits"] == m.settings["heldLimits"]


def test_saved_file_carries_the_store_and_restarts_clean(tmp_path):
    m = fileMaster(tmp_path, legacySettings())
    api = m.getModuleByName("TeslaAPI")
    for slot in (api.fleetSlot, api.ownerSlot):
        slot.accessToken, slot.refreshToken, slot.expireTime = "", "", 0
        slot.source = "pasted"
    m.loadSettings()
    m.saveSettings()
    onDisk = json.loads((tmp_path / "settings.json").read_text())
    assert "chargeLimits" not in onDisk
    assert onDisk["heldLimits"] == m.settings["heldLimits"]
    m2 = fileMaster(tmp_path, onDisk)
    m2.loadSettings()
    assert m2.heldLimits.get(VIN2) == HeldLimit(90, 50)
    assert m2.getNormalChargeLimit(1492931337) == (True, 75, 60)
    m2.heldLimits._save.assert_not_called()


def test_existing_store_wins_and_legacy_key_is_left_alone(tmp_path):
    blob = legacySettings()
    blob["heldLimits"] = {VIN: {"outside": 66, "held": None}}
    m = fileMaster(tmp_path, blob)
    m.loadSettings()
    assert m.heldLimits.get(VIN) == HeldLimit(66, None)
    assert m.heldLimits.get(VIN2) is None
    # left for a later sweep, never deleted without migrating
    assert m.settings["chargeLimits"] == blob["chargeLimits"]
    m.heldLimits._save.assert_not_called()


def test_no_legacy_key_no_save(tmp_path):
    m = fileMaster(tmp_path, {"chargeStopMode": "1"})
    m.loadSettings()
    assert "heldLimits" not in m.settings and m.heldLimits.vins() == []
    m.heldLimits._save.assert_not_called()


def test_migration_swaps_under_the_lock():
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80, 50]}
    seen = {}

    class Watch(dict):
        def __setitem__(self, k, v):
            if k == "heldLimits":
                seen["locked"] = m.settingsLock._is_owned()
            super().__setitem__(k, v)

    m.settings = Watch(m.settings)
    m.heldLimits.load()
    assert seen == {"locked": True}


# Keys and the interim wrappers


def test_wrappers_resolve_a_numeric_id_through_teslaapi():
    m, s = make()
    car = SimpleNamespace(ID=12345, VIN=VIN3)
    api = SimpleNamespace(carApiVehicles=[car])
    m.getModuleByName = Mock(side_effect=lambda n: api if n == "TeslaAPI" else None)
    m.settings["chargeLimits"] = {"12345": [85, 60]}
    m.heldLimits.load()
    # an unadopted id: entry is found and moved under the VIN
    assert m.getNormalChargeLimit(12345) == (True, 85, 60)
    assert m.heldLimits.get(VIN3) == HeldLimit(85, 60)
    assert "id:12345" not in m.settings["heldLimits"]
    m.saveNormalChargeLimit(12345, 85, -1)
    assert m.heldLimits.get(VIN3) == HeldLimit(85, None)
    m.removeNormalChargeLimit(12345)
    assert m.heldLimits.get(VIN3) is None


def test_wrapper_never_stores_the_sentinel():
    m, s = make()
    m.saveNormalChargeLimit(VIN.lower(), 80, -1)
    assert m.settings["heldLimits"] == {VIN: {"outside": 80, "held": None}}
    m.saveNormalChargeLimit(4242, 80, 0)
    assert m.settings["heldLimits"]["id:4242"] == {"outside": 80, "held": None}
    assert m.getNormalChargeLimit(4242) == (True, 80, -1)
    assert m.heldLimits.vins() == [VIN]


def test_adopt_keeps_an_existing_vin_entry():
    m, s = make()
    m.heldLimits.set(VIN, 70, 55)
    m.saveNormalChargeLimit(777, 90, None)
    m.queue_background_task = Mock()
    m.heldLimits.adoptLegacyId(777, VIN)
    assert m.heldLimits.get(VIN) == HeldLimit(70, 55)
    assert "id:777" in m.settings["heldLimits"]
    m.queue_background_task.assert_not_called()


def test_store_works_before_load_and_on_a_bare_master():
    ns = SimpleNamespace(
        settings={}, queue_background_task=Mock(), settingsLock=threading.RLock()
    )
    h = HeldLimits(ns)
    assert h.get(VIN) is None and h.vins() == [] and h.holding() == []
    h.remove(VIN)
    h.set(VIN, 80, 50)
    assert ns.settings["heldLimits"][VIN] == {"outside": 80, "held": 50}
    ns.queue_background_task.assert_called_with({"cmd": "saveSettings"})


def test_teslaapi_listing_adopts_the_legacy_id_once():
    from tests.unit.test_carapivehicle_registry import api_with_registry

    api, reg = api_with_registry()
    master = api.master
    master.heldLimits = HeldLimits(master)
    master.settings["chargeLimits"] = {"1492931337": [75, 60]}
    master.heldLimits.load()
    master.tasks.clear()
    api.addVehicle({"id": 1492931337, "vin": VIN, "display_name": "C"})
    assert master.heldLimits.get(VIN) == HeldLimit(75, 60)
    assert master.settings["heldLimits"] == {VIN: {"outside": 75, "held": 60}}
    assert master.tasks == [{"cmd": "saveSettings"}]
    # a rebind finds nothing left to adopt and saves nothing
    api.getCarApiVehicles()[0].vehicle
    assert master.tasks == [{"cmd": "saveSettings"}]


def test_teslaapi_listing_without_a_store_is_harmless():
    from tests.unit.test_carapivehicle_registry import api_with_registry

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    assert reg.get(VIN) is not None
