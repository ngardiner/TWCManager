# tests/unit/test_command_path_parity.py: the old-vs-new command behaviour
# per install type (slim Task 14), with real modules and counted requests.
#
# Real TWCMaster, real TeslaAPI (every HTTP request goes through
# requests_mock and is counted by URL), real TeslaBLE (only the tesla-control
# run is replaced: None is a radio that does not answer), real HomeAssistant
# (its session posts through requests_mock), and the real reconciler on a
# test clock with its queues run inline. Each test is one row of the parity
# table in the sdd report; the "old" numbers come from the same scenarios
# run against origin/main (the report's harness).
import re
import time
from unittest.mock import Mock, patch

import pytest
import requests
from requests_mock import ANY

from TWCManager.Vehicle import Presence
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit
from TWCManager.Vehicle.Reconcile.Reconciler import PAID_AFTER, RESTORE_RETRY, RETRY
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from tests.unit.gen2_helpers import drive as heartbeat, offersThenBeat
from tests.unit.reconcile_helpers import AWAY, HOME, listApi, placeAt, runPending
from tests.unit.test_limit_reconciler import VIN, setup

VIN2 = "5YJ3E1EA1KF000002"
FLEET = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/"
OWNER = "https://owner-api.teslamotors.com/api/1/vehicles/"
# Fleet API prices; vehicles/{vin} and the vehicle list are free
PRICE = {"vehicle_data": 0.002, "wake_up": 0.02, "command": 0.001}


def counts(requests_mock):
    # Every request, by kind: the paid three, Home Assistant service calls,
    # the free vehicle list and online check, and anything else
    out = dict.fromkeys(
        ("vehicle_data", "wake_up", "command", "ha", "free", "cached", "other"), 0
    )
    for q in requests_mock.request_history:
        path = q.url.split("?")[0]
        if "fleet-api" not in path and re.search(
            r"/(vehicle_data|wake_up|command/)", path
        ):
            # The Owner API bills nothing
            out["free"] += 1
        elif "/command/" in path:
            out["command"] += 1
        elif path.endswith("/wake_up"):
            out["wake_up"] += 1
        elif path.endswith("/vehicle_data_cached"):
            # The free snapshot read: billed nothing
            out["cached"] += 1
        elif path.endswith("/vehicle_data"):
            out["vehicle_data"] += 1
        elif "/api/services/" in path:
            out["ha"] += 1
        elif re.search(r"/api/1/(vehicles|products)(/[^/]+)?$|/live_status$", path):
            out["free"] += 1
        else:
            out["other"] += 1
    return out


def paid(requests_mock):
    c = counts(requests_mock)
    return (c["vehicle_data"], c["wake_up"], c["command"])


def dollars(requests_mock):
    c = counts(requests_mock)
    return round(sum(PRICE[k] * c[k] for k in PRICE), 4)


def commands(requests_mock):
    return [
        q.url.split("?")[0].rsplit("/", 1)[-1]
        for q in requests_mock.request_history
        if "/command/" in q.url
    ]


class FleetCar:
    # The car as the Fleet API sees it: asleep until woken (or awake), with
    # its charge state; commands answer per a script (status, result, reason)
    def __init__(
        self,
        requests_mock,
        vin=VIN,
        state="asleep",
        limit=50,
        charging="Stopped",
        soc=40,
        at=HOME,
        wakes=True,
        script=None,
        api=FLEET,
    ):
        self.vin, self.state, self.wakes = vin, state, wakes
        self.charge = {
            "charge_limit_soc": limit,
            "charging_state": charging,
            "battery_level": soc,
            "charge_current_request": 32,
            "charger_pilot_current": 32,
            "scheduled_charging_pending": False,
        }
        self.at, self.script = at, list(script or [(200, True, "")])
        self.shift = None
        base = api + vin
        requests_mock.get(
            base, json=lambda req, ctx: {"response": {"state": self.state, "vin": vin}}
        )
        requests_mock.post(base + "/wake_up", json=self._wake)
        requests_mock.get(base + "/vehicle_data", json=self._data)
        # No snapshot to read: the free read before the paid one finds nothing
        requests_mock.get(base + "/vehicle_data_cached", status_code=404, json={})
        requests_mock.post(
            re.compile(re.escape(base) + r"/command/.*"), json=self._command
        )

    def _wake(self, req, ctx):
        if self.wakes:
            self.state = "online"
        return {"response": {"state": self.state}}

    def _data(self, req, ctx):
        if self.state != "online":
            ctx.status_code = 408
            return {"response": None, "error": "vehicle unavailable"}
        return {
            "response": {
                "drive_state": {
                    "latitude": self.at[0],
                    "longitude": self.at[1],
                    "shift_state": self.shift,
                },
                "charge_state": dict(self.charge),
                "vehicle_state": {},
            }
        }

    def _command(self, req, ctx):
        if self.state != "online":
            ctx.status_code = 408
            return {"response": None, "error": "vehicle unavailable"}
        status, result, reason = (
            self.script.pop(0) if len(self.script) > 1 else self.script[0]
        )
        ctx.status_code = status
        if status != 200:
            return {"response": None, "error": "x"}
        if result:
            name = req.url.rsplit("/", 1)[-1]
            body = req.json() if req.body else {}
            if name == "set_charge_limit":
                self.charge["charge_limit_soc"] = body["percent"]
            elif name == "charge_start":
                self.charge["charging_state"] = "Charging"
            elif name == "charge_stop":
                self.charge["charging_state"] = "Stopped"
        return {"response": {"result": result, "reason": reason}}


@pytest.fixture(autouse=True)
def quiet(monkeypatch, requests_mock):
    # No real sleeps (wake settle, low-amps repeat, BLE quick retry), and any
    # URL nothing else answers is counted under "other", never raised
    import TWCManager.Vehicle.Providers.TeslaAPIProviders as providers
    import TWCManager.Vehicle.TeslaAPI as teslaapi
    import TWCManager.Vehicle.TeslaBLE as teslable

    for module in (teslaapi, teslable, providers):
        monkeypatch.setattr(module.time, "sleep", lambda s: None)
    requests_mock.register_uri(ANY, ANY, status_code=599, json={})


def install(
    fleet=False,
    owner=False,
    policy=None,
    held=None,
    at=HOME,
    located=True,
    signing=None,
):
    # A real master, store and reconciler on a test clock; a real TeslaAPI
    # with the given token slots, the car listed and placed by TeslaMate's
    # free position
    m, r, v, c = setup(here=False)
    if policy:
        m.settings["commandPolicy"] = policy
    api = listApi(m, VIN, fleet, owner, signing) if fleet or owner else None
    if located:
        placeAt(m, v, at)
    if held:
        m.heldLimits.set(VIN, *held)
    return m, r, v, c, api


def realBle(m, answers="ok", vins=(VIN,), ping=True):
    # The real TeslaBLE module; only the tesla-control run is replaced.
    # answers: an output string, None (the radio does not answer: a timeout,
    # which the module retries once), or a callable(command, args). ping:
    # the body-controller-state answer (True: VCSEC answered awake; False;
    # or a callable(vin)), recorded in b.pings
    m.settings.setdefault("Vehicles", {}).update(
        {vin: {"privKey": "x"} for vin in vins}
    )
    m.config["config"]["teslaControlPath"] = "/bin/true"
    with patch.object(TeslaBLE, "_start_dbus_session"), patch.object(
        TeslaBLE, "_cleanup_stale_pipe"
    ):
        b = TeslaBLE(m)
    b.sendPrivateKey = Mock(return_value=True)
    b._ensure_pipe_closed = Mock()
    b.runs, b.answers = [], answers

    def run(vin, command, args=None):
        b.runs.append((vin, command, args))
        out = b.answers(command, args) if callable(b.answers) else b.answers
        b._failure.transient = out is None
        return out

    b._sendCommand_internal = run
    b._fetch_state_json = lambda vin, kind: None
    b.pings = []

    def _ping(vin, scan=True):
        b.pings.append(vin)
        if ping is True:
            m.vehicleRegistry.get(vin).observe("state", "online", "TeslaBLE")
        return ping(vin) if callable(ping) else ping

    b._runPing = _ping
    for p in b.providers():
        m.providerOrder.register(p)
    for vin in vins:
        m.vehicleRegistry.getOrCreate(vin, "t").claim("TeslaBLE", True, "paired")
    return b


def bleCommands(b, vin=VIN):
    return [cmd for v, cmd, args in b.runs if v == vin]


def realHa(m, requests_mock, *cars):
    # The real HomeAssistant module, its REST session on requests_mock (every
    # service call answers 200); cars are (vin, entity map)
    from TWCManager.Vehicle.HomeAssistant import HaVehicle, HomeAssistant

    m.config["vehicle"] = dict(
        m.config.get("vehicle", {}),
        HomeAssistant={"enabled": True, "url": "http://ha", "longLivedToken": "x" * 40},
    )
    with patch("TWCManager.Vehicle.HomeAssistant.requests.Session"), patch.object(
        HomeAssistant, "_discover_vehicles"
    ):
        ha = HomeAssistant(m)
    ha._session = requests.Session()
    requests_mock.post(re.compile(r"http://ha/api/services/.*"), json=[])
    for p in ha.providers():
        m.providerOrder.register(p)
    for vin, entities in cars:
        m.vehicleRegistry.getOrCreate(vin, "t").attach(
            "HomeAssistant", HaVehicle("Car", vin, "d-" + vin, entities)
        )
    return ha


def passes(r, c, seconds, step=10, each=None):
    for _ in range(int(seconds / step)):
        c.t += step
        if each:
            each()
        r.pass_()


# Row 4: Fleet-only, asleep; a policy raise 50 -> 80


def test_fleet_only_raise_costs_the_same(requests_mock):
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    car = FleetCar(requests_mock, limit=50)
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 3600)
    assert paid(requests_mock) == (1, 1, 1) and commands(requests_mock) == [
        "set_charge_limit"
    ]
    assert counts(requests_mock)["free"] == 1 and counts(requests_mock)["other"] == 0
    assert car.charge["charge_limit_soc"] == 80 and m.heldLimits.get(VIN) == HeldLimit(
        90, 80
    )
    assert dollars(requests_mock) == 0.023


# Row 4b (#699, merge plan Finding 1): Fleet-only, asleep; wake_up answers
# offline and the car comes online 59 s later. As main (#699): the free
# online check notices it and the limit goes out, with no second wake. The
# reads and the cost are row 4's: one wake, the read, one command


def wakeUps(requests_mock):
    # wake_up requests on either API (counts() bills only the Fleet API's)
    return sum(q.url.endswith("/wake_up") for q in requests_mock.request_history)


def offlineWake(requests_mock, m, r, c, want, seconds=1800, online=59, api=FLEET):
    # Runs want() every pass for seconds; wake_up answers offline and the car
    # comes online online s after it. Returns {"wake": t, "online": t, "sent":
    # t}: the wake, the car online, the first command
    car = FleetCar(requests_mock, limit=50, api=api)
    requests_mock.post(api + VIN + "/wake_up", json={"response": {"state": "offline"}})
    seen = {}

    def each():
        want()
        if wakeUps(requests_mock) and "wake" not in seen:
            seen["wake"] = c()
        if "wake" in seen and c() - seen["wake"] >= online and "online" not in seen:
            car.state, seen["online"] = "online", c()
        if commands(requests_mock) and "sent" not in seen:
            seen["sent"] = c()

    passes(r, c, seconds, step=1, each=each)
    return car, seen


def test_fleet_only_car_online_after_an_offline_wake_gets_the_limit(requests_mock):
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    car, seen = offlineWake(requests_mock, m, r, c, lambda: None, seconds=3600)
    assert seen["sent"] - seen["online"] <= 150  # the free check noticed it
    assert paid(requests_mock) == (1, 1, 1)  # one wake in the hour: none again
    assert commands(requests_mock) == ["set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 80 and dollars(requests_mock) == 0.023
    assert m.desired.record(VIN, CHARGE_LIMIT).paidSends == 2  # the wake, the send


def test_fleet_only_car_online_seconds_after_an_offline_wake_gets_the_limit(
    requests_mock, monkeypatch
):
    # wake_up answers offline and the car connects seconds later: the polls
    # after the wake find it, and the limit goes out in the same pass
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    m, r, v, c, api = install(fleet=True, held=(90, 50))
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    car = FleetCar(requests_mock, limit=50)
    requests_mock.post(
        FLEET + VIN + "/wake_up", json={"response": {"state": "offline"}}
    )
    sleeps = []

    def sleep(secs):
        sleeps.append(secs)
        if len(sleeps) == 2:
            car.state = "online"

    monkeypatch.setattr(teslaapi.time, "sleep", sleep)
    r.pass_()
    assert wakeUps(requests_mock) == 1
    assert commands(requests_mock) == ["set_charge_limit"]
    passes(r, c, 3600)
    assert paid(requests_mock) == (0, 1, 1)  # one wake, no second
    assert car.charge["charge_limit_soc"] == 80


def wakeTimes(requests_mock, c, also=lambda: None):
    # A want() for offlineWake that records when each wake_up went out
    times = []

    def want():
        also()
        times.extend([c()] * (wakeUps(requests_mock) - len(times)))

    return times, want


@pytest.mark.parametrize("online, wakes", [(2400, 2), (4000, 3)])
def test_fleet_only_car_online_after_a_later_wake_gets_the_limit(
    requests_mock, online, wakes
):
    # Online after the second wake (+2400 s, inside its hold) or the third
    # (+4000 s): the arm ended on a failed wake, so the desire's one re-arm
    # keeps the free checks and the held wakes going
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    times, want = wakeTimes(requests_mock, c)
    car, seen = offlineWake(
        requests_mock, m, r, c, want, seconds=4 * 3600, online=online
    )
    assert seen["sent"] - seen["online"] <= 150
    assert paid(requests_mock) == (1, wakes, 1)
    assert all(b - a > 1800 for a, b in zip(times, times[1:]))
    assert car.charge["charge_limit_soc"] == 80


def test_fleet_only_wake_http_errors_then_online_gets_the_limit(requests_mock):
    # wake_up answers HTTP 500 twice (an ERROR, not asleep), and the car
    # comes online 600 s after the second: those failed wakes reached
    # nothing, so the arm re-arms as for an offline wake and the limit goes
    # out; at most 4 paid
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    car = FleetCar(requests_mock, limit=50)
    requests_mock.post(FLEET + VIN + "/wake_up", status_code=500, json={})
    times, want = wakeTimes(requests_mock, c)
    seen = {}

    def each():
        want()
        if len(times) == 2 and c() - times[1] >= 600 and "online" not in seen:
            car.state, seen["online"] = "online", c()
        if commands(requests_mock) and "sent" not in seen:
            seen["sent"] = c()

    passes(r, c, 4 * 3600, step=1, each=each)
    assert len(times) == 2 and times[1] - times[0] > 1800
    assert seen["sent"] - seen["online"] <= 150
    assert paid(requests_mock) == (1, 2, 1) and sum(paid(requests_mock)) <= 4
    assert commands(requests_mock) == ["set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 80


@pytest.mark.parametrize("start, wakes", [(False, 4), (True, 7)])
def test_fleet_only_car_never_online_after_an_offline_wake(requests_mock, start, wakes):
    # The car never answers, 4 h. A limit: each of its two arms (I2) gets a
    # wake and, after the 30-minute hold, one more: 4 wakes, then nothing.
    # With a start too, the start's desire spends its own: 7 in 4 h (the
    # worst case), never two inside one hold
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    for field, value in (
        ("chargeLimit", 50),
        ("chargingState", "Stopped"),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    also = (lambda: m.startCarsCharging(VIN)) if start else (lambda: None)
    times, want = wakeTimes(requests_mock, c, also)
    offlineWake(requests_mock, m, r, c, want, seconds=4 * 3600, online=10**9)
    assert paid(requests_mock) == (0, wakes, 0) and counts(requests_mock)["other"] == 0
    assert all(b - a > 1800 for a, b in zip(times, times[1:]))
    # Only free checks between: one per WAKE_CHECK while a follow-up is due
    # (47 and 94 measured), and the polls after each wake
    free = counts(requests_mock)["free"]
    assert free <= (50 if not start else 100) + wakes * api.WAKE_POLLS


def test_every_billed_wake_holds_the_next(requests_mock):
    # Provider.wake's contract, which the reconciler's WAKE_CHECK follow-ups
    # rely on: a failed wake by any billed provider that can wake the car
    # makes the next one, inside WAKE_CHECK, a free "wake held", no wake_up
    from TWCManager.Vehicle.Reconcile.Reconciler import WAKE_CHECK

    m, r, v, c, api = install(fleet=True, owner=True, signing="off")
    wakers = [
        p
        for p in m.providerOrder.providers()
        if p.declares(C.WAKE)
        and p.cost(C.WAKE).tier >= C.TIER_BILLED
        and p.ready(v, C.WAKE)
    ]
    assert sorted(p.name for p in wakers) == ["FleetAPI", "OwnerAPI"]
    for p in wakers:
        m, r, v, c, api = install(fleet=True, owner=True, signing="off")
        p = m.providerOrder.provider(p.name)
        requests_mock.reset_mock()
        requests_mock.get(
            re.compile(r".*/vehicles/%s$" % VIN),
            json={"response": {"state": "asleep"}},
        )
        requests_mock.post(
            re.compile(r".*/%s/wake_up$" % VIN),
            json={"response": {"state": "asleep"}},
        )
        first = p.wake(VIN)
        assert first.kind in O.RETRYABLE and not O.isNotSent(first)
        c.t += WAKE_CHECK
        assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held"), p.name
        assert wakeUps(requests_mock) == 1


def test_fleet_only_car_online_after_an_offline_wake_gets_the_start(requests_mock):
    # The same for a start (CHARGING), which wakes on its own
    m, r, v, c, api = install(fleet=True)
    for field, value in (
        ("chargingState", "Stopped"),
        ("chargeLimit", 80),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "TeslaMateVehicle", at=c() - 600)
    car, seen = offlineWake(
        requests_mock, m, r, c, lambda: m.startCarsCharging(VIN), seconds=3600
    )
    assert seen["sent"] - seen["online"] <= 150
    assert paid(requests_mock)[1:] == (1, 1) and commands(requests_mock) == [
        "charge_start"
    ]
    assert car.charge["charging_state"] == "Charging"


def test_owner_off_car_online_after_an_offline_wake_gets_the_limit(requests_mock):
    # Row 4b for a car listed command_signing "off" on an Owner token: its
    # wake takes the same hold, so the same single wake, all of it free
    m, r, v, c, api = install(owner=True, signing="off", held=(90, 50))
    v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    car, seen = offlineWake(
        requests_mock, m, r, c, lambda: None, seconds=3600, api=OWNER
    )
    assert seen["sent"] - seen["online"] <= 150 and wakeUps(requests_mock) == 1
    assert commands(requests_mock) == ["set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 80 and paid(requests_mock) == (0, 0, 0)
    assert counts(requests_mock)["other"] == 0


# Row 1: pure Gen2, no vehicle module; the heartbeat stops and starts


def test_pure_gen2_sets_desired_and_attempts_nothing(requests_mock):
    m, r, v, c, api = install(located=False)
    anon = m.vehicleRegistry.beginPlugSession("8244")
    m.stopCarsCharging(anon.key)
    m.startCarsCharging(anon.key)
    assert (
        m.desired.keys() == [] and r.pass_() == 0
    )  # mode 1: the unaddressed fan-out finds no car
    assert m.settings.get("respondToSlaves", 1) == 1 and requests_mock.call_count == 0


# Row 2: BLE-only; the policy lowers the limit twice (90 -> 70 -> 60)


def test_ble_only_limits_never_wake_to_lower(requests_mock):
    m, r, v, c, api = install()
    b = realBle(m)
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.setPolicyLimit(70, transition=True)
    passes(r, c, 600)
    m.setPolicyLimit(60, transition=True)
    passes(r, c, 600)
    # The car is expected to charge (battery unknown): each lower wakes first
    assert bleCommands(b) == ["wake", "charging-set-limit"] * 2
    assert [args for vin, cmd, args in b.runs] == [
        None,
        70,
        None,
        60,
    ] and requests_mock.call_count == 0
    assert m.heldLimits.get(VIN) == HeldLimit(90, 60)


# Row 3b (#659): an Owner token and a car listed command_signing "off" take
# the Fleet-only paths of rows 4, 9 and 28 (and a car that never wakes),
# request for request and with the same per-desire paid records, on the
# free Owner API


def path_shape(requests_mock):
    # Each request's method and path below .../api/1/, the host dropped. The
    # free snapshot read only a Fleet token makes is left out: the parity is
    # between the paid paths.
    return [
        (q.method, q.url.split("?")[0].split("/api/1/")[-1])
        for q in requests_mock.request_history
        if not q.url.split("?")[0].endswith("/vehicle_data_cached")
    ]


def paid_records(m):
    # The per-desire paid accounting the reconciler keeps, per aspect
    out = []
    for aspect in (CHARGE_LIMIT, CHARGING):
        rec = m.desired.record(VIN, aspect)
        out.append(
            rec
            and (
                rec.paidSends,
                rec.paidReached,
                rec.paidRetryDue,
                rec.paidAt,
                rec.lastOutcome,
                rec.lastOutcomeAt,
            )
        )
    return out


def off_car_run(requests_mock, scenario, base, **tokens):
    # One scenario on one token; returns (commands, request shape, the paid
    # records after each pass)
    requests_mock.reset_mock()
    m, r, v, c, api = install(
        signing="off", held=(90, 50) if scenario == "raise" else None, **tokens
    )
    each, seconds, step = (lambda: m.setPolicyLimit(50, transition=False)), 3600, 30
    if scenario == "raise":
        FleetCar(requests_mock, limit=50, api=base)
        v.observe("chargeLimit", 50, "OwnerAPI", at=c() - 600)
        m.setPolicyLimit(80, transition=True)
        each, step = None, 10
    elif scenario == "stop":
        FleetCar(requests_mock, state="online", charging="Charging", api=base)
        v.observe("chargingState", "Charging", "TeslaMateVehicle")
        m.stopCarsCharging(VIN)
        each, seconds, step = (lambda: m.stopCarsCharging(VIN)), 600, 10
    else:
        # "retry": Fleet answers 500; "asleep": the car never wakes (408)
        script = [(500, None, None)] if scenario == "retry" else None
        state = "online" if scenario == "retry" else "asleep"
        FleetCar(
            requests_mock, state=state, wakes=False, limit=90, script=script, api=base
        )
        v.observe("chargeLimit", 90, "TeslaMateVehicle")
        m.setPolicyLimit(50, transition=True)
    records = []
    for _ in range(int(seconds / step)):
        c.t += step
        if each:
            each()
        r.pass_()
        records.append(paid_records(m))
    if "owner" in tokens:
        # Nothing billed, nothing else asked
        assert paid(requests_mock) == (0, 0, 0)
        assert counts(requests_mock)["other"] == 0
    return commands(requests_mock), path_shape(requests_mock), records


@pytest.mark.parametrize("scenario", ["raise", "retry", "asleep", "stop"])
def test_owner_only_off_car_matches_fleet_only(requests_mock, scenario):
    fleet = off_car_run(requests_mock, scenario, FLEET, fleet=True)
    owner = off_car_run(requests_mock, scenario, OWNER, owner=True)
    # The lower wakes a car that will charge: a car that stays asleep gets no
    # command; the 500 is retried once
    limit = ["set_charge_limit"] * {"raise": 1, "asleep": 0}.get(scenario, 2)
    assert fleet[0] == (["charge_stop"] if scenario == "stop" else limit)
    # Same requests, and the same paid records after every pass
    if scenario == "asleep":  # the wake is held: no paid send
        assert fleet[2][-1][0][4].detail == "wake held"
    else:
        assert any(rec and rec[0] for rec in fleet[2][-1])
    assert owner == fleet


# Row 3c (Phase 3.5 Task 6): both tokens and a car listed command_signing
# "off". The provider order decides, and OwnerAPI ranks before FleetAPI: a
# policy raise on an asleep car and a start go to the free Owner API only


def test_both_tokens_off_car_commands_free(requests_mock):
    m, r, v, c, api = install(owner=True, fleet=True, signing="off", held=(90, 50))
    car = FleetCar(requests_mock, limit=50, api=OWNER)
    for field, value in (
        ("chargeLimit", 50),
        ("chargingState", "Stopped"),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "OwnerAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    m.startCarsCharging(VIN)
    passes(r, c, 3600, each=lambda: m.startCarsCharging(VIN))
    assert sorted(commands(requests_mock)) == ["charge_start", "set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 80
    assert car.charge["charging_state"] == "Charging"
    assert all("fleet-api" not in q.url for q in requests_mock.request_history)
    assert paid(requests_mock) == (0, 0, 0) and dollars(requests_mock) == 0
    assert counts(requests_mock)["other"] == 0


@pytest.mark.parametrize("scenario", ["raise", "retry", "asleep", "stop"])
def test_both_tokens_off_car_matches_owner_only(requests_mock, scenario):
    # Row 3b's scenarios with a Fleet token added: the same Owner requests
    # and paid records, and no Fleet request, even when the car never answers
    owner = off_car_run(requests_mock, scenario, OWNER, owner=True)
    both = off_car_run(requests_mock, scenario, OWNER, owner=True, fleet=True)
    assert both == owner


# Row 3: Owner token only (read-only); a policy limit and a stop


def test_owner_only_never_commands(requests_mock):
    m, r, v, c, api = install(owner=True)
    FleetCar(requests_mock, state="online", charging="Charging")
    v.observe("chargingState", "Charging", "TeslaMateVehicle")
    m.setPolicyLimit(70, transition=True)
    m.stopCarsCharging(VIN)
    passes(r, c, 600)
    assert m.desired.get(VIN, CHARGE_LIMIT) is None and paid(requests_mock) == (0, 0, 0)
    assert requests_mock.call_count == 0


# Rows 5 and 6: BLE + Fleet, the BLE radio down (the car is awake)


def test_ble_plus_fleet_waits_five_minutes(requests_mock):
    m, r, v, c, api = install(fleet=True)
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="online", limit=90)
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, PAID_AFTER[CHARGE_LIMIT] - 10)
    assert (
        paid(requests_mock) == (0, 0, 0)
        and bleCommands(b) == ["wake"] * 2 + ["charging-set-limit"] * 2
    )  # the wake and its quick retry, then the send and its retry
    passes(r, c, 3600)
    assert paid(requests_mock) == (0, 0, 1) and commands(requests_mock) == [
        "set_charge_limit"
    ]
    assert len(b.runs) == 4 and dollars(requests_mock) == 0.001


def test_ble_plus_fleet_stop_waits_a_minute(requests_mock):
    m, r, v, c, api = install(fleet=True)
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="online", charging="Charging")
    v.observe("chargingState", "Charging", "TeslaMateVehicle")
    m.stopCarsCharging(VIN)
    passes(r, c, PAID_AFTER[CHARGING] - 10)
    assert paid(requests_mock) == (0, 0, 0) and bleCommands(b) == ["charging-stop"] * 2
    passes(r, c, 3600)
    assert paid(requests_mock) == (0, 0, 1) and commands(requests_mock) == [
        "charge_stop"
    ]
    assert counts(requests_mock)["free"] == 1 and len(b.runs) == 2


# Row 7: TeslaMate + BLE; the user sets 80 in the app while we hold 50


def test_telemetry_limit_change_is_adopted(requests_mock):
    m, r, v, c, api = install(held=(90, 50))
    b = realBle(m)
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, 60)
    assert [args for vin, cmd, args in b.runs] == [None, 50]
    v.observe("chargeLimit", 80, "TeslaMateVehicle")  # the app
    passes(
        r, c, 3600, step=30, each=lambda: m.setPolicyLimit(50, transition=False)
    )  # the policy re-enforces every pass
    assert len(b.runs) == 2 and m.desired.get(VIN, CHARGE_LIMIT).value == 80
    # adopted: nothing held, so no entry
    assert m.heldLimits.get(VIN) is None and requests_mock.call_count == 0


# Row 8: BLE + Fleet; the app starts a car the heartbeat stopped


def test_app_start_is_overridden_over_ble(requests_mock):
    m, r, v, c, api = install(fleet=True)
    b = realBle(m)
    FleetCar(requests_mock, state="online", charging="Charging")
    v.observe("chargingState", "Charging", "TeslaMateVehicle")
    m.stopCarsCharging(VIN)
    passes(r, c, 120, each=lambda: m.stopCarsCharging(VIN))
    assert bleCommands(b) == ["charging-stop"]
    v.observe("chargingState", "Charging", "TeslaMateVehicle")  # the app starts it
    passes(r, c, 600, each=lambda: m.stopCarsCharging(VIN))
    assert (
        bleCommands(b) == ["charging-stop"] * 2 and requests_mock.call_count == 0
    )  # once per newer reading


# Row 9: Fleet-only; Fleet answers 500 to a limit


def test_fleet_failure_is_retried_once(requests_mock):
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", limit=90, script=[(500, None, None)])
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, 3600, step=30, each=lambda: m.setPolicyLimit(50, transition=False))
    assert commands(requests_mock) == ["set_charge_limit"] * 2 and paid(
        requests_mock
    ) == (0, 0, 2)


# Row 10: Fleet + Home Assistant; Fleet fails a limit


def test_fleet_then_ha_never_in_one_desire(requests_mock):
    m, r, v, c, api = install(fleet=True)
    realHa(
        m,
        requests_mock,
        (VIN, {"charge_limit_number": "number.limit", "charge_switch": "switch.c"}),
    )
    FleetCar(requests_mock, state="online", limit=90, script=[(500, None, None)])
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, 3600, step=30, each=lambda: m.setPolicyLimit(50, transition=False))
    assert paid(requests_mock) == (0, 0, 2) and counts(requests_mock)["ha"] == 0


# Gen3 rows (11, 29, 30): two units on one circuit, the cars named on them


def gen3(m, *cars):
    # Two real Gen3 units on m's registry; car i is correlated to unit i
    from tests.unit.test_gen3twc_provider import IP1, IP2, make as make_gen3, vitals

    g, ctl, gp = make_gen3((IP1, IP2))
    g.vehicleRegistry, g.getSlaveTWCs = m.vehicleRegistry, m.getSlaveTWCs
    m.providerOrder.register(gp)
    units = ctl.devices()
    for unit, vin in zip(units, cars):
        m.vehicleRegistry.getOrCreate(vin, "t").observe(
            "pluggedIn", True, "TeslaMateVehicle"
        )
        vitals(unit, True)
        assert unit.vehicleKey == vin
    return ctl, gp, units


def test_gen3_stop_routes(requests_mock):  # row 11
    m, r, v, c, api = install(located=False)
    b = realBle(m, vins=(VIN, VIN2))
    ctl, gp, (a, u2) = gen3(m, VIN, VIN2)
    m.vehicleRegistry.get(VIN2).observe("chargingState", "Charging", "TeslaMateVehicle")
    a.setTargetPower(4800)
    u2.setTargetPower(0)
    writes = ctl.setHouseWatts.call_count
    m.stopCarsCharging(VIN2)
    passes(r, c, 600, each=lambda: m.stopCarsCharging(VIN2))
    assert gp.limitations(m.vehicleRegistry.get(VIN2)) == frozenset(
        {C.NO_INDIVIDUAL_STOP}
    )
    assert bleCommands(b, VIN2) == ["charging-stop"] and bleCommands(b, VIN) == []
    assert (
        ctl.setHouseWatts.call_count == writes and requests_mock.call_count == 0
    )  # a's headroom is left alone


def test_gen3_shared_headroom_meets_the_budget(
    requests_mock,
):  # row 29, the wiring rule
    from tests.unit.test_gen3twc_provider import headroom

    m, r, v, c, api = install(located=False)
    ctl, gp, (a, u2) = gen3(m, VIN, VIN2)
    for first, second in ((a, u2), (u2, a)):  # either write order
        first.setTargetPower(4800 if first is a else 2400)
        second.setTargetPower(4800 if second is a else 2400)
        assert headroom(ctl) == 15  # 20 A + 10 A over two units: each may draw 15 A
        assert (
            2 * headroom(ctl) * 240 == 4800 + 2400
        )  # the circuit draws exactly its budget, in either order


def test_gen3_cloud_vin_is_free(requests_mock):  # row 30
    from tests.unit.test_wall_connector_vins import VIN as WC_VIN, setup as wcSetup
    from tests.unit.test_gen3twc_provider import vitals

    api, reg, ctl, u = wcSetup(requests_mock)
    reg.getOrCreate(WC_VIN, "TeslaMateVehicle")
    vitals(u, True)
    assert u.vehicleKey == WC_VIN
    assert (
        paid(requests_mock) == (0, 0, 0) and counts(requests_mock)["free"] == 2
    )  # products, live_status


# Row 12: chargeStopMode 2 with BLE and Fleet; the heartbeat stops a car


def onTwc(m, v):
    v.observe("twcVin", "8244", "TWC")
    s = m.getSlaveTWCs()[0]
    s.currentVIN, s.protocolVersion = v.vin, 2
    return s


def test_stop_mode_2_ble_then_disruptive(requests_mock):
    for keyed in (True, False):
        requests_mock.reset_mock()
        m, r, v, c, api = install(fleet=True)
        b = realBle(m, answers=None) if keyed else None
        FleetCar(requests_mock, state="online", charging="Charging")
        onTwc(m, v)
        m.settings["chargeStopMode"] = "2"
        v.observe("chargingState", "Charging", "TeslaMateVehicle")
        m.stopCarsCharging(VIN)
        r.pass_()
        if keyed:
            assert (
                bleCommands(b) == ["charging-stop"] * 2
                and m.settings.get("respondToSlaves", 1) == 1
            )
            passes(r, c, PAID_AFTER[CHARGING])
        assert (
            m.settings["respondToSlaves"] == 0
        )  # the shun: at once for the keyless car, at 60 s for the keyed one
        passes(r, c, 600)
        assert paid(requests_mock) == (0, 0, 0)  # Fleet never


# Rows 13 and 14: chargeRateControl 3 and 1, Fleet-only car on a TWC


def fleetPoint(requests_mock, mode, **car):
    from tests.unit.test_amps_reconciler import point

    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", charging="Charging", **car)
    pt, s = point(m, v, mode)
    v.observe("chargingState", "Charging", "FleetAPI")  # plugged in here
    return m, r, v, c, pt, s


def amps(requests_mock):
    return [
        q.json()["charging_amps"]
        for q in requests_mock.request_history
        if "set_charging_amps" in q.url
    ]


def test_mode_3_fleet_only_below_floor(requests_mock):
    m, r, v, c, pt, s = fleetPoint(requests_mock, 3)
    v.observe("chargeCurrentRequest", 32, "TeslaMateVehicle")
    pt.setTargetPower(4 * 240)
    passes(r, c, 60)
    assert (
        amps(requests_mock) == [4, 4] and s.pointAmps == 6
    )  # the low-amps repeat; the TWC holds its floor
    pt.setTargetPower(10 * 240)
    passes(r, c, 600, each=lambda: pt.setTargetPower(10 * 240))
    assert (
        amps(requests_mock) == [4, 4, 32] and s.pointAmps == 10
    )  # back on the TWC; the car (at 4) is lifted once
    assert paid(requests_mock) == (0, 0, 3)


def test_mode_1_lift_once_per_session(requests_mock):
    m, r, v, c, pt, s = fleetPoint(requests_mock, 1)
    pt.setTargetPower(2400)
    passes(r, c, 3600, each=lambda: pt.setTargetPower(2400))
    assert amps(requests_mock) == [32] and paid(requests_mock) == (0, 0, 1)
    requests_mock.reset_mock()
    m, r, v, c, pt, s = fleetPoint(requests_mock, 1)
    v.observe(
        "chargeCurrentRequest", 32, "TeslaMateVehicle"
    )  # the car already asks for the max
    pt.setTargetPower(2400)
    passes(r, c, 600, each=lambda: pt.setTargetPower(2400))
    assert amps(requests_mock) == [] and paid(requests_mock) == (0, 0, 0)


# Row 15: Fleet token rejected (401) on a limit


def test_fleet_401_retires(requests_mock):
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", limit=90, script=[(401, None, None)])
    requests_mock.post(re.compile(r".*/oauth2/v3/token"), status_code=401, json={})
    v.observe("chargeLimit", 90, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)
    passes(r, c, 3600, step=30, each=lambda: m.setPolicyLimit(50, transition=False))
    assert commands(requests_mock) == ["set_charge_limit"] and api.fleetSlot.empty()
    assert m.desired.record(VIN, CHARGE_LIMIT).excluded == {"FleetAPI": O.REFUSED}


# Row 16: Home Assistant only, two cars; the heartbeat stops one


def test_ha_only_targeted_stop(requests_mock):
    m, r, v, c, api = install()
    realHa(
        m,
        requests_mock,
        (VIN, {"charge_switch": "switch.one"}),
        (VIN2, {"charge_switch": "switch.two"}),
    )
    for vin in (VIN, VIN2):
        m.vehicleRegistry.get(vin).observe("chargingState", "Charging", "HomeAssistant")
    m.stopCarsCharging(VIN)
    passes(r, c, 600, each=lambda: m.stopCarsCharging(VIN))
    posts = [
        q.json()
        for q in requests_mock.request_history
        if "/api/services/switch/turn_off" in q.url
    ]
    assert posts == [{"entity_id": "switch.one"}] and counts(requests_mock)["ha"] == 1


# Rows 17a and 17b: a held limit, the car gone


def test_vanished_car_restore_is_hourly_and_free(requests_mock):
    # The live box's shape: ble_only, the Tesla API for free reads only
    m, r, v, c, api = install(owner=True, fleet=True, policy="ble_only", held=(90, 50))
    b = realBle(m, answers=None)
    v.observe("chargeLimit", 50, "TeslaMateVehicle")
    c.t += 1
    placeAt(m, v, AWAY)
    m.setPolicyLimit(50, transition=False)  # any pass sees the departure
    assert m.desired.get(VIN, CHARGE_LIMIT).restore
    passes(r, c, 3 * RESTORE_RETRY + 120, step=60)
    assert (
        bleCommands(b) == ["charging-set-limit"] * 8
    )  # t0, +1 h, +2 h, +3 h; each with its quick retry
    assert requests_mock.call_count == 0


def test_vanished_car_restore_with_fleet_is_bounded(requests_mock):
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="asleep", limit=50, at=AWAY)
    v.observe("chargeLimit", 50, "TeslaMateVehicle")
    c.t += 1
    placeAt(m, v, AWAY)
    m.setPolicyLimit(50, transition=False)
    passes(r, c, 3 * 3600, step=60)
    # The free online check finds it asleep and holds the restore
    assert commands(requests_mock) == [] and paid(requests_mock) == (0, 0, 0)


# Row 18 is test_reconciler_queues.py::test_drain_runs_pending_stops_only (no HTTP)


# Row 19: Fleet-only; the car's own schedule is pending; the heartbeat starts


def test_scheduled_charge_skips_the_start(requests_mock):
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="asleep")
    for field, value in (
        ("chargingState", "Stopped"),
        ("scheduledChargingPending", True),
        ("chargeLimit", 80),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "TeslaMateVehicle")
    m.startCarsCharging(VIN)
    passes(r, c, 3600, each=lambda: m.startCarsCharging(VIN))
    assert requests_mock.call_count == 0


# Row 20 and Task 10 I2: Fleet Telemetry streams while Fleet answers 408


def test_streaming_telemetry_with_fleet_408_is_bounded(requests_mock):
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="online", limit=90)
    car._command = lambda req, ctx: (
        setattr(ctx, "status_code", 408),
        {"response": None},
    )[1]
    requests_mock.post(
        re.compile(re.escape(FLEET + VIN) + r"/command/.*"), json=car._command
    )
    v.observe("chargeLimit", 90, "FleetTelemetryMQTT")
    m.setPolicyLimit(50, transition=True)
    passes(
        r,
        c,
        3600,
        step=30,
        each=lambda: (
            v.observe("chargeLimit", 90, "FleetTelemetryMQTT"),
            m.setPolicyLimit(50, transition=False),
        ),
    )
    assert paid(requests_mock) == (
        0,
        0,
        3,
    )  # within the cap of 4 sends and 1 wake per desire


def test_telemetry_rearm_on_a_start_is_once_per_desire(requests_mock):  # Task 10 I2
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="online")
    car._command = lambda req, ctx: (
        setattr(ctx, "status_code", 408),
        {"response": None},
    )[1]
    requests_mock.post(
        re.compile(re.escape(FLEET + VIN) + r"/command/.*"), json=car._command
    )
    for field, value in (
        ("chargingState", "Stopped"),
        ("chargeLimit", 80),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "FleetTelemetryMQTT")
    m.startCarsCharging(VIN)
    passes(
        r,
        c,
        3600,
        step=30,
        each=lambda: (
            v.observe("chargingState", "Stopped", "FleetTelemetryMQTT"),
            m.startCarsCharging(VIN),
        ),
    )
    assert (
        paid(requests_mock) == (0, 0, 3)
        and commands(requests_mock) == ["charge_start"] * 3
    )  # within the cap of 4


def staleStopped(requests_mock, **car):
    # Fleet-only, no feed: the car's last chargingState is a 10-minute-old
    # "Stopped"
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", **car)
    for field, value in (
        ("chargingState", "Stopped"),
        ("chargeLimit", 80),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "FleetAPI", at=c() - 600)
    return m, r, v, c


@pytest.mark.parametrize("drawing", ["live", "stale", None])
def test_fleet_only_start_no_retry_while_the_twc_draws(requests_mock, drawing):
    # The start is not accepted (the car started itself when its limit
    # rose), and the car's TWC then measures 48 A: that is the car charging,
    # so no retry goes out. A TWC reading that stops coming goes stale and
    # the retry goes, as it does with no current at all
    m, r, v, c = staleStopped(requests_mock, script=[(200, False, "")])
    seen = []

    def each():
        m.startCarsCharging(VIN)
        if drawing and commands(requests_mock) and not (drawing == "stale" and seen):
            seen.append(v.observe("actualCurrent", 48, "Gen2TWC", seen=False))

    m.startCarsCharging(VIN)
    passes(r, c, 3600, step=5, each=each)
    starts = commands(requests_mock).count("charge_start")
    want = 1 if drawing == "live" else 2
    assert starts == want and paid(requests_mock) == (1, 0, starts)


@pytest.mark.parametrize("drawing", [True, False])
def test_fleet_only_stop_is_the_same_while_the_twc_draws(requests_mock, drawing):
    # A stop plans on the car's own reading: current at its TWC changes nothing
    m, r, v, c = staleStopped(requests_mock, charging="Charging")

    def each():
        m.stopCarsCharging(VIN)
        if drawing:
            v.observe("actualCurrent", 48, "Gen2TWC", seen=False)

    passes(r, c, 600, step=5, each=each)
    # The stale "Stopped" settles it either way
    assert commands(requests_mock) == [] and paid(requests_mock) == (0, 0, 0)


# Row 21 (slim Task 12b M3): chargeRateControl 2, a rate Fleet cannot deliver


def test_mode_2_undeliverable_rate_pays_twice(requests_mock):
    m, r, v, c, pt, s = fleetPoint(requests_mock, 2, script=[(500, None, None)])
    pt.setTargetPower(10 * 240)
    passes(r, c, 3600, each=lambda: pt.setTargetPower(10 * 240))
    assert (
        amps(requests_mock) == [10, 10] and s.pointAmps == 10
    )  # one send, one blind retry; the TWC keeps the target (final review I1)


# Final review I1: in mode 2 the TWC offers its maximum only once the car's
# rate is confirmed; a rate that failed twice leaves the TWC at the target


def test_mode_2_twc_holds_the_target_until_the_car_rate_lands(requests_mock):
    for script, confirmed in (
        ([(500, None, None), (500, None, None), (200, True, "")], False),
        ([(200, True, "")], True),
    ):
        requests_mock.reset_mock()
        m, r, v, c, pt, s = fleetPoint(requests_mock, 2, script=script)
        offers = []

        def cycle():
            pt.setTargetPower(10 * 240)
            offersThenBeat(m, s, 0x04, 0.0, 10.0)  # the target set by hand
            offers.append(s.lastAmpsOffered)

        passes(r, c, 3600, each=cycle)
        if confirmed:
            assert amps(requests_mock) == [10] and offers[-1] == s.wiringMaxAmps
        else:
            assert (
                amps(requests_mock) == [10, 10] and max(offers[1:]) <= 10
            )  # the paid bound holds; the TWC carries the rate (the wiring rule)


# Row 23 (slim Task 10 M2): a spent re-arm lets the start go without the limit


def test_a_lower_that_wakes_a_refusing_car_rides_the_start_once_it_wakes(requests_mock):
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="asleep", limit=90, wakes=False)
    for field, value in (
        ("chargeLimit", 90),
        ("chargingState", "Stopped"),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)  # a lower on a car that will charge: it wakes
    passes(r, c, 600)
    c.t += 1
    v.observe(
        "chargeLimit", 90, "FleetTelemetryMQTT"
    )  # a reading that reached the car: the one re-arm
    passes(r, c, 1200)
    # The lower wakes the car (it charges once limited): the wake fails, and
    # Fleet's wake hold keeps every later send from the car
    assert commands(requests_mock) == [] and paid(requests_mock) == (0, 1, 0)
    assert m.desired.record(VIN, CHARGE_LIMIT).rearmed
    car.wakes = True
    m.startCarsCharging(VIN)
    passes(r, c, 600, each=lambda: m.startCarsCharging(VIN))
    assert commands(requests_mock) == ["set_charge_limit", "charge_start"]
    # The woken car takes the limit, then the start
    assert paid(requests_mock) == (1, 2, 2) and car.charge["charge_limit_soc"] == 50


# Row 24 (slim Task 12a I1): an anonymous Gen2 point in mode 1 stops


def test_anonymous_stop_fans_out_as_main(requests_mock):
    m, r, v, c, api = install(fleet=True)
    api.addVehicle({"id": 2, "vin": VIN2, "display_name": "Two"})
    for vin in (VIN, VIN2):
        FleetCar(requests_mock, vin=vin, state="online", charging="Charging")
        car = m.vehicleRegistry.get(vin)
        placeAt(m, car, HOME)
        car.observe("chargingState", "Charging", "TeslaMateVehicle")
    anon = m.vehicleRegistry.beginPlugSession("8244")
    m.stopCarsCharging(anon.key)
    passes(r, c, 600, each=lambda: m.stopCarsCharging(anon.key))
    assert sorted(
        q.url.split("/")[-3]
        for q in requests_mock.request_history
        if "/command/" in q.url
    ) == [VIN, VIN2]
    assert paid(requests_mock) == (
        0,
        0,
        2,
    )  # no read: the stop needs none on a car known charging


# Row 25 (slim Task 12b anonymous fan-out): a VIN-less Gen2 in mode 2 sets the rate


def test_vinless_rate_goes_to_the_plugged_in_car_only(requests_mock):
    m, r, v, c, api = install(fleet=True)
    api.addVehicle({"id": 2, "vin": VIN2, "display_name": "Two"})
    for vin, state in ((VIN, "Disconnected"), (VIN2, "Charging")):
        FleetCar(requests_mock, vin=vin, state="online", charging=state)
        car = m.vehicleRegistry.get(vin)
        placeAt(m, car, HOME)
        car.observe("chargingState", state, "TeslaMateVehicle")
    s = m.getSlaveTWCs()[0]
    s.protocolVersion = 2
    m.settings["chargeRateControl"] = "2"
    m.setMaxAmpsToDivideAmongSlaves(10)
    m.vehicleRegistry.beginPlugSession("8244")

    def beat():
        heartbeat(m, s, 10.0, state=0x01)

    passes(r, c, 600, each=beat)
    targets = [
        q.url.split("/")[-3]
        for q in requests_mock.request_history
        if "set_charging_amps" in q.url
    ]
    assert targets == [VIN2] and amps(requests_mock) == [
        10
    ]  # vehicles[0] (VIN) is unplugged: never paid for


# Row 26 (slim Task 12c, live 2026-10-16): a restart with the car plugged
# in and asleep, no TWC VIN; the policy changes to Scheduled 70


def test_restart_with_the_car_plugged_in_and_asleep_gets_the_scheduled_limit(
    requests_mock,
):
    m, r, v, c, api = install(owner=True, fleet=True, policy="ble_only", held=(100, 50))
    b = realBle(m)
    FleetCar(requests_mock, state="asleep", limit=50, soc=51, charging="Complete")
    for field, value in (
        ("chargeLimit", 50),
        ("chargingState", "Complete"),
        ("batteryLevel", 51),
    ):
        v.observe(field, value, "TeslaMateVehicle")
    s = m.getSlaveTWCs()[0]
    s.protocolVersion = 2
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.setPolicyLimit(50, transition=True)  # Non Scheduled, after the restart
    m.setPolicyLimit(70, transition=True)  # 00:00: Scheduled Charging
    passes(
        r, c, 600, each=lambda: heartbeat(m, s, 0.0, state=0x03, amps_max=32)
    )  # plugged in, asleep, no VIN; the TWC echoes its last offer
    # BASE's order again (Phase 4 Task 4): the tick's offer follows the
    # reply in the same beat, so the lift is desired with the first reply,
    # as at BASE, and goes before the start (held by the limit gap; the
    # reconciler plans a rate before a start, Task 4b)
    assert [(cmd, args) for vin, cmd, args in b.runs] == [
        ("wake", None),
        ("charging-set-limit", 70),
        ("wake", None),
        ("charging-set-amps", 32),
        ("wake", None),
        ("charging-start", None),
    ]
    assert m.heldLimits.get(VIN) == HeldLimit(
        100, 70
    )  # the limit first, then the BLE lift (main's rate broadcast), then
    # main's start: TeslaMate's "Complete" predates the raise
    assert (
        requests_mock.call_count == 0
    )  # nothing paid, nothing free: presence came from TeslaMate


# Row 27 (slim Task 12a ruling 2): chargeStopMode 2 adds no paid start


def test_mode_2_start_sends_no_paid_command(requests_mock):
    for keyed in (False, True):
        requests_mock.reset_mock()
        m, r, v, c, api = install(fleet=True)
        b = realBle(m) if keyed else None
        FleetCar(requests_mock, state="online", charging="Charging")
        onTwc(m, v)
        m.settings["chargeStopMode"] = "2"
        for field, value in (
            ("chargingState", "Charging"),
            ("chargeLimit", 80),
            ("batteryLevel", 40),
        ):
            v.observe(field, value, "TeslaMateVehicle")
        m.stopCarsCharging(VIN)  # keyless: the shun; keyed: BLE
        passes(r, c, 120)
        c.t += 1
        v.observe("chargingState", "Stopped", "TeslaMateVehicle")
        m.startCarsCharging(VIN)
        passes(r, c, 600, each=lambda: m.startCarsCharging(VIN))
        assert m.settings.get("respondToSlaves", 1) == 1 and paid(requests_mock) == (
            0,
            0,
            0,
        )  # Fleet never starts in mode 2
        if keyed:
            assert bleCommands(b) == [
                "charging-stop",
                "wake",
                "charging-start",
            ]  # a free BLE start is allowed (the ruling)


# Row 28 (slim Task 12a I3): a car below minChargeLevel is never stopped


def test_a_car_below_min_charge_level_is_not_stopped(requests_mock):
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", charging="Charging", soc=20)
    m.config["config"]["minChargeLevel"] = 30
    for field, value in (("chargingState", "Charging"), ("batteryLevel", 20)):
        v.observe(field, value, "TeslaMateVehicle")
    m.stopCarsCharging(VIN)
    passes(r, c, 600, each=lambda: m.stopCarsCharging(VIN))
    assert requests_mock.call_count == 0


# Rows 29-30 (Phase 4 Task 3, (h)): a vehicle-only install, no wall
# connector, is controlled by the tick's distributor; an hour of 10 s ticks
# at a 16 A budget, the car charging at home


def vehicleOnly(requests_mock, **kw):
    m, r, v, c, api = install(**kw)
    m.deleteSlaveTWC(m.getSlaveTWCs()[0].TWCID)
    v.observe("chargingState", "Charging", "TeslaMateVehicle")
    # Free: TeslaMate's position is home
    assert Presence.presence(m, v) == Presence.HOME
    m.setMaxAmpsToDivideAmongSlaves(16)
    return m, r, v, c


def test_fleet_only_vehicle_install_gets_its_rate(requests_mock):
    m, r, v, c = vehicleOnly(requests_mock, fleet=True)
    FleetCar(requests_mock, state="online", charging="Charging")
    passes(r, c, 3600, each=m.controlPass)
    assert amps(requests_mock) == [16]
    assert paid(requests_mock) == (0, 0, 1) and dollars(requests_mock) == 0.001


def test_ble_only_vehicle_install_gets_its_rate_with_nothing_paid(requests_mock):
    m, r, v, c = vehicleOnly(requests_mock, owner=True, policy="ble_only")
    b = realBle(m)
    passes(r, c, 3600, each=m.controlPass)
    assert ("charging-set-amps", 16) in [(cmd, a) for vin, cmd, a in b.runs]
    assert paid(requests_mock) == (0, 0, 0)


# Presence rows: one applicability rule per message. Each states its paid
# tuple (vehicle_data, wake_up, command)

TM = "TeslaMateVehicle"


def push(v, c, gap=1, **fields):
    # Pushed telemetry, each batch at its own time
    c.t += gap
    for field, value in fields.items():
        v.observe(field, value, TM)


def cycles(m, limit):
    # The policy thread's pass
    return lambda: m.setPolicyLimit(limit, transition=False)


def unplugAtHome(v, c):
    push(v, c, chargingState="Charging")
    push(v, c, chargingState="Disconnected")


def test_box_2026_10_18_arrival_while_driving_sends_nothing_until_parked_then_ble_free(
    requests_mock,
):
    # NOT_HOME; 15:18 TeslaMate fix inside + state driving -> PRESUMED/HOLD
    # (no ping, no command, no HTTP); 15:21 state online + shift P ->
    # HOME, A > L -> BLE charging-set-limit; paid == (0, 0, 0)
    m, r, v, c, api = install(at=AWAY)
    b = realBle(m)
    v.observe("chargeLimit", 50, TM)
    policy = cycles(m, 80)
    passes(r, c, 60, each=policy)
    assert m.desired.get(VIN, CHARGE_LIMIT) is None and b.pings == []
    push(v, c, state="driving")
    placeAt(m, v, HOME)
    passes(r, c, 170, each=policy)
    assert bleCommands(b) == [] == b.pings and Presence.drivingNow(v)
    push(v, c, state="online", shiftState="P")
    passes(r, c, 10, each=policy)  # the next pass, inside the 300 s retry gap
    assert bleCommands(b) == ["wake", "charging-set-limit"]
    assert requests_mock.call_count == 0 and paid(requests_mock) == (0, 0, 0)


def test_ble_contact_while_driving_still_holds(requests_mock):
    # Contact proves presence, not parked; paid == (0, 0, 0)
    m, r, v, c, api = install()
    b = realBle(m)
    v.observe("chargeLimit", 50, TM)
    push(v, c, state="driving")
    v.observe("bleReach", True, "TeslaBLE")
    policy = cycles(m, 80)
    passes(r, c, 600, each=lambda: (policy(), v.observe("state", "driving", TM)))
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert b.pings == [] and v.markAt("contact") > Presence.NEVER
    assert bleCommands(b) == [] and rec.heldAt is not None
    assert requests_mock.call_count == 0


def test_arrive_park_ble_sets_the_limit_before_the_plug(requests_mock):
    # Arriving newer than leaving, no plug: the policy limit over BLE;
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, at=AWAY)
    b = realBle(m)
    FleetCar(requests_mock, state="online", limit=50)
    v.observe("chargeLimit", 50, TM)
    c.t += 1
    placeAt(m, v, HOME)
    push(v, c, shiftState="D")
    push(v, c, 20, shiftState="P")
    passes(r, c, 30, each=cycles(m, 80))
    assert bleCommands(b) == ["wake", "charging-set-limit"]
    assert not Presence.pluggedNow(m, v) and paid(requests_mock) == (0, 0, 0)


def test_a_20_s_drive_inside_the_radius_between_policy_passes_is_seen(
    requests_mock,
):
    # HOME unplugged after an unplug at t0; pushed shift D at t+5, P at t+25
    # (inside); policy pass at t+30: driveEnd t+25 > leaving -> apply over
    # BLE; paid == (0, 0, 0)
    m, r, v, c, api = install()
    b = realBle(m)
    v.observe("chargeLimit", 90, TM)
    unplugAtHome(v, c)
    t0 = c.t
    c.t += 1
    v.observe("bleReach", True, "TeslaBLE")
    assert Presence.presence(m, v) == Presence.HOME
    push(v, c, t0 + 5 - c.t, shiftState="D")
    push(v, c, 20, shiftState="P")
    c.t = t0 + 30
    m.setPolicyLimit(50, transition=False)
    r.pass_()
    assert Presence.arriving(v) == t0 + 25 > Presence.leaving(v) == t0 + 5
    assert bleCommands(b) == ["charging-set-limit"] and b.runs[-1][2] == 50
    assert requests_mock.call_count == 0


def test_arrive_limit_set_then_shift_to_d_in_the_driveway_restores_over_ble(
    requests_mock,
):
    # The limit set at arrival; shift D in the driveway is the departure:
    # the restore goes over BLE at once, inside the radius. No Fleet
    # request; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, at=AWAY)
    b = realBle(m)
    FleetCar(requests_mock, state="online", limit=90)
    v.observe("chargeLimit", 90, TM)
    c.t += 1
    placeAt(m, v, HOME)
    push(v, c, shiftState="D")
    push(v, c, 20, shiftState="P")
    policy = cycles(m, 50)
    passes(r, c, 60, each=policy)
    assert bleCommands(b) == ["wake", "charging-set-limit"]
    assert m.heldLimits.get(VIN) == HeldLimit(90, 50)
    push(v, c, shiftState="D")
    r.pass_()
    assert [args for vin, cmd, args in b.runs] == [None, 50, 90]  # no wake to restore
    assert m.heldLimits.get(VIN) is None and Presence.inside(m, v)
    c.t += 1
    placeAt(m, v, AWAY)
    passes(r, c, 60, each=policy)
    assert [args for vin, cmd, args in b.runs] == [None, 50, 90]
    assert requests_mock.call_count == 0


def test_a_car_that_never_left_unplugged_for_hours_is_not_re_applied(
    requests_mock,
):
    # The unplug restores it once; hours of BLE contact and policy passes
    # apply nothing; paid == (0, 0, 0)
    m, r, v, c, api = install(held=(90, 50))
    b = realBle(m)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    policy, beats = cycles(m, 50), []

    def each():
        policy()
        beats.append(c.t)
        if len(beats) % 10 == 0:
            v.observe("bleReach", True, "TeslaBLE")  # still in range

    passes(r, c, 4 * 3600, step=60, each=each)
    assert Presence.presence(m, v) == Presence.HOME
    assert [args for vin, cmd, args in b.runs] == [90]
    assert m.heldLimits.get(VIN) is None and requests_mock.call_count == 0


def test_leave_without_plugging_is_restored_by_fleet_once_the_fix_is_outside(
    requests_mock,
):
    # 1 command, no wake: paid == (0, 0, 1)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    car = FleetCar(requests_mock, state="online", limit=50, at=AWAY)
    v.observe("chargeLimit", 50, TM)
    policy = cycles(m, 50)
    passes(r, c, 600, each=policy)
    assert paid(requests_mock) == (0, 0, 0)  # parked, never plugged: no change
    push(v, c, chargeLimit=50)
    placeAt(m, v, AWAY)
    passes(r, c, 3600, each=policy)
    assert commands(requests_mock) == ["set_charge_limit"]
    assert paid(requests_mock) == (0, 0, 1) and car.charge["charge_limit_soc"] == 90


def presumedFleet(requests_mock, at):
    # Fleet-only, home set, no position known: presumed
    m, r, v, c, api = install(fleet=True, located=False)
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    car = FleetCar(requests_mock, limit=50, at=at)
    v.observe("chargeLimit", 50, "OwnerAPI", at=c.t - 3600)
    assert Presence.presence(m, v) == Presence.PRESUMED
    return m, r, v, c, car


def test_presumed_fleet_only_car_gets_the_limit_through_the_paid_sequence(
    requests_mock,
):
    # Woken, read (the read places it home), sent: paid == (1, 1, 1)
    m, r, v, c, car = presumedFleet(requests_mock, HOME)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 3600)
    assert paid(requests_mock) == (1, 1, 1) and car.charge["charge_limit_soc"] == 80


def test_presumed_fleet_only_car_read_outside_is_sent_nothing(requests_mock):
    # Woken and read; the read places it away: paid == (1, 1, 0)
    m, r, v, c, car = presumedFleet(requests_mock, AWAY)
    policy = cycles(m, 80)
    m.setPolicyLimit(80, transition=True)
    passes(r, c, 3600, each=policy)
    assert paid(requests_mock) == (1, 1, 0) and commands(requests_mock) == []
    assert Presence.presence(m, v) == Presence.NOT_HOME
    assert m.desired.get(VIN, CHARGE_LIMIT) is None


def test_unplug_restores_over_ble_at_once(requests_mock):
    # Scenario 7; paid == (0, 0, 0)
    m, r, v, c, api = install(held=(90, 50))
    b = realBle(m)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    m.setPolicyLimit(50, transition=False)
    r.pass_()
    assert [args for vin, cmd, args in b.runs] == [90] and b.pings == []
    assert m.heldLimits.get(VIN) is None and requests_mock.call_count == 0


def test_ble_gone_five_minutes_then_one_remote_restore(requests_mock):
    # A restore never wakes: paid == (0, 0, 1)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="online", limit=50)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    policy = cycles(m, 50)
    passes(r, c, PAID_AFTER[CHARGE_LIMIT] - 10, each=policy)
    assert paid(requests_mock) == (0, 0, 0)
    assert bleCommands(b) == ["charging-set-limit"] * 2
    passes(r, c, 3600, each=policy)
    assert commands(requests_mock) == ["set_charge_limit"]
    assert paid(requests_mock) == (0, 0, 1)


def test_ble_only_departure_never_pays(requests_mock):
    # ble_only: the restore retries hourly over BLE; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, policy="ble_only", held=(90, 50))
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="online", limit=50)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    passes(r, c, 2 * RESTORE_RETRY + 120, step=60, each=cycles(m, 50))
    assert bleCommands(b) == ["charging-set-limit"] * 6
    assert requests_mock.call_count == 0


def test_replug_before_the_remote_restore_sends_none(requests_mock):
    # Scenario 8: the plug-in is an arrival, at the limit already held;
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="online", limit=50)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    policy = cycles(m, 50)
    passes(r, c, 120, each=policy)
    push(v, c, chargingState="Charging")
    passes(r, c, 3600, each=policy)
    assert paid(requests_mock) == (0, 0, 0)
    assert m.heldLimits.get(VIN) == HeldLimit(90, 50)
    assert bleCommands(b) == ["charging-set-limit"] * 2  # the one failed restore


def test_fleet_only_unplug_one_remote_restore_plug_one_remote_apply(
    requests_mock,
):
    # Scenario 17: the apply reads the limit first, its last write being
    # stale by then: paid == (1, 0, 2)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    car = FleetCar(requests_mock, state="online", limit=50)
    v.observe("chargeLimit", 50, TM)
    policy = cycles(m, 50)
    unplugAtHome(v, c)
    passes(r, c, 600, each=policy)
    assert commands(requests_mock) == ["set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 90
    push(v, c, chargingState="Charging")
    passes(r, c, 600, each=policy)
    assert commands(requests_mock) == ["set_charge_limit"] * 2
    assert car.charge["charge_limit_soc"] == 50 and paid(requests_mock) == (1, 0, 2)


def test_fleet_only_restore_to_a_car_found_asleep_is_held(requests_mock):
    # Scenario 18: asleep, never woken for a restore; its free online check
    # holds it: paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    passes(r, c, 3 * RESTORE_RETRY, step=60, each=cycles(m, 50))
    assert commands(requests_mock) == []
    assert paid(requests_mock) == (0, 0, 0)


def test_2026_10_11_unplug_ble_silence_inside_radius_policy_cycles_send_nothing(
    requests_mock,
):
    # Scenario 1: the unplug departs it; policy cycles never re-apply, and
    # the restore is the hourly BLE retry only; paid == (0, 0, 0)
    m, r, v, c, api = install(owner=True, fleet=True, policy="ble_only", held=(90, 50))
    b = realBle(m, answers=None)
    v.observe("chargeLimit", 50, TM)
    unplugAtHome(v, c)
    passes(r, c, RESTORE_RETRY + 120, step=60, each=cycles(m, 50))
    assert [args for vin, cmd, args in b.runs] == [90] * 4
    assert requests_mock.call_count == 0


def test_departing_car_is_never_presumed_present(requests_mock):
    # Scenario 19: unplugged and driving off, the policy limit is never sent;
    # the restore goes at the drive start; paid == (0, 0, 0)
    m, r, v, c, api = install(held=(90, 50))
    b = realBle(m)
    v.observe("chargeLimit", 50, TM)
    policy = cycles(m, 80)
    unplugAtHome(v, c)
    push(v, c, 5, shiftState="D")
    r.pass_()
    assert [args for vin, cmd, args in b.runs] == [90]
    passes(r, c, 120, each=policy)
    assert Presence.presence(m, v) == Presence.PRESUMED
    c.t += 1
    placeAt(m, v, AWAY)
    passes(r, c, 60, each=policy)
    assert [args for vin, cmd, args in b.runs] == [90]
    assert requests_mock.call_count == 0


def startup(requests_mock, charging):
    # After a restart: Fleet-only, asleep, home set, no position; held
    m, r, v, c, api = install(fleet=True, located=False, held=(90, 50))
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    car = FleetCar(requests_mock, limit=50, charging=charging)
    v.observe("chargeLimit", 50, TM)
    passes(r, c, 3600, each=cycles(m, 50))
    assert Presence.presence(m, v) == Presence.PRESUMED
    assert paid(requests_mock) == (0, 0, 0)  # nothing wakes it to learn presence
    m.startCarsCharging(VIN)
    passes(r, c, 600)
    return m, v, car


def test_startup_asleep_car_is_presumed_and_not_woken_until_charging_needs_it(
    requests_mock,
):
    # Scenario 9: the start wakes it; paid == (1, 1, 1)
    m, v, car = startup(requests_mock, "Stopped")
    assert commands(requests_mock) == ["charge_start"]
    assert paid(requests_mock) == (1, 1, 1)


def test_startup_wake_finds_the_car_unplugged_and_sends_nothing(requests_mock):
    # Scenario 10: paid == (1, 1, 0)
    m, v, car = startup(requests_mock, "Disconnected")
    assert commands(requests_mock) == [] and paid(requests_mock) == (1, 1, 0)
    assert m.desired.get(VIN, CHARGING) is None


def test_startup_wake_finds_the_car_plugged_and_starts_charging(requests_mock):
    # Scenario 11: paid == (1, 1, 1)
    m, v, car = startup(requests_mock, "Stopped")
    assert car.charge["charging_state"] == "Charging"
    assert Presence.pluggedHere(m, v) and paid(requests_mock) == (1, 1, 1)


def test_ble_silent_umc_car_with_telemetry_inside_radius_is_home(requests_mock):
    # Scenario 13: no TWC; TeslaMate says charging inside; BLE silent: the
    # lower goes by Fleet after the free wait; paid == (0, 0, 1)
    m, r, v, c, api = install(fleet=True)
    b = realBle(m, answers=None)
    FleetCar(requests_mock, state="online", limit=90)
    push(v, c, chargeLimit=90, chargingState="Charging")
    assert Presence.pluggedHere(m, v)
    passes(r, c, 3600, each=cycles(m, 50))
    assert b.pings == [] and commands(requests_mock) == ["set_charge_limit"]
    assert paid(requests_mock) == (0, 0, 1)


def test_ble_plus_fleet_car_leaving_a_umc_is_not_watched(requests_mock):
    # No telemetry and no TWC: nothing watches a car charging on a UMC for
    # its departure, so nothing reads it, and it keeps the held limit;
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m, answers=None)
    car = FleetCar(requests_mock, state="online", limit=50, at=AWAY)
    v.observe("chargeLimit", 50, "FleetAPI", at=c.t - 600)
    v.observe("chargingState", "Charging", "FleetAPI", at=c.t - 600)
    car.charge["charging_state"] = "Disconnected"
    passes(r, c, 3600, each=cycles(m, 50))
    assert Presence.presence(m, v) == Presence.HOME and commands(requests_mock) == []
    assert paid(requests_mock) == (0, 0, 0) and car.charge["charge_limit_soc"] == 50


def test_2026_10_12_return_plugged_in_keeps_the_policy_limit(requests_mock):
    # Restored while away; back and plugged in: the policy limit again over
    # BLE; paid == (0, 0, 0)
    m, r, v, c, api = install(held=(90, 50))
    b = realBle(m)
    v.observe("chargeLimit", 50, TM)
    policy = cycles(m, 50)
    c.t += 1
    placeAt(m, v, AWAY)
    passes(r, c, 600, each=policy)
    c.t += 1
    placeAt(m, v, HOME)
    push(v, c, chargingState="Charging")
    passes(r, c, 600, each=policy)
    assert [args for vin, cmd, args in b.runs] == [90, None, 50]
    assert m.heldLimits.get(VIN) == HeldLimit(90, 50)


def test_2026_10_12_arrival_during_a_running_restore_ends_at_the_policy_limit(
    requests_mock,
):
    # The plug-in lands while the restore runs; paid == (0, 0, 0)
    m, r, v, c, api = install(held=(90, 50))
    v.observe("chargeLimit", 50, TM)
    c.t += 1
    placeAt(m, v, AWAY)

    def answer(command, args):
        if args == 90:
            c.t += 1
            placeAt(m, v, HOME)
            push(v, c, chargingState="Charging")
        return "ok"

    b = realBle(m, answers=answer)
    passes(r, c, 600, each=cycles(m, 50))
    assert [args for vin, cmd, args in b.runs] == [90, None, 50]
    assert m.heldLimits.get(VIN) == HeldLimit(90, 50)


@pytest.mark.parametrize("where", ["home", "presumed", "away", "unplugged"])
def test_wake_only_a_home_car(requests_mock, where):
    # A start may wake a car that may be here (paid (1, 1, 1)); one known
    # away is never woken (paid (0, 0, 0))
    m, r, v, c, api = install(fleet=True, located=where != "presumed")
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    FleetCar(requests_mock, charging="Stopped")
    if where == "away":
        c.t += 1
        placeAt(m, v, AWAY)
    elif where == "unplugged":
        unplugAtHome(v, c)
    m.startCarsCharging(VIN)
    passes(r, c, 600)
    woken = where in ("home", "presumed")
    assert paid(requests_mock) == ((1, 1, 1) if woken else (0, 0, 0))


def test_a_drive_inside_the_radius_spends_no_budget_and_the_park_applies(
    requests_mock,
):
    # TeslaMate + Fleet, no BLE. Twenty minutes of driving inside the radius,
    # state pushed every 5 s: each push plans the held raise again, and only
    # requests that went out count against its budget. Parked, it goes:
    # paid == (1, 0, 1)
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="online", limit=50)
    car.shift = "D"
    v.observe("chargeLimit", 50, TM)
    push(v, c, state="driving")
    policy = cycles(m, 80)
    passes(
        r, c, 1200, step=5, each=lambda: (policy(), v.observe("state", "driving", TM))
    )
    rec = m.desired.record(VIN, CHARGE_LIMIT)
    assert commands(requests_mock) == [] and rec.heldAt is not None
    assert rec.paidSpent == paid(requests_mock)[0] + paid(requests_mock)[1]
    car.shift = "P"
    push(v, c, state="online", shiftState="P")
    passes(r, c, 10, each=policy)
    assert car.charge["charge_limit_soc"] == 80 and paid(requests_mock) == (1, 0, 1)


# The Gen2 unplug: a 00 whose first heartbeat drops the slave's max amps
# from above 0 to 0, held a minute, unless live telemetry says plugged in


def gen2(monkeypatch, m, c, vin=VIN, maxA=32.0):
    # The serial thread's clock is the test's; the TWC links at 03 with the
    # car's max amps and, given a vin, names the car by its VIN read
    monkeypatch.setattr(time, "time", c)
    s = m.getSlaveTWCs()[0]
    heartbeat(m, s, 0.0, state=0x03, amps_max=maxA)
    if vin:
        s.currentVIN = vin
        m.recordVehicleVIN(s)
        s.currentVIN = ""
    return s


def followsPlug(s, c):
    # BLE's charge read of a car in range: plugged in at the TWC's max while
    # the TWC shows a car (a state, or a max above 0), else Disconnected. A
    # read takes time: what it finds is newer than the ping before it
    def fetch(vin, kind):
        c.t += 0.01
        plugged = s.reportedState or s.reportedAmpsMax
        state = "Stopped" if plugged else "Disconnected"
        return chargeRead(**{vin: (state, s.reportedAmpsMax)})(vin, kind)

    return fetch


def beats(m, r, c, s, seconds, state, maxA, policy=None, step=10):
    # A heartbeat every step, the policy pass every 30 s, a reconcile pass
    # every 10 s
    for _ in range(int(seconds / step)):
        c.t += step
        heartbeat(m, s, 0.0, state=state, amps_max=maxA)
        if policy and int(c.t) % 30 == 0:
            policy()
        if int(c.t) % 10 == 0:
            r.pass_()


def limits(b, vin=VIN):
    return [args for v, cmd, args in b.runs if v == vin and cmd == "charging-set-limit"]


def test_box_2026_10_18_departure_restores_over_ble_within_a_minute(
    requests_mock, monkeypatch
):
    # 13:08: a VIN TWC at 03 with max 32 A, the limit held, Fleet asleep, no
    # telemetry; the check's BLE read finds the car Disconnected, and the
    # next policy pass restores. paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m, answers=lambda command, args: b.sent.append(c.t) or "ok")
    b.sent = []
    FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, "TeslaBLE")
    policy = cycles(m, 50)
    policy()  # the policy in force when the TWC names the car
    s = gen2(monkeypatch, m, c)
    b._fetch_state_json = followsPlug(s, c)
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    assert limits(b) == []
    onset = c.t + 10
    beats(m, r, c, s, 600, 0x00, 0.0, policy)
    # With 32 A to share the car was started and rated over BLE; the restore
    # itself woke nothing
    assert limits(b) == [90] and bleCommands(b)[-2:] == ["charging-set-amps"] + [
        "charging-set-limit"
    ]
    sent = [t for t, run in zip(b.sent, b.runs) if run[1] == "charging-set-limit"]
    assert len(sent) == 1 and onset + s.CHECK_WAIT <= sent[0] <= onset + 60
    assert paid(requests_mock) == (0, 0, 0) and m.heldLimits.get(VIN) is None


def test_box_2026_10_18_false_00_at_47_a_with_telemetry_plugged_is_no_departure(
    requests_mock, monkeypatch
):
    # 15:25:58-15:36:27: a waking car's 00 keeps 47 A; TeslaMate said plugged
    # in before it and nothing since, so BLE's read answers: plugged in.
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m)
    FleetCar(requests_mock, state="online", limit=50)
    push(v, c, state="online", pluggedIn=True, chargeLimit=50)
    s = gen2(monkeypatch, m, c)
    b._fetch_state_json = followsPlug(s, c)
    policy = cycles(m, 50)
    beats(m, r, c, s, 600, 0x03, 47.0, policy)
    beats(m, r, c, s, 630, 0x00, 47.0, policy)
    assert v.markAt("unplug") == Presence.NEVER
    assert Presence.presence(m, v) == Presence.HOME
    assert limits(b) == [] and paid(requests_mock) == (0, 0, 0)


def test_box_2026_10_17_morning_wake_episodes_declare_no_departure(
    requests_mock, monkeypatch
):
    # 07:58:28-08:39:01: a plugged car waking makes the TWC say 00 between
    # brief 09/03 replies, its max never 0 at a 00 onset; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m)
    FleetCar(requests_mock, state="online", limit=50)
    v.observe("chargeLimit", 50, "TeslaBLE")
    policy = cycles(m, 50)
    policy()  # the policy in force when the TWC names the car
    s = gen2(monkeypatch, m, c)
    b._fetch_state_json = followsPlug(s, c)
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    runs = [
        (0x00, 487),
        (0x09, 13),
        (0x00, 49),
        (0x09, 12),
        (0x00, 370),
        (0x03, 15),
        (0x00, 129),
        (0x03, 14),
        (0x00, 123),
        (0x04, 375),
        (0x00, 598),
        (0x03, 4),
        (0x00, 244),
        (0x01, 600),
    ]
    for state, seconds in runs:
        beats(m, r, c, s, seconds, state, 32.0, policy, step=1)
    assert v.markAt("unplug") == Presence.NEVER and v.value("twcVin") == "8244"
    # Started and rated over BLE with the 32 A to share, a read showing it
    # Stopped sends the start again (the car here never draws); no limit
    assert limits(b) == [] and "charging-set-limit" not in bleCommands(b)
    assert paid(requests_mock) == (0, 0, 0)


def test_a_sleeping_car_at_a_gen2_00_is_never_woken_by_a_restore(
    requests_mock, monkeypatch
):
    # Three real unplugs and returns; BLE never answers. An unplug wakes a
    # car, but Fleet here answers asleep throughout: it stands for a car no
    # provider can read, so no unplug is declared, and nothing restores or
    # wakes it. At its limit, it is not started: paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, held=(90, 50))
    b = realBle(m, answers=None, ping=False)
    FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, "TeslaBLE")
    v.observe("batteryLevel", 50, "TeslaBLE")
    policy = cycles(m, 50)
    policy()  # the policy in force when the TWC names the car
    s = gen2(monkeypatch, m, c)
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    for _ in range(3):
        beats(m, r, c, s, 300, 0x00, 0.0, policy)
        beats(m, r, c, s, 300, 0x03, 32.0, policy)
    assert v.markAt("unplug") == Presence.NEVER
    assert "wake" not in bleCommands(b) and paid(requests_mock) == (0, 0, 0)


def test_an_idle_car_at_0_a_never_departs_on_a_twc_00_even_when_the_re_apply_would_be_a_raise(
    requests_mock, monkeypatch
):
    # The car at 03 offers nothing (max 0 A), its limit under the policy's,
    # no telemetry: no unplug, no arrival, no wake; paid == (0, 0, 0)
    import TWCManager.TWCMaster as master
    import TWCManager.Vehicle.Reconcile.Targets as targets

    m, r, v, c, api = install(fleet=True)
    b = realBle(
        m, ping=lambda vin: not v.observe("state", "asleep", "TeslaBLE") or True
    )
    FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, "TeslaBLE")
    s = gen2(monkeypatch, m, c, maxA=0.0)
    arrivals = []
    for module in (master, targets):
        monkeypatch.setattr(module, "onArrival", lambda m, v: arrivals.append(v))
    policy = cycles(m, 80)
    beats(m, r, c, s, 600, 0x03, 0.0, policy)
    for _ in range(3):
        beats(m, r, c, s, 300, 0x00, 0.0, policy)
        beats(m, r, c, s, 300, 0x03, 0.0, policy)
    assert v.markAt("unplug") == Presence.NEVER and arrivals == []
    # With 32 A to share, the raise to a car that would charge wakes it over
    # BLE, after its start
    assert bleCommands(b)[-2:] == ["wake", "charging-set-limit"]
    assert paid(requests_mock) == (0, 0, 0)


def test_a_sleeping_car_at_00_with_power_available_is_woken_for_a_raise(
    requests_mock, monkeypatch
):
    # 32 A to share; the named car asleep at 00 (max 0 A), its limit at its
    # battery: the policy's raise wakes it over BLE, and the TWC still
    # holding it is admitted, so it is started too; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True)
    b = realBle(
        m, ping=lambda vin: not v.observe("state", "asleep", "TeslaBLE") or True
    )
    FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, "TeslaBLE")
    v.observe("batteryLevel", 50, "TeslaBLE")
    s = gen2(monkeypatch, m, c, maxA=32.0)
    m.setMaxAmpsToDivideAmongSlaves(32)
    beats(m, r, c, s, 300, 0x00, 0.0, cycles(m, 50))
    beats(m, r, c, s, 1800, 0x00, 0.0, cycles(m, 80))
    assert bleCommands(b) == ["wake", "charging-set-limit", "wake", "charging-start"]
    assert limits(b) == [80] and paid(requests_mock) == (0, 0, 0)


def test_no_gps_source_plug_unplug_cycle_end_to_end(requests_mock, monkeypatch):
    # BLE and a VIN-less TWC only: no position, no telemetry, no Tesla API.
    # The unplug departs the only car that may be on it; paid == (0, 0, 0)
    m, r, v, c, api = install(located=False, held=(80, 50))
    b = realBle(m)
    v.observe("chargeLimit", 50, "TeslaBLE")
    v.observe("bleReach", True, "TeslaBLE")
    s = gen2(monkeypatch, m, c, vin=None)
    b._fetch_state_json = followsPlug(s, c)
    policy = cycles(m, 50)
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    assert Presence.presence(m, v) == Presence.HOME and limits(b) == []
    beats(m, r, c, s, 120, 0x00, 0.0, policy)
    assert Presence.presence(m, v) == Presence.NOT_HOME
    assert limits(b) == [80] and m.heldLimits.get(VIN) is None
    assert requests_mock.call_count == 0


def test_gen2_steady_00_after_link_does_not_block_startup_charging(
    requests_mock, monkeypatch
):
    # Plugged in and asleep across the restart: 03 for two minutes after the
    # link, then a steady 00 inside the link grace, the max dropping to 0:
    # no departure and nothing restored, and the start goes over BLE once
    # the ping answers: the car, on no point, has room; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, located=False, held=(90, 50))
    b = realBle(m)
    FleetCar(requests_mock, state="asleep", limit=50)
    v.observe("chargeLimit", 50, "TeslaBLE")
    policy = cycles(m, 50)
    s = gen2(monkeypatch, m, c, vin=None, maxA=47.0)
    beats(m, r, c, s, 120, 0x03, 47.0, policy)
    beats(m, r, c, s, 900, 0x00, 0.0, policy)
    assert v.markAt("unplug") == Presence.NEVER
    assert Presence.presence(m, v) != Presence.NOT_HOME
    assert limits(b) == [] and bleCommands(b) == ["wake", "charging-start"]
    m.setMaxAmpsToDivideAmongSlaves(32)
    m.startCarsCharging(VIN)
    beats(m, r, c, s, 60, 0x00, 0.0, policy)
    # The edge's check, its one follow-up when that check can't tell, and
    # the start's (the car only presumed home)
    assert b.pings == [VIN] * 3 and Presence.presence(m, v) == Presence.HOME
    assert "charging-start" in bleCommands(b) and limits(b) == []
    assert m.heldLimits.get(VIN) == HeldLimit(90, 50)
    assert paid(requests_mock) == (0, 0, 0)


def test_plug_in_on_a_vin_twc_comes_home_with_one_apply(requests_mock, monkeypatch):
    # Away, then plugged in on a TWC that reads its VIN: home, and the policy
    # limit goes once over BLE; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, at=AWAY)
    b = realBle(m)
    FleetCar(requests_mock, state="asleep", limit=80)
    v.observe("chargeLimit", 80, "TeslaBLE")
    policy = cycles(m, 50)
    passes(r, c, 300, each=policy)
    assert Presence.presence(m, v) == Presence.NOT_HOME and limits(b) == []
    s = gen2(monkeypatch, m, c)
    assert Presence.presence(m, v) == Presence.HOME
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    assert limits(b) == [50] and m.heldLimits.get(VIN) == HeldLimit(80, 50)
    assert paid(requests_mock) == (0, 0, 0)


def test_a_protocol_2_gen2_with_power_at_its_minimum_keeps_one_start_budget(
    requests_mock, monkeypatch
):
    # Fleet only; the TWC speaks protocol 2, so its gate starts the car
    # when it is offered its minimum. The car accepts starts but stays
    # Stopped, a feed says so every 30 s, and the power swings across the
    # TWC's 6 A minimum every 90 s for an hour. A start that has gone waits
    # for room, at most two sends a desire: paid == (0, 0, 3), its rate and
    # two starts
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="online", limit=80, soc=50)
    reads = dict(chargingState="Stopped", chargeLimit=80, batteryLevel=50)
    for field, value in dict(reads, state="online").items():
        v.observe(field, value, "FleetAPI")
    s = gen2(monkeypatch, m, c)
    s.protocolVersion = 2
    t0 = c.t
    while c.t - t0 < 3600:
        c.t += 10
        m.setMaxAmpsToDivideAmongSlaves((7, 5)[int((c.t - t0) // 90) % 2])
        if int(c.t) % 30 == 0:
            car.charge["charging_state"] = "Stopped"
            v.observe("chargingState", "Stopped", "FleetTelemetryMQTT")
        heartbeat(m, s, 0.0, state=0x03, amps_max=32.0)
        r.pass_()
    assert commands(requests_mock) == ["set_charging_amps"] + ["charge_start"] * 2
    assert paid(requests_mock) == (0, 0, 3)


# A VIN-less plug: one attribution names the car, else nobody is rated


def attributeQueued(m):
    # What the BLE worker does with the serial thread's attribution jobs
    runPending(m.reconciler.ble)


def chargeRead(**cars):
    # BLE's charge state per VIN: (chargingState, pilot amps); None for a
    # car the radio does not answer
    def fetch(vin, kind):
        if kind != "charge" or cars.get(vin) is None:
            return None
        state, pilot = cars[vin]
        cs = {"chargingState": {state: {}}, "chargerPilotCurrent": pilot}
        return {"chargeState": dict(cs, chargerActualCurrent=0)}

    return fetch


def twoCarsHome(requests_mock, **kw):
    # Two listed cars parked at home since before the plug, limits known,
    # both paired over BLE
    m, r, v, c, api = install(fleet=True, **kw)
    api.addVehicle({"id": 2, "vin": VIN2, "display_name": "Two"})
    two = m.vehicleRegistry.get(VIN2)
    placeAt(m, two, HOME)
    b = realBle(m, vins=(VIN, VIN2))
    for vin in (VIN, VIN2):
        FleetCar(requests_mock, vin=vin, state="asleep", limit=80)
        m.vehicleRegistry.get(vin).observe("chargeLimit", 80, "TeslaBLE")
    return m, r, v, two, c, b


def test_vin_less_single_candidate_plug_one_apply(requests_mock, monkeypatch):
    # Scenario 3: away, home, then plugged in on a TWC that reads no VIN: the
    # only candidate is named and the policy limit goes once over BLE;
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, at=AWAY)
    b = realBle(m)
    FleetCar(requests_mock, state="asleep", limit=80)
    v.observe("chargeLimit", 80, "TeslaBLE")
    policy = cycles(m, 50)
    passes(r, c, 300, each=policy)
    c.t += 10
    placeAt(m, v, HOME)
    s = gen2(monkeypatch, m, c, vin=None)
    attributeQueued(m)
    assert v.value("twcVin") == "8244" and m.vehicleRegistry.get("anon:8244") is None
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    assert limits(b) == [50] and m.heldLimits.get(VIN) == HeldLimit(80, 50)
    assert paid(requests_mock) == (0, 0, 0)


def test_vin_less_two_candidates_ble_pilot_one_apply(requests_mock, monkeypatch):
    # Scenario 4: two cars at home, each given the policy limit once; BLE
    # reads one charging at the TWC's 32 A pilot and the other unplugged:
    # that one is named and it alone is rated; paid == (0, 0, 0)
    m, r, v, two, c, b = twoCarsHome(requests_mock)
    b._fetch_state_json = chargeRead(
        **{VIN: ("Charging", 32), VIN2: ("Disconnected", 0)}
    )
    m.settings["chargeRateControl"] = "2"
    policy = cycles(m, 50)
    s = gen2(monkeypatch, m, c, vin=None)
    m.setMaxAmpsToDivideAmongSlaves(10)
    attributeQueued(m)
    assert v.value("twcVin") == "8244" and two.value("twcVin") is None
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    assert limits(b) == [50] and limits(b, VIN2) == [50]
    rated = {vin for vin, cmd, args in b.runs if cmd == "charging-set-amps"}
    assert rated == {VIN} and paid(requests_mock) == (0, 0, 0)


def test_vinless_ambiguous_plug_rates_nobody_and_logs_once(
    requests_mock, monkeypatch, caplog
):
    # Scenario 5: two cars at home, BLE answers neither: nobody is named or
    # rated, and the ambiguity logs once for the plug; paid == (0, 0, 0)
    m, r, v, two, c, b = twoCarsHome(requests_mock)
    m.settings["chargeRateControl"] = "2"
    m.setMaxAmpsToDivideAmongSlaves(10)
    policy = cycles(m, 50)
    with caplog.at_level("INFO"):
        s = gen2(monkeypatch, m, c, vin=None)
        for _ in range(60):
            beats(m, r, c, s, 10, 0x03, 32.0, policy)
            attributeQueued(m)
    assert caplog.text.count("8244: no single car plugged in; left anonymous") == 1
    assert v.value("twcVin") is None and two.value("twcVin") is None
    assert s.vehicleKey == "anon:8244"
    assert {cmd for vin, cmd, args in b.runs} <= {"charging-set-limit", "wake"}
    assert paid(requests_mock) == (0, 0, 0)


def test_vinless_rate_never_falls_back_to_vehicles_0(requests_mock):
    # (h): row 25 with neither car plugged in: the anonymous point rates
    # nobody, where main rated vehicles[0]; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True)
    api.addVehicle({"id": 2, "vin": VIN2, "display_name": "Two"})
    for vin in (VIN, VIN2):
        FleetCar(requests_mock, vin=vin, state="online", charging="Disconnected")
        car = m.vehicleRegistry.get(vin)
        placeAt(m, car, HOME)
        car.observe("chargingState", "Disconnected", "TeslaMateVehicle")
    s = m.getSlaveTWCs()[0]
    s.protocolVersion = 2
    m.settings["chargeRateControl"] = "2"
    m.setMaxAmpsToDivideAmongSlaves(10)
    m.vehicleRegistry.beginPlugSession("8244")
    rated = []

    def beat():
        heartbeat(m, s, 10.0, state=0x01)
        rated.extend(k for k in (VIN, VIN2) if m.desired.get(k, AMPS))

    passes(r, c, 600, each=beat)
    assert (
        rated == [] and amps(requests_mock) == [] and paid(requests_mock) == (0, 0, 0)
    )


def test_supercharger_telemetry_plugged_is_not_home(requests_mock, monkeypatch):
    # Scenario 6: the car charges at a Supercharger, its live feed saying
    # plugged in, as something plugs in at home on a TWC that reads no VIN:
    # the car is not home, not named and not applied; paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, at=AWAY)
    b = realBle(m)
    FleetCar(requests_mock, state="online", limit=80, charging="Charging")
    v.observe("chargeLimit", 80, "TeslaBLE")
    policy = cycles(m, 50)
    passes(r, c, 300, each=policy)
    push(v, c, fastChargerPresent=True, pluggedIn=True, chargingState="Charging")
    s = gen2(monkeypatch, m, c, vin=None)
    attributeQueued(m)
    beats(m, r, c, s, 600, 0x03, 32.0, policy)
    assert Presence.presence(m, v) == Presence.NOT_HOME
    assert v.value("twcVin") is None and limits(b) == []
    assert paid(requests_mock) == (0, 0, 0)


def test_a_returning_car_on_an_unnamed_gen2_asleep_starts_over_ble(
    requests_mock, monkeypatch
):
    # BLE and a VIN-less TWC only: after the unplug the car returns, BLE
    # reaches it, and the TWC says 03 asleep at 6 A with no VIN read. The
    # plug is attributed to it, so a start for the car goes over BLE;
    # paid == (0, 0, 0)
    m, r, v, c, api = install(located=False)
    b = realBle(m)
    v.observe("chargeLimit", 50, "TeslaBLE")
    v.observe("bleReach", True, "TeslaBLE")
    s = gen2(monkeypatch, m, c, vin=None)
    b._fetch_state_json = followsPlug(s, c)
    attributeQueued(m)
    assert v.value("twcVin") == "8244"
    beats(m, r, c, s, 600, 0x03, 32.0)
    beats(m, r, c, s, 120, 0x00, 0.0)
    assert Presence.presence(m, v) == Presence.NOT_HOME
    c.t += 600
    v.observe("bleReach", True, "TeslaBLE")
    beats(m, r, c, s, 10, 0x03, 6.0)
    attributeQueued(m)
    assert v.value("twcVin") == "8244" and Presence.pluggedHere(m, v)
    m.startCarsCharging(VIN)
    beats(m, r, c, s, 60, 0x03, 6.0)
    assert "charging-start" in bleCommands(b)
    assert paid(requests_mock) == (0, 0, 0)


def test_a_plug_attributed_before_the_policy_pass_sends_a_held_car_nothing(
    requests_mock, monkeypatch
):
    # The no-GPS plug/unplug cycle with each attribution run as the BLE
    # worker runs it, at once: the car is named before the policy's first
    # pass and already reads its held 50, so nothing is restored or applied
    # while it is plugged; the unplug restores 80 once; paid == (0, 0, 0)
    m, r, v, c, api = install(located=False, held=(80, 50))
    b = realBle(m)
    v.observe("chargeLimit", 50, "TeslaBLE")
    v.observe("bleReach", True, "TeslaBLE")
    s = gen2(monkeypatch, m, c, vin=None)
    b._fetch_state_json = followsPlug(s, c)
    attributeQueued(m)
    assert v.value("twcVin") == "8244"
    policy = cycles(m, 50)
    for _ in range(60):
        beats(m, r, c, s, 10, 0x03, 32.0, policy)
        attributeQueued(m)
    assert Presence.presence(m, v) == Presence.HOME and limits(b) == []
    beats(m, r, c, s, 120, 0x00, 0.0, policy)
    assert Presence.presence(m, v) == Presence.NOT_HOME
    assert limits(b) == [80] and m.heldLimits.get(VIN) is None
    assert paid(requests_mock) == (0, 0, 0)


def test_box_2026_10_19_return_and_leave_unplugged_goes_over_ble(requests_mock, caplog):
    # TeslaMate + BLE + Fleet, never plugged in. Away, BLE fails and Fleet
    # finds the car away; it drives back into the radius still out of BLE
    # range (a position a second, held with no ping); parks: the policy
    # limit over BLE; drives off: the restore over BLE in the same second.
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True, located=False)
    m.settings.update(homeLat=HOME[0], homeLon=HOME[1])
    car = FleetCar(requests_mock, state="online", limit=100, soc=60, at=AWAY)
    near = []
    b = realBle(
        m,
        answers=lambda command, args: "ok" if near else None,
        ping=lambda vin: bool(near),
    )
    v.observe("chargeLimit", 100, TM)
    policy = cycles(m, 50)
    passes(r, c, 30, step=1, each=policy)  # after a restart, nothing known
    assert b.pings and b.runs == [] and v.markAt("absent") > Presence.NEVER
    push(v, c, state="driving", shiftState="D", batteryLevel=60)
    placeAt(m, v, AWAY)
    passes(r, c, 900, step=30, each=lambda: (policy(), placeAt(m, v, AWAY)))
    assert m.desired.get(VIN, CHARGE_LIMIT) is None
    pings = len(b.pings)
    car.at = HOME
    push(v, c, chargeLimit=100)
    placeAt(m, v, HOME)

    def driving():
        if c.t % 30 == 0:
            policy()
        placeAt(m, v, HOME)

    with caplog.at_level("INFO"):
        passes(r, c, 60, step=1, each=driving)
    held = [x for x in caplog.records if "held: driving" in x.getMessage()]
    assert len(held) == 1 and commands(requests_mock) == []
    assert len(b.pings) == pings and b.runs == []  # held before any ping
    near.append(True)
    push(v, c, shiftState="P", state="online")
    passes(r, c, 3, step=1, each=driving)
    assert limits(b) == [50] and paid(requests_mock) == (0, 0, 0)
    assert m.heldLimits.get(VIN) == HeldLimit(100, 50)
    passes(r, c, 75, step=1, each=driving)
    # Shift D in the driveway is the departure: restored at once, with the
    # car still inside the radius and in BLE range
    push(v, c, shiftState="D", state="driving")
    r.pass_()
    assert limits(b) == [50, 100] and m.heldLimits.get(VIN) is None
    assert paid(requests_mock) == (0, 0, 0) and Presence.inside(m, v)


def test_box_2026_10_19_plug_in_at_the_limit_names_the_only_car(
    requests_mock, monkeypatch
):
    # 15:54: the only car, parked at home with BLE contact seconds ago and its
    # limit 50 under its battery 60, plugs in on a Gen2 that reads VINs but
    # reads none. TeslaMate still says unplugged (from before the plug), and
    # BLE reads Complete at the 47 A the offer rose to from 6 A during the
    # read: it is still the car. paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", limit=50, soc=60)
    b = realBle(m)
    read = chargeRead(**{VIN: ("Complete", 47)})
    m.settings["SlaveTWCs"] = {"8244": {"supportsVINQuery": 1}}
    m.heldLimits.set(VIN, 100, 50)
    held = m.heldLimits.get(VIN)
    push(v, c, pluggedIn=False, chargingState="Disconnected")
    c.t += 9000
    push(v, c, state="online", shiftState="P", chargeLimit=50, batteryLevel=60)
    v.observe("bleReach", True, "TeslaBLE")
    policy = cycles(m, 50)
    policy()
    c.t += 90
    s = gen2(monkeypatch, m, c, vin=None, maxA=6.0)
    b._fetch_state_json = lambda vin, kind: setattr(s, "reportedAmpsMax", 47.0) or (
        read(vin, kind)
    )
    attributeQueued(m)
    assert v.value("twcVin") == "8244" and s.vehicleKey == VIN
    push(v, c, gap=8, pluggedIn=True, chargingState="Complete")
    beats(m, r, c, s, 10, 0x03, 47.0, policy)
    beats(m, r, c, s, 600, 0x00, 47.0, policy)  # idle at its limit
    assert v.value("twcVin") == "8244" and Presence.pluggedHere(m, v)
    assert limits(b) == [] and m.heldLimits.get(VIN) == held
    assert "wake" not in bleCommands(b) and paid(requests_mock) == (0, 0, 0)


def test_a_plug_left_anonymous_is_named_by_the_feed_s_plug_in(
    requests_mock, monkeypatch
):
    # Two cars at home, neither answering BLE: the plug is left anonymous
    # until TeslaMate reports one of them plugged in. paid == (0, 0, 0)
    m, r, v, two, c, b = twoCarsHome(requests_mock)
    b._fetch_state_json = chargeRead()
    policy = cycles(m, 50)
    passes(r, c, 60, each=policy)
    s = gen2(monkeypatch, m, c, vin=None)
    attributeQueued(m)
    assert s.vehicleKey == "anon:8244"
    push(v, c, gap=8, pluggedIn=True, chargingState="Stopped")
    attributeQueued(m)
    assert s.vehicleKey == VIN and two.value("twcVin") is None
    assert paid(requests_mock) == (0, 0, 0)


def test_box_2026_10_20_a_waking_00_keeps_the_plug_for_the_feed_to_name(
    requests_mock, monkeypatch
):
    # 10:13:59: the only car plugs in at 0 A offered, and BLE reads it at a
    # 47 A pilot, so the plug is left unnamed; 6 A, then 47 A; at 10:14:16
    # the TWC says 00 with its max still 47 A while the car rests; TeslaMate
    # reports the plug-in at 10:14:30 and names it. paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", limit=50, soc=60)
    b = realBle(m)
    b._fetch_state_json = chargeRead(**{VIN: ("Complete", 47)})
    push(v, c, pluggedIn=False, chargingState="Disconnected")
    c.t += 9000
    push(v, c, state="online", shiftState="P", chargeLimit=50, batteryLevel=60)
    policy = cycles(m, 50)
    policy()
    s = gen2(monkeypatch, m, c, vin=None, maxA=0.0)
    attributeQueued(m)
    assert s.vehicleKey == "anon:8244"
    beats(m, r, c, s, 2, 0x09, 6.0, step=2)
    beats(m, r, c, s, 2, 0x09, 47.0, step=2)
    beats(m, r, c, s, 13, 0x03, 47.0, step=13)
    beats(m, r, c, s, 14, 0x00, 47.0, step=1)
    assert m.vehicleRegistry.vehicleAt("8244").isAnonymous
    push(v, c, pluggedIn=True, chargingState="Complete")
    attributeQueued(m)
    assert v.value("twcVin") == "8244" and Presence.pluggedHere(m, v)
    c.t += (30 - c.t % 30) % 30
    s.protocolVersion = 2
    beats(
        m,
        r,
        c,
        s,
        600,
        0x00,
        47.0,
        lambda: m.setMaxAmpsToDivideAmongSlaves(32) or policy(),
    )
    assert v.value("twcVin") == "8244" and Presence.pluggedHere(m, v)
    assert "wake" not in bleCommands(b) and paid(requests_mock) == (0, 0, 0)


def unnamedAt00(requests_mock, monkeypatch):
    # Two cars at home, neither answering BLE, so the plug stays unnamed;
    # car 1 reads Stopped (plugged in, here by elimination), limit 80 over
    # battery 50; the TWC says 00 with its max still 32 A, so the session
    # stays open
    m, r, v, two, c, b = twoCarsHome(requests_mock)
    b._fetch_state_json = chargeRead()
    s = gen2(monkeypatch, m, c, vin=None)
    s.protocolVersion = 2
    m.setMaxAmpsToDivideAmongSlaves(0)  # nothing to share until a row says
    attributeQueued(m)
    assert s.vehicleKey == "anon:8244"
    v.observe("batteryLevel", 50, "FleetAPI")
    v.observe("chargingState", "Stopped", "FleetAPI")
    beats(m, r, c, s, 400, 0x03, 32.0)
    assert s.vehicleKey == "anon:8244"
    c.t += (30 - c.t % 30) % 30
    beats(m, r, c, s, 30, 0x00, 32.0)
    assert m.vehicleRegistry.vehicleAt("8244").isAnonymous
    assert s.vehicleKey is None
    return m, r, v, c, s, b


def test_an_unnamed_plug_at_00_wakes_no_candidate_for_a_raise(
    requests_mock, monkeypatch
):
    # The policy raises the limit to 90 while the unnamed TWC rests at 00
    # with no power to share, so the raise waits for a free channel.
    # paid == (0, 0, 0)
    m, r, v, c, s, b = unnamedAt00(requests_mock, monkeypatch)
    beats(m, r, c, s, 600, 0x00, 32.0, cycles(m, 90))
    assert "wake" not in bleCommands(b) and paid(requests_mock) == (0, 0, 0)


def test_an_unnamed_plug_at_00_starts_only_the_car_plugged_there_as_the_offer_rises(
    requests_mock, monkeypatch
):
    # The offer rises while the unnamed TWC rests at 00 with a car not full:
    # the TWC reports no car there, so no start fans out. The car plugged in
    # here, by elimination, would charge and has room: it alone is started,
    # by its VIN, and its own endpoint rates it over BLE. paid == (0, 0, 0)
    m, r, v, c, s, b = unnamedAt00(requests_mock, monkeypatch)
    starts = []
    monkeypatch.setattr(m, "startCarsCharging", starts.append)
    for amps in (6, 12, 24, 32):
        m.setMaxAmpsToDivideAmongSlaves(amps)
        beats(m, r, c, s, 60, 0x00, 32.0)
    assert starts == [] and m.desired.get(VIN2, CHARGING) is None
    assert m.desired.get(VIN, CHARGING).value is True
    assert bleCommands(b).count("charging-start") == 1
    assert paid(requests_mock) == (0, 0, 0)


def test_a_car_just_unplugged_is_not_the_next_plug_s(requests_mock, monkeypatch):
    # A swap: car 1 unplugs (its feed says so) and car 2, with no feed and
    # no BLE answer, plugs in. Both are inside and only car 1 answered BLE:
    # car 2 is the match. paid == (0, 0, 0)
    m, r, v, two, c, b = twoCarsHome(requests_mock)
    b._fetch_state_json = chargeRead()
    push(v, c, pluggedIn=True, chargingState="Stopped")
    push(v, c, gap=60, pluggedIn=False, chargingState="Disconnected")
    v.observe("bleReach", True, "TeslaBLE")
    b._runPing = lambda vin, scan=True: vin == VIN
    c.t += 30
    s = gen2(monkeypatch, m, c, vin=None)
    attributeQueued(m)
    assert v.value("twcVin") is None and s.vehicleKey == two.vin
    assert paid(requests_mock) == (0, 0, 0)


def test_a_plug_unnamed_for_long_does_not_take_a_car_plugged_in_elsewhere(
    requests_mock, monkeypatch
):
    # The TWC has been anonymous for an hour; the feed's plug-in now is
    # another charger's. paid == (0, 0, 0)
    m, r, v, two, c, b = twoCarsHome(requests_mock)
    b._fetch_state_json = chargeRead()
    s = gen2(monkeypatch, m, c, vin=None)
    attributeQueued(m)
    c.t += 3600
    push(v, c, pluggedIn=True, chargingState="Stopped")
    attributeQueued(m)
    assert s.vehicleKey == "anon:8244" and v.value("twcVin") is None
    assert paid(requests_mock) == (0, 0, 0)


def test_box_2026_10_19_no_ble_run_for_a_car_driving_or_away(requests_mock):
    # 15:49:52: the car drives inside the radius, out of BLE range, when the
    # policy pass gives it the limit: held for driving before any ping or
    # read, each of which ties up the BLE worker for 25 s. A desire set while
    # the car was home and run once it is seen away is cleared the same way.
    # paid == (0, 0, 0)
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state="online", limit=100)
    b = realBle(m, answers=None, ping=False)
    b.fetched = []
    b._fetch_state_json = lambda vin, kind: b.fetched.append(kind)
    v.observe("chargeLimit", 100, TM)
    push(v, c, state="driving", shiftState="D")
    placeAt(m, v, HOME)
    policy = cycles(m, 50)
    passes(r, c, 60, step=1, each=lambda: c.t % 30 or policy())
    assert (b.pings, b.runs, b.fetched) == ([], [], [])
    assert m.desired.record(VIN, CHARGE_LIMIT).heldAt is not None
    push(v, c, state="online", shiftState="P")
    m.desired.set(VIN, CHARGE_LIMIT, 50, "policy", True)
    placeAt(m, v, AWAY)
    r.pass_()
    runPending(m.reconciler.ble)
    assert m.desired.get(VIN, CHARGE_LIMIT) is None
    assert (b.pings, b.runs, b.fetched) == ([], [], [])
    assert paid(requests_mock) == (0, 0, 0)


# A Gen2 status edge checks the car: the feed, else BLE (asleep is still
# plugged in), else the read cascade; nothing that can check means no unplug


def checkedCar(requests_mock, monkeypatch, fleet="online", ping="online"):
    # The box, 2026-10-20 12:44: the car named on TWC 8244 at 03 with 47 A,
    # BLE in range answering its sleep status, Fleet with the car's state;
    # BLE's charge read follows the plug, each read's time in b.reads
    m, r, v, c, api = install(fleet=True)
    FleetCar(requests_mock, state=fleet, limit=50, soc=60)
    b = realBle(m, ping=lambda vin: not v.observe("state", ping, "TeslaBLE") or True)
    s = gen2(monkeypatch, m, c, maxA=47.0)
    b.reads, fetch = [], followsPlug(s, c)
    b._fetch_state_json = lambda vin, kind: b.reads.append(c.t) or fetch(vin, kind)
    return m, r, v, c, s, b


def boxUnplugs(m, r, c, s, v, feed=lambda plugged: None):
    # 12:45:23 woken by the app, 00 at 47 A; 12:46:12 unplugged, the max
    # drops to 0; 12:46:48 plugged in again for 27 s, named by its VIN read;
    # 12:47:15 unplugged, 00 at 0 A. feed(plugged) is the feed's report, 13 s
    # after each unplug and 8 s after the plug. Returns each unplug's mark
    beats(m, r, c, s, 49, 0x00, 47.0, step=7)
    assert v.value("twcVin") == "8244" and v.markAt("unplug") == Presence.NEVER
    beats(m, r, c, s, 12, 0x00, 0.0, step=2)
    feed(False)
    beats(m, r, c, s, 22, 0x00, 0.0, step=2)
    first = v.markAt("unplug")
    assert first > Presence.NEVER and v.value("twcVin") is None
    beats(m, r, c, s, 2, 0x09, 6.0, step=2)
    s.currentVIN = VIN
    m.recordVehicleVIN(s)
    s.currentVIN = ""
    beats(m, r, c, s, 8, 0x09, 47.0, step=2)
    feed(True)
    beats(m, r, c, s, 2, 0x09, 47.0, step=2)
    beats(m, r, c, s, 16, 0x03, 6.0, step=2)
    assert v.value("twcVin") == "8244" and v.markAt("unplug") == first
    beats(m, r, c, s, 12, 0x00, 0.0, step=2)
    feed(False)
    beats(m, r, c, s, 48, 0x00, 0.0, step=2)
    assert v.markAt("unplug") > first and v.value("twcVin") is None
    return first, v.markAt("unplug")


def test_box_2026_10_20_each_unplug_departs_once_ble_reads_it_disconnected(
    requests_mock, monkeypatch
):
    # The wake's check reads the car plugged in; each unplug's reads it
    # Disconnected. paid == (0, 0, 0)
    m, r, v, c, s, b = checkedCar(requests_mock, monkeypatch)
    beats(m, r, c, s, 600, 0x03, 47.0)
    boxUnplugs(m, r, c, s, v)
    # One more read: the start's, of a car whose charging state was unknown
    assert len(b.reads) == 4 and len(b.pings) == 3
    assert "charging-set-limit" not in bleCommands(b)
    assert paid(requests_mock) == (0, 0, 0)


def test_box_2026_10_20_with_teslamate_live_the_feed_departs_the_car(
    requests_mock, monkeypatch
):
    # The same with TeslaMate reporting each plug change within the wait:
    # those need no BLE check. The wake's 00, which the feed says nothing
    # about, falls through to BLE's. paid == (0, 0, 0)
    m, r, v, c, s, b = checkedCar(requests_mock, monkeypatch)
    push(v, c, state="online", pluggedIn=True)
    beats(m, r, c, s, 600, 0x03, 47.0)
    wake, said = c.t + 7, []
    first, second = boxUnplugs(
        m,
        r,
        c,
        s,
        v,
        lambda plugged: said.append(c.t) or push(v, c, gap=0, pluggedIn=plugged),
    )
    assert [int(t) for t in b.reads] == [wake + 21] and len(b.pings) == 1
    assert (first, second) == (said[0], said[2])  # at the feed's reports
    # Started and rated over BLE with the 32 A to share; no limit
    assert bleCommands(b) == ["wake", "charging-set-amps", "wake", "charging-start"]
    assert paid(requests_mock) == (0, 0, 0)


def test_a_sleeping_car_on_a_03_00_flicker_stays_plugged_in(requests_mock, monkeypatch):
    # The ping says asleep: an unplug would have woken it. No read, no
    # unplug. paid == (0, 0, 0)
    m, r, v, c, s, b = checkedCar(requests_mock, monkeypatch, "asleep", "asleep")
    beats(m, r, c, s, 600, 0x03, 6.0)
    beats(m, r, c, s, 10, 0x00, 6.0)
    beats(m, r, c, s, 120, 0x03, 6.0)
    # The one read is the start's, of a car whose charging state was unknown
    assert b.pings == [VIN] and len(b.reads) == 1
    assert v.markAt("unplug") == Presence.NEVER and v.value("twcVin") == "8244"
    assert paid(requests_mock) == (0, 0, 0)


@pytest.mark.parametrize("state, spent", [("online", (2, 0, 1)), ("asleep", (2, 1, 0))])
def test_fleet_only_checks_a_car_awake_with_one_read(
    requests_mock, monkeypatch, state, spent
):
    # The free online check first; only an awake car is read, and Disconnected
    # departs it. Just restarted, with 32 A to share and its charging state
    # unknown, the car is started: its one wake (if asleep) and read learn it
    # Complete; its rate is set. Awake then, the unplug's check reads it too
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state=state, limit=50, charging="Complete")
    s = gen2(monkeypatch, m, c)
    beats(m, r, c, s, 600, 0x03, 32.0)
    car.charge["charging_state"] = "Disconnected"
    beats(m, r, c, s, 120, 0x00, 0.0)
    assert v.markAt("unplug") > Presence.NEVER and paid(requests_mock) == spent


def test_an_unnamed_session_ends_when_its_only_candidate_reads_disconnected(
    requests_mock, monkeypatch
):
    # A VIN-less TWC left unnamed; the one car that may be on it reads
    # Disconnected over BLE after the 00. paid == (0, 0, 0)
    m, r, v, c, s, b = checkedCar(requests_mock, monkeypatch)
    m.vehicleRegistry.endPlugSession("8244")
    beats(m, r, c, s, 60, 0x00, 0.0)
    beats(m, r, c, s, 400, 0x03, 32.0)
    assert s.vehicleKey == "anon:8244"
    beats(m, r, c, s, 60, 0x00, 0.0)
    assert m.vehicleRegistry.vehicleAt("8244") is None
    assert v.markAt("unplug") > Presence.NEVER and paid(requests_mock) == (0, 0, 0)


def test_fleet_only_checks_past_its_stored_reading(requests_mock, monkeypatch):
    # The wake's check pays for a read, which defers reads 300 s; the unplug
    # 49 s later still reads the car and departs it. Started with 32 A to
    # share, its charging state unknown: one more read, and its rate.
    # paid == (3, 0, 1)
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="online", limit=50, charging="Complete")
    s = gen2(monkeypatch, m, c, maxA=47.0)
    beats(m, r, c, s, 600, 0x03, 47.0)
    beats(m, r, c, s, 49, 0x00, 47.0, step=7)
    assert paid(requests_mock) == (2, 0, 1)
    car.charge["charging_state"] = "Disconnected"
    beats(m, r, c, s, 40, 0x00, 0.0, step=2)
    assert v.markAt("unplug") > Presence.NEVER and v.value("twcVin") is None
    assert paid(requests_mock) == (3, 0, 1)


def test_fleet_only_checks_past_its_cached_asleep(requests_mock, monkeypatch):
    # The wake's check finds the car asleep (no read); the unplug wakes it,
    # and the next check, inside the online check's 120 s cache, asks again
    # and reads it Disconnected. Just restarted with 32 A to share, its
    # charging state unknown, the car is started first: one wake and read
    # learn it Complete. paid == (3, 1, 0)
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="asleep", limit=50, charging="Complete")
    s = gen2(monkeypatch, m, c, maxA=47.0)
    beats(m, r, c, s, 600, 0x03, 47.0)
    beats(m, r, c, s, 49, 0x00, 47.0, step=7)
    assert paid(requests_mock) == (2, 1, 0)
    car.state, car.charge["charging_state"] = "online", "Disconnected"
    beats(m, r, c, s, 40, 0x00, 0.0, step=2)
    assert v.markAt("unplug") > Presence.NEVER and paid(requests_mock) == (3, 1, 0)


def test_box_2026_10_20_a_late_feed_neither_holds_an_unplug_nor_departs_a_replug(
    requests_mock, monkeypatch
):
    # 07:41:59 unplugged; TeslaMate's false comes 44 s later, after the check,
    # which reads BLE and departs the car. 10:13:59 plugged in again, named;
    # 10:14:16 00 at 47 A while the car rests: the old false is no news since
    # that edge, BLE reads it plugged in, and it stays. paid == (0, 0, 0)
    m, r, v, c, s, b = checkedCar(requests_mock, monkeypatch)
    push(v, c, state="online", pluggedIn=True)
    beats(m, r, c, s, 600, 0x03, 47.0)
    beats(m, r, c, s, 30, 0x00, 0.0, step=2)
    assert v.markAt("unplug") > Presence.NEVER and v.value("twcVin") is None
    beats(m, r, c, s, 14, 0x00, 0.0, step=2)
    push(v, c, gap=0, pluggedIn=False)
    left = v.markAt("unplug")
    c.t += 9000
    beats(m, r, c, s, 2, 0x03, 6.0, step=2)
    s.currentVIN = VIN
    m.recordVehicleVIN(s)
    s.currentVIN = ""
    beats(m, r, c, s, 16, 0x03, 47.0, step=2)
    beats(m, r, c, s, 14, 0x00, 47.0, step=2)
    push(v, c, gap=0, pluggedIn=True)
    beats(m, r, c, s, 600, 0x00, 47.0)
    assert v.value("twcVin") == "8244" and v.markAt("unplug") == left
    assert "wake" not in bleCommands(b) and paid(requests_mock) == (0, 0, 0)
