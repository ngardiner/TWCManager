import logging

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle import Presence as P
from TWCManager.Vehicle.Providers.TeslaAPIProviders import FleetCachedProvider
from TWCManager.Vehicle.Watch import DEFAULTS, Watch
from TWCManager.Vehicle.WatchBudget import EVENT
from tests.unit.reconcile_helpers import AWAY, HOME, listApi, placeAt, runPending
from tests.unit.test_command_path_parity import FLEET, OWNER, FleetCar, counts, paid
from tests.unit.test_limit_reconciler import VIN, setup
from tests.unit.test_watch import apiOnly, chargingFleetCar, later, passes, readsAt


def snapshotAt(requests_mock, stamp):
    # The car's last upload: stamp is read when asked, so a test moves it
    return requests_mock.get(
        FLEET + VIN + "/vehicle_data_cached",
        json=lambda req, ctx: {
            "response": {
                "charge_state": {"timestamp": int(stamp() * 1000), "charging_state": "Charging"},
                "vehicle_state": {},
            }
        },
    )


def firstRead(requests_mock, snapshot):
    # A charging car whose first (paid) read is done: the snapshot is absent then
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 1
    snapshotAt(requests_mock, snapshot)
    return m, r, v, c, w


def test_watch_cadence_ignores_the_cached_provider(requests_mock):
    m, r, v, c = setup(here=False); chargingFleetCar(requests_mock, m, v)
    assert any(p.name == "FleetAPICached" for p in m.providerOrder.candidates(v, C.READ_CHARGE))
    w = Watch(m, clock=c); w.readNow(VIN, "start"); w.pass_(); runPending(r.cloud)
    spent = m.watchBudget.spend; asked = []
    m.watchBudget.spend = lambda *a: asked.append(a) or spent(*a)
    later(m, v, c, 600); w.pass_(); runPending(r.cloud); assert asked == []      # inside cloudChargingReadSecs
    later(m, v, c, 300); w.pass_(); runPending(r.cloud)
    assert len(asked) == 1 and asked[0][0] == 0.002                               # due at 900, priced as paid Fleet

def test_watch_takes_a_snapshot_newer_than_its_last_read_for_free(requests_mock):
    stamp = [0.0]
    m, r, v, c, w = firstRead(requests_mock, lambda: stamp[0])
    before = m.watchBudget.tokens()
    later(m, v, c, 900); stamp[0] = c.t - 100; w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["cached"] == 2 and paid(requests_mock)[0] == 1
    assert m.watchBudget.tokens() == before

def test_watch_pays_only_when_the_cached_snapshot_is_older(requests_mock):
    stamp = [0.0]
    m, r, v, c, w = firstRead(requests_mock, lambda: stamp[0])
    before = m.watchBudget.tokens()
    later(m, v, c, 900); stamp[0] = c.t - 1000; w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["cached"] == 2 and paid(requests_mock)[0] == 2
    assert m.watchBudget.tokens() == before - 0.002

def test_watch_budget_refusal_behaves_as_today(requests_mock, caplog):
    stamp = [0.0]
    m, r, v, c, w = firstRead(requests_mock, lambda: stamp[0])
    m.watchBudget.spend(m.watchBudget.tokens(), EVENT)                  # nothing left
    caplog.set_level(logging.DEBUG, "TWCManager.Vehicle.Watch")
    later(m, v, c, 900); stamp[0] = c.t - 1000; w.pass_(); runPending(r.cloud)
    assert paid(requests_mock)[0] == 1 and "No budget" in caplog.text
    assert w._last[(VIN, "read")] == ("", c.t) and (VIN, "read") not in w._inflight   # settled, not a miss
    later(m, v, c, 60); w.pass_(); runPending(r.cloud)
    assert counts(requests_mock)["cached"] == 2                         # waits its cadence

def test_watch_never_waits(requests_mock, monkeypatch):
    stamp = [0.0]
    m, r, v, c, w = firstRead(requests_mock, lambda: stamp[0])
    seen = []; fetch = FleetCachedProvider.fetch
    monkeypatch.setattr(FleetCachedProvider, "fetch", lambda self, *a, **k: seen.append(k.get("wait", a[4:5] and a[4])) or fetch(self, *a, **k))
    later(m, v, c, 900); stamp[0] = c.t - 1000; w.pass_(); runPending(r.cloud)
    assert seen == [False]

def test_watch_reads_a_free_owner_car_live_without_the_cached_provider(requests_mock):
    # Both tokens: the Owner read is free and the cached provider is ready
    # (the Fleet slot), so only the price gate keeps it from being tried
    m, r, v, c = setup(here=False)
    for base in (OWNER, FLEET):
        FleetCar(requests_mock, state="online", charging="Charging", at=HOME, api=base)
    listApi(m, VIN, owner=True, fleet=True); placeAt(m, v, HOME)
    assert any(p.name == "FleetAPICached" and p.ready(v, C.READ_CHARGE) for p in m.providerOrder.candidates(v, C.READ_CHARGE))
    w = Watch(m, clock=c); w.pass_(); runPending(r.cloud); n = counts(requests_mock)
    w.readNow(VIN, "test"); w.pass_(); runPending(r.cloud)
    now = counts(requests_mock)
    assert now["cached"] == n["cached"] == 0 and paid(requests_mock) == (0, 0, 0)
    assert now["free"] > n["free"]

def test_a_cloud_only_car_due_for_its_fix_is_read_past_a_fresh_snapshot(requests_mock):
    # The snapshot carries no location: a car posting one after its last read
    # must still be seen leaving
    m, r, v, c, car = apiOnly(requests_mock, state="asleep"); placeAt(m, v, HOME)
    w = Watch(m, clock=c); passes(m, v, c, w, r, 1, 900)
    car.state = "online"; passes(m, v, c, w, r, 1, 900); passes(m, v, c, w, r, 1, 60)
    assert paid(requests_mock)[0] == 1                                   # the wake read
    stamp = c.t + 30
    requests_mock.get(
        FLEET + VIN + "/vehicle_data_cached",
        json={"response": {"charge_state": {"timestamp": int(stamp * 1000), "charging_state": "Disconnected"}, "vehicle_state": {}}},
    )
    car.at = AWAY                                                        # it drives off, awake
    gone = DEFAULTS["sleepCourtesySecs"] + DEFAULTS["homeAwakeSecs"]
    readsAt(requests_mock, m, v, c, w, r, c.t + gone)
    assert paid(requests_mock)[0] == 2 and P.presence(m, v) == P.NOT_HOME

def test_a_cloud_only_car_is_read_on_its_wake_past_a_fresh_snapshot(requests_mock):
    # A woken car may be leaving, or back: the wake read is for its position
    m, r, v, c, car = apiOnly(requests_mock, state="asleep"); m.config["watch"] = {"homeAsleepSecs": 300}
    placeAt(m, v, HOME); w = Watch(m, clock=c)
    assert readsAt(requests_mock, m, v, c, w, r, 1360) == []
    snapshotAt(requests_mock, lambda: c.t)                               # posted on the wake
    car.state = "online"
    assert readsAt(requests_mock, m, v, c, w, r, 2160) == [360]          # paid, not the snapshot
