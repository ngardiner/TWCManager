from TWCManager.Vehicle.Providers import Capability as C
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


def test_provider_reads_once_enabled():
    ha, reg = make_ha()
    (p,) = ha.providers()
    assert p.available(C.READ_CHARGE) and p.declares(C.READ_CHARGE)


def test_disabled_reads_nothing():
    ha, reg = make_ha(enabled=False)
    (p,) = ha.providers()
    v = reg.getOrCreate(VIN, "HomeAssistant")
    v.attach("HomeAssistant", HaVehicle("Car", VIN, "d1", {}))
    assert ha.registry is reg and not p.available(C.READ_CHARGE)
    assert not p.ready(v, C.READ_CHARGE)


def test_provider_fetch_observes_its_fields():
    ha, reg = make_ha()
    (p,) = ha.providers()
    v = reg.getOrCreate(VIN, "HomeAssistant")
    assert not p.ready(v, C.READ_CHARGE)
    hv = HaVehicle("Car", VIN, "d1", {"battery_level_sensor": "sensor.b"})
    v.attach("HomeAssistant", hv)
    assert p.ready(v, C.READ_CHARGE)
    ha._get_state = Mock(return_value={"state": "61"})
    assert p.fetch(v, "batteryLevel") is True
    assert (v.latest("batteryLevel").value, v.latest("batteryLevel").source) == (61.0, "HomeAssistant")
    assert p.fetch(v, "chargeLimit") is False  # observed, but not the field asked


def test_ha_down_is_a_failed_read_and_cascade_continues():
    from TWCManager.Vehicle.Providers.Order import ProviderOrder
    from TWCManager.Vehicle.Providers.Provider import Provider

    ha, reg = make_ha()
    (p,) = ha.providers()
    v = reg.getOrCreate(VIN, "HomeAssistant")
    v.observe("batteryLevel", 40.0, "TeslaBLE")
    hv = HaVehicle("Car", VIN, "d1", {"battery_level_sensor": "sensor.b"})
    v.attach("HomeAssistant", hv)
    ha._get_state = Mock(return_value=None)
    assert p.fetch(v, "batteryLevel") is False
    assert hv.refresh_from_home_assistant(ha) is False

    class Next(Provider):
        name, rank, FIELDS = "Next", 7, frozenset({"batteryLevel"})
        DECLARATIONS = {C.READ_CHARGE: C.Declaration(C.HA_BILLED)}
        ready = lambda self, v, c: True

        def fetch(self, v, f, allowWake=False):
            v.observe("batteryLevel", 55.0, self.name)
            return True

    m = Mock()
    m.settings, m.config = {}, {}
    reg.providerOrder = ProviderOrder(m)
    reg.providerOrder.register(Next())
    reg.providerOrder.register(p)
    obs = v.read("batteryLevel", maxAge=0)
    assert (obs.value, obs.source) == (55.0, "Next")


def test_unobserved_field_not_advertised():
    ha, reg = make_ha()
    (p,) = ha.providers()
    assert "voltage" not in p.FIELDS and "gpsAsOf" not in p.FIELDS


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


def test_fetch_without_an_attachment_reads_nothing():  # 13b M3
    ha, reg = make_ha()
    (p,) = ha.providers()
    assert p.fetch(reg.getOrCreate(VIN, "t"), "batteryLevel") is False


def test_ha_logs_name_the_host_never_the_url_or_exception_text(caplog):
    # Final review M8: a URL can carry userinfo or a query token
    import logging

    import requests

    secret = "http://user:s3cret@ha.local:8123/?token=t0ken"
    master = Mock(vehicleRegistry=VehicleRegistry({}))
    master.config = {
        "config": {},
        "vehicle": {
            "HomeAssistant": {"enabled": True, "url": secret, "longLivedToken": "x" * 40}
        },
    }
    caplog.set_level(logging.DEBUG)
    for error in (requests.exceptions.ConnectionError(secret), ValueError(secret)):
        with patch("TWCManager.Vehicle.HomeAssistant.requests.Session") as S:
            S.return_value.get.side_effect = error
            HomeAssistant(master)
    with patch("TWCManager.Vehicle.HomeAssistant.requests.Session") as S, patch.object(
        HomeAssistant,
        "_discover_vehicles",
        side_effect=requests.exceptions.ConnectionError(secret),
    ):
        S.return_value.get.return_value = Mock(raise_for_status=Mock())
        HomeAssistant(master)  # discovery fails after connectivity is verified
    ha, reg = make_ha()
    ha._session = Mock()
    ha._session.get.side_effect = requests.exceptions.ConnectionError(secret)
    ha.entity_ids = {}
    ha._get_state("sensor.x")
    assert "ha.local" in caplog.text
    assert "s3cret" not in caplog.text and "t0ken" not in caplog.text
