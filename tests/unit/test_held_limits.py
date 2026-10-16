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
VIN4 = "5YJ3E1EA1KF000004"


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
    assert m.settings["heldLimits"][VIN2] == {"outside": 90, "held": 50}
    # a numeric ID waits, on disk where it was, for a vehicle list
    assert m.settings["chargeLimits"] == {"12345": 85} and m.heldLimits.waiting()
    m.heldLimits.resolveLegacyIds({12345: VIN3})
    assert m.heldLimits.get(VIN3) == HeldLimit(85, None)
    assert "chargeLimits" not in m.settings and not m.heldLimits.waiting()
    assert m.heldLimits.holding() == [VIN2]


def test_set_and_remove():
    m, s = make()
    m.heldLimits.load()
    m.heldLimits.set(VIN.lower(), 80, 50)
    assert m.heldLimits.get(VIN) == HeldLimit(80, 50)
    m.heldLimits.set(VIN, 80, None)
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)
    m.heldLimits.remove(VIN)
    assert m.heldLimits.get(VIN) is None


def test_believed_prefers_a_newer_read_over_our_write():
    m, s = make()
    v = m.vehicleRegistry.getOrCreate(VIN, "t")
    v.observe("chargeLimit", 90, "TeslaAPI")
    assert v.believed("chargeLimit").value == 90
    v.noteWrite("chargeLimit", 50, "TeslaBLE")
    # our write is newer than the read
    assert v.believed("chargeLimit").value == 50
    v.observe(
        "chargeLimit", 80, "TeslaMateVehicle", at=v.lastWrite("chargeLimit").at + 1
    )
    assert v.believed("chargeLimit").value == 80
    v.observe(
        "chargingState", "Charging", "Gen2TWC", at=v.lastWrite("chargeLimit").at + 2
    )
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
        VIN3: {"outside": 80, "held": None},
    }
    # the numeric IDs wait for a vehicle list; the malformed ones are gone
    assert m.settings["chargeLimits"] == {
        "1492931337": [75, 60],
        "1492931338": [85, 0],
        "1492931339": 70,
        # held out of range: kept, and the held value is discarded on resolving
        "999": [80, 30],
    }
    m.heldLimits._save.assert_called_once_with()
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    # three dropped (bad outside values), one held value discarded
    assert len(warned) == 4 and all("chargeLimits" in r.getMessage() for r in warned)
    assert sum("Dropped" in r.getMessage() for r in warned) == 3
    assert sorted(m.settings["heldLimits"]) == sorted([VIN, VIN2, VIN3])
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
    assert onDisk["heldLimits"] == m.settings["heldLimits"]
    assert sorted(onDisk["chargeLimits"]) == [
        "1492931337",
        "1492931338",
        "1492931339",
        "999",
    ]
    m2 = fileMaster(tmp_path, onDisk)
    m2.loadSettings()
    assert m2.heldLimits.get(VIN2) == HeldLimit(90, 50)
    m2.heldLimits._save.assert_not_called()
    m2.heldLimits.resolveLegacyIds({1492931337: VIN4})
    assert m2.heldLimits.get(VIN4) == HeldLimit(75, 60)


def test_an_existing_store_keeps_its_own_cars(tmp_path):
    blob = legacySettings()
    blob["heldLimits"] = {VIN: {"outside": 66, "held": None}}
    m = fileMaster(tmp_path, blob)
    m.loadSettings()
    # its own car is not overwritten; the others migrate as usual
    assert m.heldLimits.get(VIN) == HeldLimit(66, None)
    assert m.heldLimits.get(VIN2) == HeldLimit(90, 50)
    assert "1492931337" in m.settings["chargeLimits"]
    m.heldLimits._save.assert_called_once_with()


def test_no_legacy_key_no_save(tmp_path):
    m = fileMaster(tmp_path, {"chargeStopMode": "1"})
    m.loadSettings()
    assert "heldLimits" not in m.settings and m.heldLimits.holding() == []
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


def test_resolving_keeps_an_existing_vin_entry():
    m, s = make()
    m.heldLimits.set(VIN, 70, 55)
    m.settings["chargeLimits"] = {"777": [90, None]}
    m.heldLimits.load()
    m.heldLimits.resolveLegacyIds({777: VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(70, 55)
    assert "chargeLimits" not in m.settings


def test_resolving_replaces_a_malformed_vin_entry():  # slim Task 11 re-review nit
    m, s = make()
    m.settings["heldLimits"] = {VIN: {"outside": "x"}}
    m.settings["chargeLimits"] = {"777": [90, None]}
    m.heldLimits.load()
    m.settings["heldLimits"][VIN] = {"outside": "x"}
    m.heldLimits.resolveLegacyIds({777: VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(90, None)


def test_store_works_before_load_and_on_a_bare_master():
    ns = SimpleNamespace(
        settings={}, queue_background_task=Mock(), settingsLock=threading.RLock()
    )
    h = HeldLimits(ns)
    assert h.get(VIN) is None and h.holding() == []
    h.remove(VIN)
    h.set(VIN, 80, 50)
    assert ns.settings["heldLimits"][VIN] == {"outside": 80, "held": 50}
    ns.queue_background_task.assert_called_with({"cmd": "saveSettings"})


def test_teslaapi_listing_without_a_store_is_harmless():
    from tests.unit.test_carapivehicle_registry import api_with_registry

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    assert reg.get(VIN) is not None


def test_store_never_keeps_the_sentinel():
    m, s = make()
    m.heldLimits.set(VIN, 80, -1)
    assert m.settings["heldLimits"][VIN] == {"outside": 80, "held": None}


def test_out_of_range_held_keeps_the_outside_limit(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80, 30], VIN2: [80, 101]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)
    assert m.heldLimits.get(VIN2) == HeldLimit(80, None)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and not any("Dropped" in w for w in warned)


def test_invalid_outside_is_dropped(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: ["80", 50], VIN2: [None, 50], VIN3: 80}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    assert m.heldLimits.get(VIN) is None and m.heldLimits.get(VIN2) is None
    assert m.heldLimits.get(VIN3) == HeldLimit(80, None)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and all("Dropped" in w for w in warned)


def test_case_collision_warns_naming_both_keys(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN.lower(): [70, None], VIN: [80, 50]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    # the later key wins, and the warning says which one
    assert m.heldLimits.get(VIN) == HeldLimit(80, 50)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert VIN.lower() in warned[0] and VIN in warned[0].replace(VIN.lower(), "")


# Fix round 1


def test_integral_floats_round_trip_as_ints():
    # Home Assistant observes chargeLimit/batteryLevel as floats.
    m, s = make()
    m.heldLimits.set(VIN3, 80.0, 60)
    assert m.settings["heldLimits"][VIN3] == {"outside": 80, "held": 60}
    # limitOverride's minBatteryLevelAtHome - 1 from a float batteryLevel
    m.heldLimits.set(VIN, 80, 66.0)
    assert m.heldLimits.get(VIN) == HeldLimit(80, 66)
    assert type(m.settings["heldLimits"][VIN]["held"]) is int
    m.settings["heldLimits"][VIN2] = {"outside": 80.0, "held": None}
    assert m.heldLimits.get(VIN2) == HeldLimit(80, None)


def test_integral_floats_migrate():
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80.0, 50], VIN2: [80, 50.0], VIN3: 70.0}
    m.heldLimits.load()
    assert m.settings["heldLimits"] == {
        VIN: {"outside": 80, "held": 50},
        VIN2: {"outside": 80, "held": 50},
        VIN3: {"outside": 70, "held": None},
    }


def test_non_integral_floats_are_invalid(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80.5, 50], VIN2: [80, 50.5]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    # an invalid outside drops the entry; an invalid held keeps the outside
    assert m.heldLimits.get(VIN) is None
    assert m.heldLimits.get(VIN2) == HeldLimit(80, None)
    m.settings["heldLimits"][VIN3] = {"outside": 80.5, "held": None}
    assert m.heldLimits.get(VIN3) is None


def test_set_rounds_finite_numbers():
    # limitOverride's minBatteryLevelAtHome - 1 from a fractional batteryLevel
    m, s = make()
    m.heldLimits.set(VIN, 80.4, 65.3)
    assert m.heldLimits.get(VIN) == HeldLimit(80, 65)
    assert m.settings["heldLimits"][VIN] == {"outside": 80, "held": 65}
    m.heldLimits.set(VIN2, 79.6, 65.3)
    assert m.heldLimits.get(VIN2) == HeldLimit(80, 65)


def test_set_refuses_what_is_not_a_finite_number(caplog):
    m, s = make()
    m.heldLimits.set(VIN, 70, None)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for outside, held in (
            ("80", None),
            (None, None),
            (True, None),
            (float("nan"), None),
            (float("inf"), None),
            (80, "50"),
            (80, float("nan")),
            (80, float("-inf")),
        ):
            m.heldLimits.set(VIN, outside, held)
    assert m.heldLimits.get(VIN) == HeldLimit(70, None)
    assert len(caplog.records) == 8


def test_listing_before_load_is_resolved_on_the_next_check(tmp_path, requests_mock):
    # TeslaMate token sync and retained telemetry can list cars before
    # loadSettings runs: the next checkCharge resolves (free calls only)
    from tests.unit.teslatokens import FLEET_NA_URL, fleet_token
    from tests.unit.test_carapivehicle_registry import api_with_registry

    m = fileMaster(tmp_path, {"chargeLimits": {"1492931337": [75, 60]}})
    api, reg = api_with_registry()
    api.storeToken(fleet_token())
    api.master.heldLimits = m.heldLimits
    listing = (
        '{"count": 1, "response": [{"id": 1492931337, "vin": "%s", "display_name": null}]}'
        % VIN3
    )
    requests_mock.get(FLEET_NA_URL, text=listing)
    assert api.loadVehicleList() is True and m.heldLimits.get(VIN3) is None
    m.getModuleByName = Mock(side_effect=lambda n: api if n == "TeslaAPI" else None)
    m.loadSettings()
    api.updateChargeAtHome()
    assert (
        m.heldLimits.get(VIN3) == HeldLimit(75, 60) and "chargeLimits" not in m.settings
    )
    assert [q.method for q in requests_mock.request_history] == ["GET", "GET"]


def test_both_keys_present_the_store_wins(tmp_path, caplog):
    blob = {
        "heldLimits": {VIN: {"outside": 66, "held": None}},
        # an out-of-range held value on a car the store keeps: no warning
        "chargeLimits": {VIN: [95, 101]},
    }
    m = fileMaster(tmp_path, blob)
    with caplog.at_level(logging.WARNING):
        m.loadSettings()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert m.heldLimits.get(VIN) == HeldLimit(66, None)
    assert "chargeLimits" not in m.settings


def test_set_refuses_a_non_vin_and_maps_zero_to_none(caplog):
    m, s = make()
    with caplog.at_level(logging.WARNING):
        m.heldLimits.set("anon:8244", 80, 50)
    assert "heldLimits" not in m.settings and caplog.records
    m.heldLimits.set(VIN, 80, 0)
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)


# One-pass migration (Phase 3.5 Task 1): characterization


def test_waiting_is_false_before_load():
    # loadSettings reads chargeLimits just before load(): nothing waits sooner
    ns = SimpleNamespace(
        settings={"chargeLimits": {"11": 80}},
        queue_background_task=Mock(),
        settingsLock=threading.RLock(),
    )
    h = HeldLimits(ns)
    assert not h.waiting()
    h.resolveLegacyIds({11: VIN})
    assert ns.settings == {"chargeLimits": {"11": 80}}
    ns.queue_background_task.assert_not_called()


def test_an_id_naming_a_vin_load_migrated_keeps_that_entry(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: 70, "11": [90, 50]}
    m.heldLimits.load()
    with caplog.at_level(logging.INFO):
        m.heldLimits.resolveLegacyIds({11: VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(70, None)
    assert "chargeLimits" not in m.settings
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "11" in i and VIN in i and "kept normal limit 70%, discarded 90%" in i
        for i in infos
    )


def test_an_id_keys_out_of_range_hold_warns_at_load_not_at_resolve(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {"11": [80, 30]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "Kept the normal limit" in warned[0]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        m.heldLimits.resolveLegacyIds({11: VIN})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)


def test_a_malformed_store_entry_is_kept_over_a_vin_key_at_load():
    m, s = make()
    m.settings["heldLimits"] = {VIN: {"outside": "x"}}
    m.settings["chargeLimits"] = {VIN: 80}
    m.heldLimits.load()
    assert m.settings["heldLimits"] == {VIN: {"outside": "x"}}
    assert "chargeLimits" not in m.settings


def test_a_resolve_saves_once_and_logs_no_migrated_line(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {"11": 80}
    m.heldLimits.load()
    m.queue_background_task = Mock()
    with caplog.at_level(logging.INFO):
        m.heldLimits.resolveLegacyIds({11: VIN})
    assert m.queue_background_task.call_args_list == [(({"cmd": "saveSettings"},),)]
    assert "Migrated chargeLimits" not in caplog.text
    assert m.heldLimits.get(VIN) == HeldLimit(80, None)


def test_a_non_dict_charge_limits_never_waits():  # pre-flight F9
    m, s = make()
    m.settings["chargeLimits"] = [["11", 80]]
    m.heldLimits.load()
    assert not m.heldLimits.waiting()
    assert m.settings["chargeLimits"] == [["11", 80]]
