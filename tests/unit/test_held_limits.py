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
        "12345": [85, 60],
        "bad": "x",
    }
    m.heldLimits.load()
    # holding nothing is no entry; a numeric ID waits, on disk, for a list
    assert m.heldLimits.get(VIN) is None
    assert m.heldLimits.get(VIN2) == HeldLimit(90, 50)
    assert m.settings["chargeLimits"] == {VIN2: [90, 50], "12345": [85, 60]}
    assert m.heldLimits.waiting()
    m.heldLimits.resolveLegacyIds({12345: VIN3})
    assert m.heldLimits.get(VIN3) == HeldLimit(85, 60)
    assert "12345" not in m.settings["chargeLimits"] and not m.heldLimits.waiting()
    assert m.heldLimits.holding() == [VIN2, VIN3]


def test_set_and_remove():
    m, s = make()
    m.heldLimits.load()
    m.heldLimits.set(VIN.lower(), 80, 50)
    assert m.heldLimits.get(VIN) == HeldLimit(80, 50)
    assert m.settings["chargeLimits"] == {VIN: [80, 50]}
    # nothing held: no entry
    m.heldLimits.set(VIN, 80, None)
    assert m.heldLimits.get(VIN) is None and m.settings["chargeLimits"] == {}
    m.heldLimits.set(VIN, 80, 50)
    m.heldLimits.remove(VIN)
    assert m.heldLimits.get(VIN) is None and m.settings["chargeLimits"] == {}


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
    # A 1.3.x settings.json: BLE stored VIN keys, TeslaAPI numeric IDs; JSON
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
            VIN4: 85,
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
    api = Mock()
    m.getModuleByName = Mock(side_effect=lambda n: api if n == "TeslaAPI" else None)
    m.queue_background_task = Mock()
    # loadSettings queues saves of its own; count the store's alone.
    m.heldLimits._save = Mock()
    return m


def test_realistic_settings_file_migrates(tmp_path, caplog):
    m = fileMaster(tmp_path, legacySettings())
    with caplog.at_level(logging.WARNING):
        m.loadSettings()
    # Only real holds stay: entries holding nothing (an int, held -1, 0 or
    # None) go silently, malformed or out-of-range ones with a warning; the
    # numeric ID waits for a vehicle list
    assert m.settings["chargeLimits"] == {VIN2: [90, 50], "1492931337": [75, 60]}
    m.heldLimits._save.assert_called_once_with()
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # three malformed (bad outside values), one held value out of range
    assert len(warned) == 4 and all(
        "Dropped" in w and "chargeLimits" in w for w in warned
    )
    assert sum("out of range" in w for w in warned) == 1
    assert [m.heldLimits.get(v) for v in (VIN, VIN2, VIN3, VIN4)] == [
        None,
        HeldLimit(90, 50),
        None,
        None,
    ]
    assert m.heldLimits.holding() == [VIN2] and m.heldLimits.waiting()


def test_a_clean_file_holding_only_real_limits_is_not_saved(tmp_path):
    m = fileMaster(tmp_path, {"chargeLimits": {VIN: [80, 50], "11": [90, 60]}})
    m.loadSettings()
    assert m.settings["chargeLimits"] == {VIN: [80, 50], "11": [90, 60]}
    m.heldLimits._save.assert_not_called()


def test_migration_is_idempotent_and_a_rerun_loses_nothing(tmp_path):
    m = fileMaster(tmp_path, legacySettings())
    m.loadSettings()
    first = json.loads(json.dumps(m.settings["chargeLimits"]))
    m.heldLimits._save.reset_mock()
    # a second load in the same run is a no-op
    m.heldLimits.load()
    assert m.settings["chargeLimits"] == first
    m.heldLimits._save.assert_not_called()
    # a restart from the saved (migrated) file changes nothing either
    saved = dict(m.settings)
    m2 = fileMaster(tmp_path, json.loads(json.dumps(saved)))
    m2.loadSettings()
    assert m2.settings["chargeLimits"] == first
    m2.heldLimits._save.assert_not_called()


def test_crash_before_the_save_reruns_the_same_migration(tmp_path):
    # The queued save never ran: the file is still the 1.3.x one.
    m = fileMaster(tmp_path, legacySettings())
    m.loadSettings()
    m2 = fileMaster(tmp_path, legacySettings())
    m2.loadSettings()
    assert m2.settings["chargeLimits"] == m.settings["chargeLimits"]


def test_saved_file_carries_the_store_and_restarts_clean(tmp_path):
    m = fileMaster(tmp_path, legacySettings())
    api = m.getModuleByName("TeslaAPI")
    for slot in (api.fleetSlot, api.ownerSlot):
        slot.accessToken, slot.refreshToken, slot.expireTime = "", "", 0
        slot.source = "pasted"
    m.loadSettings()
    m.heldLimits.set(VIN, 80, 60)
    m.heldLimits.set(VIN2, 90, None)
    m.saveSettings()
    onDisk = json.loads((tmp_path / "settings.json").read_text())
    # held: [outside, held] under the VIN; restored: no entry
    assert onDisk["chargeLimits"] == {VIN: [80, 60], "1492931337": [75, 60]}
    m2 = fileMaster(tmp_path, onDisk)
    m2.loadSettings()
    assert m2.heldLimits.get(VIN) == HeldLimit(80, 60)
    assert m2.heldLimits.get(VIN2) is None
    assert m2.heldLimits.holding() == [VIN]
    m2.heldLimits._save.assert_not_called()
    m2.heldLimits.resolveLegacyIds({1492931337: "5YJ3E1EA1KF000005"})
    assert m2.heldLimits.get("5YJ3E1EA1KF000005") == HeldLimit(75, 60)


def test_no_charge_limits_no_save(tmp_path):
    m = fileMaster(tmp_path, {"chargeStopMode": "1"})
    m.loadSettings()
    assert "chargeLimits" not in m.settings and m.heldLimits.holding() == []
    assert not m.heldLimits.waiting()
    m.heldLimits._save.assert_not_called()


def test_migration_swaps_under_the_lock():
    m, s = make()
    m.settings["chargeLimits"] = {VIN.lower(): [80, 50]}
    seen = {}

    class Watch(dict):
        def __setitem__(self, k, v):
            if k == "chargeLimits":
                seen["locked"] = m.settingsLock._is_owned()
            super().__setitem__(k, v)

    m.settings = Watch(m.settings)
    m.heldLimits.load()
    assert seen == {"locked": True}


# Keys and the interim wrappers


def test_resolving_keeps_an_existing_vin_entry():
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [70, 55], "777": [90, 60]}
    m.heldLimits.load()
    m.heldLimits.resolveLegacyIds({777: VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(70, 55)
    assert m.settings["chargeLimits"] == {VIN: [70, 55]}


def test_resolving_replaces_a_malformed_vin_entry():  # slim Task 11 re-review nit
    m, s = make()
    m.settings["chargeLimits"] = {"777": [90, 60]}
    m.heldLimits.load()
    m.settings["chargeLimits"][VIN] = {"outside": "x"}
    m.heldLimits.resolveLegacyIds({777: VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(90, 60)


def test_store_works_before_load_and_on_a_bare_master():
    ns = SimpleNamespace(
        settings={}, queue_background_task=Mock(), settingsLock=threading.RLock()
    )
    h = HeldLimits(ns)
    assert h.get(VIN) is None and h.holding() == [] and not h.waiting()
    h.remove(VIN)
    h.set(VIN, 80, 50)
    assert ns.settings["chargeLimits"][VIN] == [80, 50]
    ns.queue_background_task.assert_called_with({"cmd": "saveSettings"})


def test_teslaapi_listing_without_a_store_is_harmless():
    from tests.unit.test_teslaapi_registry import api_with_registry

    api, reg = api_with_registry()
    api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"})
    assert reg.get(VIN) is not None


def test_nothing_held_is_no_entry():
    m, s = make()
    for held in (-1, 0, None):
        m.heldLimits.set(VIN, 80, 50)
        m.heldLimits.set(VIN, 80, held)
        assert VIN not in m.settings["chargeLimits"] and m.heldLimits.get(VIN) is None


def test_out_of_range_held_is_dropped_with_a_warning(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80, 30], VIN2: [80, 101], VIN3: [80, 50]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    assert m.heldLimits.get(VIN) is None and m.heldLimits.get(VIN2) is None
    assert m.settings["chargeLimits"] == {VIN3: [80, 50]}
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and all("out of range" in w for w in warned)


def test_invalid_outside_is_dropped(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: ["80", 50], VIN2: [None, 50], VIN3: [80, 50]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    assert m.heldLimits.get(VIN) is None and m.heldLimits.get(VIN2) is None
    assert m.heldLimits.get(VIN3) == HeldLimit(80, 50)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2 and all("malformed" in w for w in warned)


def test_case_collision_warns_naming_both_keys(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN.lower(): [70, 60], VIN: [80, 50]}
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
    assert m.settings["chargeLimits"][VIN3] == [80, 60]
    # limitOverride's minBatteryLevelAtHome - 1 from a float batteryLevel
    m.heldLimits.set(VIN, 80, 66.0)
    assert m.heldLimits.get(VIN) == HeldLimit(80, 66)
    assert type(m.settings["chargeLimits"][VIN][1]) is int
    m.settings["chargeLimits"][VIN2] = [80.0, 60.0]
    assert m.heldLimits.get(VIN2) == HeldLimit(80, 60)


def test_integral_floats_read_as_ints():
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80.0, 50], VIN2: [80, 50.0], VIN3: 70.0}
    m.heldLimits.load()
    assert [m.heldLimits.get(v) for v in (VIN, VIN2, VIN3)] == [
        HeldLimit(80, 50),
        HeldLimit(80, 50),
        None,  # an int holds nothing
    ]
    assert type(m.heldLimits.get(VIN).outside) is int


def test_non_integral_floats_are_invalid(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [80.5, 50], VIN2: [80, 50.5]}
    with caplog.at_level(logging.WARNING):
        m.heldLimits.load()
    # an invalid outside or held value drops the entry, with a warning
    assert m.heldLimits.get(VIN) is None and m.heldLimits.get(VIN2) is None
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    m.settings.setdefault("chargeLimits", {})[VIN3] = [80.5, 60]
    assert m.heldLimits.get(VIN3) is None


def test_set_rounds_finite_numbers():
    # limitOverride's minBatteryLevelAtHome - 1 from a fractional batteryLevel
    m, s = make()
    m.heldLimits.set(VIN, 80.4, 65.3)
    assert m.heldLimits.get(VIN) == HeldLimit(80, 65)
    assert m.settings["chargeLimits"][VIN] == [80, 65]
    m.heldLimits.set(VIN2, 79.6, 65.3)
    assert m.heldLimits.get(VIN2) == HeldLimit(80, 65)


def test_set_refuses_what_is_not_a_finite_number(caplog):
    m, s = make()
    m.heldLimits.set(VIN, 70, 60)
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
    assert m.heldLimits.get(VIN) == HeldLimit(70, 60)
    assert len(caplog.records) == 8


def test_listing_before_load_is_resolved_on_the_next_check(tmp_path, requests_mock):
    # TeslaMate token sync and retained telemetry can list cars before
    # loadSettings runs: the next checkCharge resolves (free calls only)
    from tests.unit.teslatokens import FLEET_NA_URL, fleet_token
    from tests.unit.test_teslaapi_registry import api_with_registry

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
    assert m.heldLimits.get(VIN3) == HeldLimit(75, 60)
    assert m.settings["chargeLimits"] == {VIN3: [75, 60]}
    assert [q.method for q in requests_mock.request_history] == ["GET", "GET"]


def test_set_refuses_a_non_vin_and_maps_zero_to_no_entry(caplog):
    m, s = make()
    with caplog.at_level(logging.WARNING):
        m.heldLimits.set("anon:8244", 80, 50)
    assert "chargeLimits" not in m.settings and caplog.records
    m.heldLimits.set(VIN, 80, 50)
    m.heldLimits.set(VIN, 80, 0)
    assert m.heldLimits.get(VIN) is None


# One-pass migration (Phase 3.5 Task 1): characterization


def test_waiting_is_false_before_load():
    # loadSettings reads chargeLimits just before load(): nothing waits sooner
    ns = SimpleNamespace(
        settings={"chargeLimits": {"11": [80, 60]}},
        queue_background_task=Mock(),
        settingsLock=threading.RLock(),
    )
    h = HeldLimits(ns)
    assert not h.waiting()
    h.resolveLegacyIds({11: VIN})
    assert ns.settings == {"chargeLimits": {"11": [80, 60]}}
    ns.queue_background_task.assert_not_called()


def test_an_id_naming_a_vin_load_migrated_keeps_that_entry(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {VIN: [70, 55], "11": [90, 50]}
    m.heldLimits.load()
    with caplog.at_level(logging.INFO):
        m.heldLimits.resolveLegacyIds({11: VIN})
    assert m.heldLimits.get(VIN) == HeldLimit(70, 55)
    assert m.settings["chargeLimits"] == {VIN: [70, 55]}
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
    assert len(warned) == 1 and "out of range" in warned[0]
    assert not m.heldLimits.waiting()  # dropped at load: nothing waits
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        m.heldLimits.resolveLegacyIds({11: VIN})
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert m.heldLimits.get(VIN) is None


def test_a_resolve_saves_once_and_logs_no_migrated_line(caplog):
    m, s = make()
    m.settings["chargeLimits"] = {"11": [80, 60]}
    m.heldLimits.load()
    m.queue_background_task = Mock()
    with caplog.at_level(logging.INFO):
        m.heldLimits.resolveLegacyIds({11: VIN})
    assert m.queue_background_task.call_args_list == [(({"cmd": "saveSettings"},),)]
    assert "Migrated chargeLimits" not in caplog.text
    assert m.heldLimits.get(VIN) == HeldLimit(80, 60)


def test_a_non_dict_charge_limits_never_waits():  # pre-flight F9
    m, s = make()
    m.settings["chargeLimits"] = [["11", 80]]
    m.heldLimits.load()
    assert not m.heldLimits.waiting()
    assert m.settings["chargeLimits"] == [["11", 80]]
