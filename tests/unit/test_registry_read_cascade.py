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
        self.lockHeld = False

    def fetch(self, vehicle, field, allowWake):
        self.calls.append((field, allowWake))
        if vehicle._fields._lock.locked() or vehicle._lock.locked():
            self.lockHeld = True
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
    assert not ble.lockHeld and not api.lockHeld


def test_allow_wake_is_passed_through_and_defaults_false():
    src = Src("BLE", 0, {"chargeLimit": 1})
    v = Vehicle(VIN, VIN, Reg(src))
    v.read("chargeLimit", maxAge=-1)
    v.read("chargeLimit", maxAge=-1, allowWake=True)
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


def test_raising_fetch_logs_no_message_or_traceback(caplog):
    import logging

    ble = Src("BLE", 0, None)

    def boom(vehicle, field, allowWake):
        raise RuntimeError("token=SECRET")

    ble.fetch = boom
    v = Vehicle(VIN, VIN, Reg(ble))
    with caplog.at_level(logging.DEBUG):
        v.read("chargeLimit", maxAge=0)
    assert "SECRET" not in caplog.text and "Traceback" not in caplog.text
    assert "RuntimeError" in caplog.text and "BLE" in caplog.text


def test_value_not_newer_than_our_write_is_not_fresh():
    c = Clock()
    src = Src("API", 1, {"chargeLimit": 90})
    v = Vehicle(VIN, VIN, Reg(src), clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1005.0
    v.noteWrite("chargeLimit", 90, "BLE")
    c.t = 1010.0
    obs = v.read("chargeLimit", maxAge=30)
    assert src.calls == [("chargeLimit", False)] and obs.value == 90


def test_value_not_newer_than_our_write_falls_back_to_latest():
    c = Clock()
    src = Src("API", 1, None)
    v = Vehicle(VIN, VIN, Reg(src), clock=c)
    v.observe("chargeLimit", 80, "X")
    c.t = 1005.0
    v.noteWrite("chargeLimit", 90, "BLE")
    c.t = 1010.0
    assert v.read("chargeLimit", maxAge=30).value == 80
    assert len(src.calls) == 1


def test_result_lacking_requested_field_continues_to_next_source():
    c = Clock()
    a = Src("A", 0, {"batteryLevel": 55})
    b = Src("B", 1, {"chargeLimit": 9})
    v = Vehicle(VIN, VIN, Reg(a, b), clock=c)
    assert v.read("chargeLimit", maxAge=0).value == 9
    assert v.latest("batteryLevel").value == 55 and len(b.calls) == 1


def test_read_with_sources_skips_other_sources():
    c = Clock()
    first = Src("A", 0, {"chargeLimit": 1})
    second = Src("B", 1, {"chargeLimit": 2})
    v = Vehicle(VIN, VIN, Reg(first, second), clock=c)
    assert v.read("chargeLimit", 0, sources=[second]).value == 2
    assert first.calls == [] and second.calls == [("chargeLimit", False)]


def test_read_with_sources_keeps_registry_order():
    c = Clock()
    first = Src("A", 0, None)
    second = Src("B", 1, {"chargeLimit": 2})
    v = Vehicle(VIN, VIN, Reg(first, second), clock=c)
    v.read("chargeLimit", 0, sources=[second, first])
    assert first.calls == [("chargeLimit", False)] and len(second.calls) == 1


class LiveReg(Reg):
    def __init__(self, *sources):
        super().__init__(*sources)
        self.liveness = {}

    def registerLiveness(self, source, liveAt):
        self.liveness[source] = liveAt

    def sourceLiveAt(self, source, vehicle):
        fn = self.liveness.get(source)
        return NOT_TRACKED if fn is None else fn(vehicle)


def test_force_reads_past_a_live_feed():
    c = Clock()
    src = Src("TeslaMateVehicle", 1, {"chargeLimit": 75})
    reg = LiveReg(src)
    reg.registerLiveness("TeslaMateVehicle", lambda vehicle: c())
    v = Vehicle(VIN, VIN, reg, clock=c)
    v.observe("chargeLimit", 80, "TeslaMateVehicle")
    c.t = 5000.0
    assert v.read("chargeLimit", 300).value == 80 and src.calls == []
    assert v.read("chargeLimit", 300, force=True).value == 75
    assert src.calls == [("chargeLimit", False)]


def test_force_still_never_fetches_without_max_age():
    src = Src("BLE", 0, {"chargeLimit": 1})
    v = Vehicle(VIN, VIN, Reg(src))
    assert v.read("chargeLimit", None, force=True) is None and src.calls == []
