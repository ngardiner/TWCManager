from TWCManager.Vehicle.Registry.Fields import NOT_TRACKED
from TWCManager.Vehicle.Registry.ReadSource import ReadSource, cascade
from TWCManager.Vehicle.Registry.Vehicle import Vehicle

VIN = "5YJ3E1EA1KF000001"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Src(ReadSource):
    def __init__(self, name, order, result, fields=("chargeLimit", "batteryLevel")):
        self.name, self.order, self.fields = name, order, frozenset(fields)
        self.result, self.calls = result, []

    def fetch(self, vehicle, field, allowWake):
        self.calls.append((field, allowWake))
        assert not vehicle._fields._lock.locked() and not vehicle._lock.locked()
        return self.result


class Reg:
    def __init__(self, *sources):
        self.sources = list(sources)

    def readSources(self):
        return sorted(self.sources, key=lambda s: s.order)

    def sourceLiveAt(self, source, vehicle):
        return NOT_TRACKED  # polled sources only here


def test_fresh_value_returned_without_fetch():
    c = Clock()
    src = Src("BLE", 0, {"chargeLimit": 90})
    v = Vehicle(VIN, VIN, Reg(src), clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1010.0
    assert v.read("chargeLimit", maxAge=30).value == 80 and src.calls == []


def test_stale_value_cascades_in_order_and_stops_at_first_success():
    c = Clock()
    ble = Src("BLE", 0, None)
    api = Src("API", 1, {"chargeLimit": 90, "batteryLevel": 55})
    v = Vehicle(VIN, VIN, Reg(api, ble), clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1100.0
    obs = v.read("chargeLimit", maxAge=30)
    assert (obs.value, obs.source, obs.at) == (90, "API", 1100.0)
    assert v.latest("batteryLevel").value == 55
    assert ble.calls == [("chargeLimit", False)]
    assert api.calls == [("chargeLimit", False)]


def test_allow_wake_is_passed_through_and_defaults_false():
    src = Src("BLE", 0, {"chargeLimit": 1})
    v = Vehicle(VIN, VIN, Reg(src))
    v.read("chargeLimit", maxAge=0)
    v.read("chargeLimit", maxAge=0, allowWake=True)
    assert [w for _, w in src.calls] == [False, True]


def test_max_age_none_never_fetches():
    src = Src("BLE", 0, {"chargeLimit": 1})
    v = Vehicle(VIN, VIN, Reg(src))
    assert v.read("chargeLimit", maxAge=None) is None and src.calls == []


def test_source_without_field_or_declining_is_skipped():
    loc = Src("LOC", 0, {"lat": 1.0}, fields=("lat", "lon"))
    shy = Src("SHY", 0, {"chargeLimit": 5})
    shy.canRead = lambda vehicle: False
    v = Vehicle(VIN, VIN, Reg(loc, shy))
    assert v.read("chargeLimit", maxAge=0) is None
    assert loc.calls == [] and shy.calls == []


def test_all_sources_fail_returns_stale_latest():
    c = Clock()
    src = Src("BLE", 0, None)
    v = Vehicle(VIN, VIN, Reg(src), clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 5000.0
    assert v.read("chargeLimit", maxAge=1).value == 80


def test_raising_source_counts_as_failure_and_next_is_tried():
    c = Clock()
    ble = Src("BLE", 0, None)

    def boom(vehicle, field, allowWake):
        raise RuntimeError("radio")

    ble.fetch = boom
    api = Src("API", 1, {"chargeLimit": 7})
    v = Vehicle(VIN, VIN, Reg(ble, api), clock=c)
    assert v.read("chargeLimit", maxAge=0).value == 7


def test_empty_result_means_source_wrote_it_itself():
    c = Clock()
    v = Vehicle(VIN, VIN, None, clock=c)

    class Self(Src):
        def fetch(self, vehicle, field, allowWake):
            vehicle.observe(field, 3, self.name)
            return {}

    assert cascade(v, [Self("S", 0, None)], "chargeLimit", False).value == 3


def test_no_registry_returns_latest():
    v = Vehicle(VIN, VIN, None)
    assert v.read("chargeLimit", maxAge=0) is None
    v.observe("chargeLimit", 1, "X")
    assert v.read("chargeLimit", maxAge=-1).value == 1
