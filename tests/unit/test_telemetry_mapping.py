"""
Telemetry (TeslaMate etc.) -> registry vehicle mapping by name.

The retained display_name message arrives once at subscribe time; if no
registry vehicle has that name yet (the Tesla API vehicle list isn't loaded)
the mapping must be retried, not dropped.
"""

from unittest.mock import Mock

from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.Telemetry import TelmetryBase

VIN = "5YJ3E1EA1KF000001"


class FakeCarApi:
    """Names no registry vehicle until loadVehicleList() is called."""

    def __init__(self, registry, cars, load_ok=True):
        self.registry = registry
        self._pending = cars  # [(vin, name)]
        self.load_ok = load_ok
        self.loads = 0

    def getCarApiRetryRemaining(self):
        return 0

    def loadVehicleList(self):
        self.loads += 1
        if self.load_ok:
            for vin, name in self._pending:
                self.registry.getOrCreate(vin, "OwnerAPI").observe(
                    "name", name, "OwnerAPI"
                )
        return self.load_ok


class Probe(TelmetryBase):
    configName = "Probe"
    events = {"charge_limit_soc": ["chargeLimit", lambda a: int(a)]}
    unmappedRetrySecs = 3600  # never fires during a test

    def scheduleUnmappedRetry(self, delay=None):
        self.scheduled = True


def make(load_ok=True):
    registry = VehicleRegistry({})
    api = FakeCarApi(registry, [(VIN, "TestCar")], load_ok)
    master = Mock()
    master.vehicleRegistry = registry
    master.config = {"config": {}, "vehicle": {"Probe": {"enabled": True}}}
    master.getModuleByName.return_value = api
    return Probe(master), api, registry


def test_display_name_does_not_load_list_on_mqtt_thread():
    t, api, registry = make()
    t.applyDataToVehicle("1", "display_name", "TestCar")
    assert api.loads == 0  # no cloud call on the caller's thread
    assert "1" not in t.vehicles and t.scheduled

    t.retryUnmappedVehicles()  # timer thread does the load
    assert api.loads == 1
    assert t.vehicles["1"] is registry.get(VIN)


def test_events_before_name_are_replayed_after_mapping():
    t, api, registry = make()
    t.applyDataToVehicle("1", "charge_limit_soc", "80")  # arrives first
    t.applyDataToVehicle("1", "display_name", "TestCar")
    assert registry.get(VIN) is None
    t.retryUnmappedVehicles()
    obs = registry.get(VIN).latest("chargeLimit")
    assert (obs.value, obs.source) == (80, "Probe")


def test_unmatched_name_is_remembered_and_retried():
    t, api, registry = make(load_ok=False)
    t.applyDataToVehicle("1", "charge_limit_soc", "75")
    t.applyDataToVehicle("1", "display_name", "TestCar")
    t.retryUnmappedVehicles()
    assert "1" not in t.vehicles  # load failed; still remembered

    api.load_ok = True  # API comes back
    t.retryUnmappedVehicles()
    v = registry.get(VIN)
    assert t.vehicles["1"] is v
    assert v.latest("chargeLimit").value == 75  # parked event replayed
    assert v.latest("chargeLimit").source == "Probe"


def test_state_is_per_instance():
    a, _, _ = make()
    b, _, _ = make()
    a.vehicleNames["1"] = "x"
    assert b.vehicleNames == {}
