"""distributeEVSEPower admits points while their minimums fit, drawing
points first, then shares the power among them at one water level."""

import pytest

from TWCManager import TWCMaster as T
from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.TWCMaster import TWCMaster
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

V = 240


class Point(EVSEInstance):
    # A charging point at V volts, its limits in amps
    isReadOnly = False

    def __init__(self, key, low, high, drawing=True, controllers=("Gen2TWCs",)):
        self.vehicleKey = self.ID = key
        # Its own member, a TWC drawing 10 A or none
        self.wantsToCharge, self.localEndpoints = drawing, [self]
        self.reportedAmpsActual = 10 if drawing else 0
        self.minPower, self.maxPower = low * V, high * V
        self.controllers, self.currentVoltage = list(controllers), [V, 0, 0]
        self.target = None

    def setTargetPower(self, watts):
        self.target = watts


def _master(points, amps, gen3=None, willing=(), monkeypatch=None):
    # willing: the keys of the cars that would charge if started
    m = TWCMaster.__new__(TWCMaster)
    # Wiring for 400 A: no budget but the feed binds
    m.config = {
        "config": {
            "defaultVoltage": V,
            "numberOfPhases": 1,
            "wiringMaxAmpsAllTWCs": 400,
        }
    }
    m.vehicleRegistry = VehicleRegistry(m.config)
    for key in willing:
        m.vehicleRegistry.getOrCreate(key, "t")
    if monkeypatch:
        monkeypatch.setattr(T, "wouldCharge", lambda m, v: v.key in willing)
    m.getModuleByName = lambda n: gen3 if n == "Gen3TWCs" else None
    m.getVoltageMeasurement = lambda: (V, 1)
    m.chargingPoints = lambda: points
    m.getMaxAmpsToDivideAmongSlaves = lambda: amps
    m.distributeEVSEPower()
    return m


def amps(points):
    return [round(p.target / V, 6) for p in points]


def test_distributor_visits_every_evse():
    points = [Point("p%d" % i, 5, 29 + i) for i in range(3)]
    _master(points, 100)
    assert all(p.target is not None for p in points)


def test_each_point_at_its_maximum_when_the_total_is_more_than_enough():
    points = [Point("a", 5, 30), Point("b", 5, 12), Point("c", 5, 20)]
    _master(points, 100)
    assert amps(points) == [30, 12, 20]


def test_a_gen3_and_a_car_share_at_one_level():
    gen3, car = Point("g", 16, 48), Point("v", 5, 32)
    _master([gen3, car], 30)
    assert amps([gen3, car]) == [16, 14]


def test_a_gen3_and_two_cars_share_at_one_level():
    points = [Point("g", 16, 48), Point("v", 5, 32), Point("w", 5, 32)]
    _master(points, 40)
    assert amps(points) == [16, 12, 12]


def test_a_capped_point_leaves_the_rest_to_the_other():
    a, b = Point("a", 6, 16), Point("b", 6, 48)
    _master([a, b], 48)
    assert amps([a, b]) == [16, 32]


A, B = "5YJ3E1EA1KF00000A", "5YJ3E1EA1KF00000B"


def test_a_minimum_that_does_not_fit_wastes_no_share(monkeypatch):
    # EVSE A (6-32) and EVSE B (20-32) at 25 A, each with a car that would
    # charge: one is admitted and takes it all. Neither drawing, point
    # order decides
    for first in (A, B):
        a, b = Point(A, 6, 32, False), Point(B, 20, 32, False)
        points = [a, b] if first == A else [b, a]
        m = _master(points, 25, willing=(A, B), monkeypatch=monkeypatch)
        assert amps(points) == [25, 0] and m.admission == {first}


def test_with_no_car_that_would_charge_nothing_is_admitted(monkeypatch):
    a, b = Point(A, 6, 32, False), Point(B, 20, 32, False)
    m = _master([a, b], 25, monkeypatch=monkeypatch)
    assert amps([a, b]) == [0, 0]
    # Either minimum fits, not both: the first takes the room
    assert m.admission == {A}


def test_the_drawing_point_keeps_its_seat(monkeypatch):
    for drawing in (A, B):
        a, b = Point(A, 6, 32, drawing == A), Point(B, 20, 32, drawing == B)
        m = _master([a, b], 25, willing=(A, B), monkeypatch=monkeypatch)
        assert amps([a, b]) == ([25, 0] if drawing == A else [0, 25])
        assert m.admission == {drawing}


def test_a_gen3_budget_binds_its_units_as_a_group():
    # Two Gen3 units on a 32 A circuit and a car, at 60 A: the units share
    # their 32, and the car takes the rest up to its maximum
    class Gen3:
        maxPower = 32 * V

    g = [Point("g%d" % i, 6, 48, controllers=["Gen3TWCs"]) for i in range(2)]
    car = Point("v", 5, 32, controllers=["VehicleEndpoints"])
    _master(g + [car], 60, Gen3())
    assert amps(g + [car]) == [16, 16, 28]


def test_a_budget_that_fits_only_one_minimum_admits_one():
    class Gen3:
        maxPower = 10 * V

    g = [Point("g%d" % i, 6, 48, controllers=["Gen3TWCs"]) for i in range(2)]
    g[1].reportedAmpsActual = 0
    _master(g, 60, Gen3())
    assert amps(g) == [10, 0]
    # Both drawing: the second is held at its minimum, counted in the budget,
    # while the hold decides
    g[1].reportedAmpsActual = 10
    _master(g, 60, Gen3())
    assert amps(g) == [6, 6]


def test_a_car_side_arrival_never_evicts_a_drawing_gen2_car(monkeypatch):
    # A Gen2 car drawing (6 A minimum), a car-side arrival that would
    # charge (5 A), later in point order and earlier: at 11 A both fit,
    # 6 / 5; at 10 A the arrival waits and the Gen2 car keeps all 10
    for arrivalFirst in (False, True):
        gen2 = Point(A, 6, 32)
        car = Point(B, 5, 32, False, controllers=["VehicleEndpoints"])
        points = [car, gen2] if arrivalFirst else [gen2, car]
        m = _master(points, 11, willing=(B,), monkeypatch=monkeypatch)
        assert amps([gen2, car]) == [6, 5] and m.admission == {A, B}
        m = _master(points, 10, willing=(B,), monkeypatch=monkeypatch)
        assert amps([gen2, car]) == [10, 0] and m.admission == {A}


def test_the_drawing_car_keeps_its_seat_as_power_wavers_at_the_edge(monkeypatch):
    # Two cars of 6 A minimum, either of which fits, never both: the power
    # wavers under 12 A. Each pass sees what the last one offered: B,
    # drawing, keeps its seat though A comes first in point order
    a, b = Point(A, 6, 32, False), Point(B, 6, 32, True)
    seats = []
    for n in range(20):
        power = (11, 6, 9.5, 7)[n % 4]
        _master([a, b], power, willing=(A, B), monkeypatch=monkeypatch)
        for p in (a, b):
            p.reportedAmpsActual = 10 if p.target else 0
        seats.append(tuple(bool(p.target) for p in (a, b)))
    assert seats == [(False, True)] * 20


@pytest.mark.parametrize("kind", ["VehicleEndpoints", "Gen3TWCs"])
def test_an_arrival_gets_only_what_the_others_draw_leaves(kind):
    # A draws all 48 A when B's car arrives. Each is given 24 A at once, but
    # A's car takes a while to ramp down: until it has, B gets only what A's
    # draw leaves, never less than its minimum. The 48 A is the policy's for
    # car-side points, the Gen3 circuit's for units
    class Gen3:
        maxPower = 48 * V

    a, b = (Point(k, 6, 48, controllers=[kind]) for k in "ab")
    a.reportedAmpsActual, b.reportedAmpsActual = 48, 0
    feed = 48 if kind == "VehicleEndpoints" else 100
    seen = []
    for draw in (48, 40, 30, 24):
        a.reportedAmpsActual = draw
        _master([a, b], feed, Gen3())
        seen.append(amps([a, b]))
    assert seen == [[24, 6], [24, 8], [24, 18], [24, 24]]
