"""
Telemetry (TeslaMate etc.) -> TeslaAPI vehicle mapping.

The retained display_name message arrives once at subscribe time; if the Tesla
API vehicle list isn't loaded yet the mapping must be retried, not dropped.
"""

from unittest.mock import Mock

from TWCManager.Vehicle.Telemetry import TelmetryBase


class FakeVehicle:
    def __init__(self, name):
        self.name = name
        self.syncSource = "TeslaAPI"
        self.chargeLimit = 0


class FakeCarApi:
    """Vehicle list is empty until loadVehicleList() is called."""

    def __init__(self, vehicles, load_ok=True):
        self._pending = vehicles
        self.carApiVehicles = []
        self.load_ok = load_ok
        self.loads = 0

    def getCarApiRetryRemaining(self):
        return 0

    def loadVehicleList(self):
        self.loads += 1
        if self.load_ok:
            self.carApiVehicles = list(self._pending)
        return self.load_ok

    def getCarApiVehicles(self):
        return self.carApiVehicles


class Probe(TelmetryBase):
    configName = "Probe"
    events = {"charge_limit_soc": ["chargeLimit", lambda a: int(a)]}
    unmappedRetrySecs = 3600  # never fires during a test

    def scheduleUnmappedRetry(self, delay=None):
        self.scheduled = True


def make(carapi):
    master = Mock()
    master.config = {"config": {}, "vehicle": {"Probe": {"enabled": True}}}
    master.getModuleByName.return_value = carapi
    return Probe(master)


def test_display_name_does_not_load_list_on_mqtt_thread():
    veh = FakeVehicle("Callandor")
    api = FakeCarApi([veh])
    t = make(api)
    t.applyDataToVehicle("1", "display_name", "Callandor")
    assert api.loads == 0  # no cloud call on the caller's thread
    assert "1" not in t.vehicles and t.scheduled

    t.retryUnmappedVehicles()  # timer thread does the load
    assert api.loads == 1
    assert t.vehicles["1"] is veh
    assert veh.syncSource == "Probe"


def test_events_before_name_are_replayed_after_mapping():
    veh = FakeVehicle("Callandor")
    t = make(FakeCarApi([veh]))
    t.applyDataToVehicle("1", "charge_limit_soc", "80")  # arrives first
    assert veh.chargeLimit == 0
    t.applyDataToVehicle("1", "display_name", "Callandor")
    t.retryUnmappedVehicles()
    assert veh.chargeLimit == 80


def test_unmatched_name_is_remembered_and_retried():
    veh = FakeVehicle("Callandor")
    api = FakeCarApi([veh], load_ok=False)
    t = make(api)
    t.applyDataToVehicle("1", "charge_limit_soc", "75")
    t.applyDataToVehicle("1", "display_name", "Callandor")
    t.retryUnmappedVehicles()
    assert "1" not in t.vehicles  # load failed; still remembered

    api.load_ok = True  # API comes back
    t.retryUnmappedVehicles()
    assert t.vehicles["1"] is veh
    assert veh.syncSource == "Probe"
    assert veh.chargeLimit == 75  # parked event replayed


def test_state_is_per_instance():
    a = make(FakeCarApi([]))
    b = make(FakeCarApi([]))
    a.vehicleNames["1"] = "x"
    assert b.vehicleNames == {}
