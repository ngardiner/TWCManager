"""A fresh install (no settings.json) loads the same normalised settings
as an existing file would, so pages and policy that read them work."""

import json

from tests.unit.test_settings_lock import make
from types import SimpleNamespace

from tests.unit.test_vehicles_page import render


def test_fresh_install_creates_builtin_vehicle_groups(tmp_path):
    m = make(tmp_path)
    m.loadSettings()
    groups = m.settings["VehicleGroups"]
    assert groups["Allow Charging"]["Members"] == []
    assert groups["Deny Charging"]["Members"] == []
    assert m.settings["sunrise"] == 6
    assert m.settings["sunset"] == 20


def test_fresh_install_matches_empty_settings_file(tmp_path):
    fresh = make(tmp_path / "fresh")
    (tmp_path / "fresh").mkdir()
    fresh.loadSettings()
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "settings.json").write_text(json.dumps({}))
    empty = make(tmp_path / "empty")
    empty.loadSettings()
    assert fresh.settings == empty.settings


def test_fresh_install_vehicles_page_renders(tmp_path):
    m = make(tmp_path)
    m.loadSettings()
    html = render(m)
    assert "Allow Charging" in html


def test_fresh_install_vin_entitlement_does_not_raise(tmp_path):
    m = make(tmp_path)
    m.loadSettings()
    twc = SimpleNamespace(currentVIN="5YJ3E1EA1KF000001")
    assert m.checkVINEntitlement(twc) == 1
