from unittest.mock import Mock, patch
from TWCManager.Vehicle.HomeAssistant import HomeAssistant, HaVehicle
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"


def make_ha(enabled=True):
    reg = VehicleRegistry({})
    master = Mock()
    master.vehicleRegistry = reg
    master.config = {
        "config": {},
        "vehicle": {
            "HomeAssistant": {
                "enabled": enabled,
                "url": "http://ha",
                "longLivedToken": "x" * 40,
            }
        },
    }
    with patch("TWCManager.Vehicle.HomeAssistant.requests.Session") as S:
        S.return_value.get.return_value = Mock(raise_for_status=Mock())
        with patch.object(HomeAssistant, "_discover_vehicles"):
            ha = HomeAssistant(master)
    return ha, reg


def test_discovered_vehicle_is_attached_and_claimed():
    ha, reg = make_ha()
    devices = [
        {
            "id": "d1",
            "name": "Car",
            "manufacturer": "Tesla",
            "identifiers": [["teslemetry", VIN]],
        }
    ]
    with patch.object(
        ha,
        "_map_tesla_entities",
        return_value={
            "charge_switch": "switch.car_charge",
            "battery_level_sensor": "sensor.b",
        },
    ):
        ha._build_vehicles_from_registry(devices, [])
    v = reg.get(VIN)
    assert (
        isinstance(v.attachment("HomeAssistant"), HaVehicle)
        and v.tier == "managed"
        and v.name == "Car"
    )


def test_refresh_observes_with_source():
    ha, reg = make_ha()
    hv = HaVehicle(
        "Car",
        VIN,
        "d1",
        {"battery_level_sensor": "sensor.b", "charge_limit_number": "number.l"},
    )
    reg.getOrCreate(VIN, "HomeAssistant").attach("HomeAssistant", hv)
    ha._get_state = Mock(
        side_effect=lambda ent: {
            "sensor.b": {"state": "73"},
            "number.l": {"state": "90"},
        }.get(ent)
    )
    hv.refresh_from_home_assistant(ha)
    v = reg.get(VIN)
    assert (v.latest("batteryLevel").value, v.latest("batteryLevel").source) == (
        73.0,
        "HomeAssistant",
    )
    assert v.latest("chargeLimit").value == 90.0
    # Only keys present in the responses are observed.
    assert v.latest("lat") is None and v.latest("chargingState") is None


def test_refresh_observes_charging_and_location():
    ha, reg = make_ha()
    hv = HaVehicle(
        "Car", VIN, "d1", {"charging_sensor": "s.c", "location_tracker": "d.l"}
    )
    reg.getOrCreate(VIN, "HomeAssistant")
    ha._get_state = Mock(
        side_effect=lambda ent: {
            "s.c": {"state": "charging"},
            "d.l": {"state": "away", "attributes": {"latitude": 1.5, "longitude": 2.5}},
        }.get(ent)
    )
    ha.is_location_home = Mock(return_value=False)
    hv.refresh_from_home_assistant(ha)
    v = reg.get(VIN)
    assert v.latest("chargingState").value == "Charging"
    assert (v.latest("lat").value, v.latest("lon").value) == (1.5, 2.5)


def test_refresh_does_not_resurrect_forgotten_vehicle():
    ha, reg = make_ha()
    hv = HaVehicle("Car", VIN, "d1", {"battery_level_sensor": "sensor.b"})
    ha._get_state = Mock(return_value={"state": "50"})
    hv.refresh_from_home_assistant(ha)
    assert reg.get(VIN) is None
    assert hv.battery_level == 50.0


def test_read_source_registered():
    ha, reg = make_ha()
    assert [s.name for s in reg.readSources()] == ["HomeAssistant"]


def test_disabled_registers_nothing():
    ha, reg = make_ha(enabled=False)
    assert ha.registry is reg and reg.readSources() == []


def test_read_source_fetch_returns_observed_fields():
    ha, reg = make_ha()
    src = reg.readSources()[0]
    v = reg.getOrCreate(VIN, "HomeAssistant")
    assert not src.canRead(v)
    hv = HaVehicle("Car", VIN, "d1", {"battery_level_sensor": "sensor.b"})
    v.attach("HomeAssistant", hv)
    assert src.canRead(v)
    ha._get_state = Mock(return_value={"state": "61"})
    assert src.fetch(v, "batteryLevel", False) == {"batteryLevel": 61.0}


def test_ha_down_is_a_failed_read_and_cascade_continues():
    from TWCManager.Vehicle.Registry.ReadSource import cascade

    ha, reg = make_ha()
    v = reg.getOrCreate(VIN, "HomeAssistant")
    v.observe("batteryLevel", 40.0, "TeslaBLE")
    hv = HaVehicle("Car", VIN, "d1", {"battery_level_sensor": "sensor.b"})
    v.attach("HomeAssistant", hv)
    ha._get_state = Mock(return_value=None)
    src = reg.readSources()[0]
    assert src.fetch(v, "batteryLevel", False) is None
    assert hv.refresh_from_home_assistant(ha) is False

    class Next:
        name = "Next"
        order = 3
        fields = frozenset({"batteryLevel"})
        canRead = lambda self, v: True
        fetch = lambda self, v, f, w: {"batteryLevel": 55.0}

    obs = cascade(v, [src, Next()], "batteryLevel", False)
    assert (obs.value, obs.source) == (55.0, "Next")


def test_unobserved_field_not_advertised_and_order_after_api():
    from TWCManager.Vehicle.Registry.Sources import TeslaAPIReadSource

    ha, reg = make_ha()
    src = reg.readSources()[0]
    assert "voltage" not in src.fields and "gpsAsOf" not in src.fields
    assert src.order > TeslaAPIReadSource.order


def test_refresh_fails_fast_after_connection_error():
    ha, reg = make_ha()
    hv = HaVehicle(
        "Car",
        VIN,
        "d1",
        {
            "battery_level_sensor": "a",
            "charge_limit_number": "b",
            "charging_sensor": "c",
        },
    )
    calls = []

    def get(ent):
        calls.append(ent)
        ha.connectionFailures += 1
        return None

    ha._get_state = get
    assert hv.refresh_from_home_assistant(ha) is False
    assert calls == ["a"]


def test_failed_registration_does_not_append_vehicle():
    ha, reg = make_ha()
    devices = [
        {"id": "d1", "name": "Car", "manufacturer": "Tesla", "serial_number": VIN}
    ]
    with patch.object(
        ha,
        "_map_tesla_entities",
        return_value={"charge_switch": "s.c", "battery_level_sensor": "s.b"},
    ), patch.object(reg, "getOrCreate", side_effect=ValueError("bad")):
        ha._build_vehicles_from_registry(devices, [])
    assert ha.carApiVehicles == []
