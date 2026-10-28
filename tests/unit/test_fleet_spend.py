import threading
import time

import pytest

from TWCManager.Vehicle.FleetSpend import TELEMETRY_SIGNAL_USD, FleetSpend

A, B = "5YJ3E1EA1KF000001", "5YJ3E1EA1KF000002"


def at(y, mo, d, h=12, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def spend(t=None):
    clock = Clock(at(2026, 10, 20) if t is None else t)
    return FleetSpend(clock=clock), clock


def test_requests_add_up_per_car_and_type():
    s, clock = spend()
    s.request(A, "command:set_charge_limit", 0.001)
    clock.t += 60
    s.request(A, "command:set_charge_limit", 0.001)
    s.request(A, "wake_up", 0.02)
    s.request(B, "vehicle_data", 0.002)
    assert s.month()["api"] == pytest.approx(0.024)
    car = s.car(A)
    assert car["requests"] == 3
    assert car["types"][0]["kind"] == "wake_up"
    limit = next(t for t in car["types"] if t["kind"] == "command:set_charge_limit")
    assert limit["count"] == 2 and limit["last"] == clock.t


def test_a_request_with_no_car_counts_only_in_the_total():
    s, _ = spend()
    s.request(None, "vehicle_data", 0.002)
    assert s.month()["api"] == pytest.approx(0.002)
    assert s.car(A)["requests"] == 0


def test_signals_count_per_field_and_price_the_total():
    s, _ = spend()
    for _ in range(3):
        s.signal(A, "Location")
    s.signal(A, "ChargeAmps")
    car = s.car(A)
    assert car["signals"] == 4
    assert car["fields"][0] == {"field": "Location", "count": 3}
    assert s.month()["telemetry"] == pytest.approx(4 * TELEMETRY_SIGNAL_USD)


def test_a_new_month_moves_totals_to_last_month():
    s, clock = spend()
    s.request(A, "wake_up", 0.02)
    s.signal(A, "Location")
    clock.t = at(2026, 11, 2)
    m = s.month()
    assert m["api"] == 0 and m["signals"] == 0
    assert m["last"]["api"] == pytest.approx(0.02)
    assert m["last"]["telemetry"] == pytest.approx(TELEMETRY_SIGNAL_USD)
    assert s.car(A)["types"] == []


def test_a_restart_across_the_month_boundary_rolls_over():
    s, _ = spend(at(2026, 10, 31, 23, 59))
    s.request(A, "wake_up", 0.02)
    blob = s.toJSON()
    t, _ = spend(at(2026, 11, 1, 0, 1))
    t.load(blob)
    assert t.month()["api"] == 0
    assert t.month()["last"]["api"] == pytest.approx(0.02)
    assert t.car(A)["types"] == []


def test_two_months_on_last_month_is_zero():
    s, _ = spend()
    s.request(A, "wake_up", 0.02)
    t, _ = spend(at(2026, 12, 5))
    t.load(s.toJSON())
    assert t.month()["api"] == 0 and t.month()["last"]["api"] == 0


def test_pace_scales_spend_to_the_whole_month():
    s, clock = spend(at(2026, 11, 3))
    s.request(A, "command:x", 0.30)
    clock.t = at(2026, 11, 16, 0, 0)
    pace = s.month()["pace"]
    assert pace["api"] == pytest.approx(0.60, rel=0.01)
    assert pace["by"] == "Dec 1"
    clock.t = at(2026, 11, 1, 12)
    t, _ = spend(clock.t)
    assert t.month()["pace"] is None


@pytest.mark.parametrize(
    "blob", [None, {"month": 3}, {"month": "2026-10", "cars": "x"}, "junk"]
)
def test_a_bad_blob_starts_empty(blob):
    s, _ = spend()
    s.load(blob)
    assert s.month()["api"] == 0 and s.car(A)["requests"] == 0


def test_changed_clears_on_read():
    s, _ = spend()
    assert s.changed() is False
    s.request(A, "wake_up", 0.02)
    assert s.changed() is True
    assert s.changed() is False


def test_counting_from_threads_loses_nothing():
    s, _ = spend()

    def count():
        for _ in range(1000):
            s.signal(A, "Location")

    threads = [threading.Thread(target=count) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert s.car(A)["signals"] == 8000
