import json
import time
from unittest.mock import Mock

from tests.unit.test_settings_lock import make

VIN = "5YJ3E1EA1KF000001"


def test_registry_round_trips_through_settings_file(tmp_path):
    m = make(tmp_path)
    m.vehicleRegistry.getOrCreate(VIN, "TWC")
    m.saveSettings()
    saved = json.load(open(tmp_path / "settings.json"))
    assert VIN in saved["vehicleRegistry"]["vehicles"]
    m2 = make(tmp_path)
    m2.loadSettings()
    assert m2.vehicleRegistry.get(VIN) is not None


def test_old_settings_without_registry_key_loads_empty(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"chargeStopMode": "1"}))
    m = make(tmp_path)
    m.loadSettings()
    assert m.vehicleRegistry.vehicles() == []
    (tmp_path / "settings.json").write_text(json.dumps({"vehicleRegistry": "corrupt"}))
    m = make(tmp_path)
    m.loadSettings()
    assert m.vehicleRegistry.vehicles() == []


def test_expire_vehicles_saves_only_when_something_expired(tmp_path):
    m = make(tmp_path)
    m.queue_background_task = Mock()
    assert m.expireVehicles() == []
    m.queue_background_task.assert_not_called()
    m.vehicleRegistry.expire = Mock(return_value=[VIN])
    assert m.expireVehicles() == [VIN]
    m.queue_background_task.assert_called_once_with({"cmd": "saveSettings"})


def test_last_vehicle_expiry_starts_at_startup_time(tmp_path):
    before = time.time()
    m = make(tmp_path)
    assert before <= m.lastVehicleExpiry <= time.time()
