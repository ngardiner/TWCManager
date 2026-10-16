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

from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Reconcile.Desired import AMPS, CHARGE_LIMIT, CHARGING
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimit
from TWCManager.Vehicle.Reconcile.Reconciler import PAID_AFTER, RESTORE_RETRY, RETRY
from TWCManager.Vehicle.TeslaBLE import TeslaBLE
from tests.unit.gen2_helpers import drive as heartbeat
from tests.unit.reconcile_helpers import AWAY, HOME, placeAt
from tests.unit.teslatokens import fleet_token, new_api, owner_token
from tests.unit.test_limit_reconciler import VIN, setup

VIN2 = "5YJ3E1EA1KF000002"
FLEET = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/"
# Fleet API prices; vehicles/{vin} and the vehicle list are free
PRICE = {"vehicle_data": 0.002, "wake_up": 0.02, "command": 0.001}


def counts(requests_mock):
    # Every request, by kind: the paid three, Home Assistant service calls,
    # the free vehicle list and online check, and anything else
    out = dict.fromkeys(
        ("vehicle_data", "wake_up", "command", "ha", "free", "other"), 0
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
        base = FLEET + vin
        requests_mock.get(
            base, json=lambda req, ctx: {"response": {"state": self.state, "vin": vin}}
        )
        requests_mock.post(base + "/wake_up", json=self._wake)
        requests_mock.get(base + "/vehicle_data", json=self._data)
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
                    "shift_state": None,
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


def install(fleet=False, owner=False, policy=None, held=None, at=HOME, located=True):
    # A real master, store and reconciler on a test clock; a real TeslaAPI
    # with the given token slots, the car listed and placed by TeslaMate's
    # free position
    m, r, v, c = setup()
    if policy:
        m.settings["commandPolicy"] = policy
    api = None
    if fleet or owner:
        api = new_api(master=m)
        api.now = c
        api.wakeDelayMins = 0
        if owner:
            api.storeToken(owner_token())
        if fleet:
            api.storeToken(fleet_token())
        for p in api.providers():
            m.providerOrder.register(p)
        m.providerOrder.provider("FleetAPI").clock = c
        api.addVehicle({"id": 1, "vin": VIN, "display_name": "Car"})
    if located:
        placeAt(m, v, at)
    if held:
        m.heldLimits.set(VIN, *held)
    return m, r, v, c, api


def realBle(m, answers="ok", vins=(VIN,)):
    # The real TeslaBLE module; only the tesla-control run is replaced.
    # answers: an output string, None (the radio does not answer: a timeout,
    # which the module retries once), or a callable(command, args)
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
    v.observe("chargeLimit", 50, "TeslaAPI", at=c() - 600)
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


def offlineWake(requests_mock, m, r, c, want, seconds=1800, online=59):
    # Runs want() every pass for seconds; wake_up answers offline and the car
    # comes online online s after it. Returns {"wake": t, "online": t, "sent":
    # t}: the wake, the car online, the first command
    car = FleetCar(requests_mock, limit=50)
    requests_mock.post(
        FLEET + VIN + "/wake_up", json={"response": {"state": "offline"}}
    )
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
    v.observe("chargeLimit", 50, "TeslaAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    car, seen = offlineWake(requests_mock, m, r, c, lambda: None, seconds=3600)
    assert seen["sent"] - seen["online"] <= 150  # the free check noticed it
    assert paid(requests_mock) == (1, 1, 1)  # one wake in the hour: none again
    assert commands(requests_mock) == ["set_charge_limit"]
    assert car.charge["charge_limit_soc"] == 80 and dollars(requests_mock) == 0.023
    assert m.desired.record(VIN, CHARGE_LIMIT).paidSends == 2  # the wake, the send


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
    v.observe("chargeLimit", 50, "TeslaAPI", at=c() - 600)
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
    v.observe("chargeLimit", 50, "TeslaAPI", at=c() - 600)
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
        v.observe(field, value, "TeslaAPI", at=c() - 600)
    m.setPolicyLimit(80, transition=True)
    also = (lambda: m.startCarsCharging(VIN)) if start else (lambda: None)
    times, want = wakeTimes(requests_mock, c, also)
    offlineWake(requests_mock, m, r, c, want, seconds=4 * 3600, online=10**9)
    assert paid(requests_mock) == (0, wakes, 0) and counts(requests_mock)["other"] == 0
    assert all(b - a > 1800 for a, b in zip(times, times[1:]))
    # Only free checks between: one per WAKE_CHECK while a follow-up is due
    # (47 and 94 measured)
    assert counts(requests_mock)["free"] <= (50 if not start else 100)


def test_every_billed_wake_holds_the_next(requests_mock):
    # Provider.wake's contract, which the reconciler's WAKE_CHECK follow-ups
    # rely on: a failed wake by any billed provider that can wake the car
    # makes the next one, inside WAKE_CHECK, a free "wake held", no wake_up
    from TWCManager.Vehicle.Reconcile.Reconciler import WAKE_CHECK

    m, r, v, c, api = install(fleet=True, owner=True)
    wakers = [
        p
        for p in m.providerOrder.providers()
        if p.declares(C.WAKE)
        and p.cost(C.WAKE).tier >= C.TIER_BILLED
        and p.ready(v, C.WAKE)
    ]
    assert wakers
    for p in wakers:
        m, r, v, c, api = install(fleet=True, owner=True)
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
    assert bleCommands(b) == ["charging-set-limit", "charging-set-limit"]
    assert [args for vin, cmd, args in b.runs] == [
        70,
        60,
    ] and requests_mock.call_count == 0
    assert m.heldLimits.get(VIN) == HeldLimit(90, 60)


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
        and bleCommands(b) == ["charging-set-limit"] * 2
    )  # one try plus its quick retry
    passes(r, c, 3600)
    assert paid(requests_mock) == (0, 0, 1) and commands(requests_mock) == [
        "set_charge_limit"
    ]
    assert len(b.runs) == 2 and dollars(requests_mock) == 0.001


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
    assert [args for vin, cmd, args in b.runs] == [50]
    v.observe("chargeLimit", 80, "TeslaMateVehicle")  # the app
    passes(
        r, c, 3600, step=30, each=lambda: m.setPolicyLimit(50, transition=False)
    )  # the policy re-enforces every pass
    assert len(b.runs) == 1 and m.desired.get(VIN, CHARGE_LIMIT).value == 80
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
    g.vehicleRegistry = m.vehicleRegistry
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
        amps(requests_mock) == [4, 4] and s._evseTargetAmps == 6
    )  # the low-amps repeat; the TWC holds its floor
    pt.setTargetPower(10 * 240)
    passes(r, c, 600, each=lambda: pt.setTargetPower(10 * 240))
    assert (
        amps(requests_mock) == [4, 4, 32] and s._evseTargetAmps == 10
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
    assert (
        paid(requests_mock)[1] == 0 and paid(requests_mock)[2] <= 2
    )  # never a wake; at most 2 commands for the desire
    assert commands(requests_mock) == ["set_charge_limit"] * 2


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


# Row 21 (slim Task 12b M3): chargeRateControl 2, a rate Fleet cannot deliver


def test_mode_2_undeliverable_rate_pays_twice(requests_mock):
    m, r, v, c, pt, s = fleetPoint(requests_mock, 2, script=[(500, None, None)])
    pt.setTargetPower(10 * 240)
    passes(r, c, 3600, each=lambda: pt.setTargetPower(10 * 240))
    assert (
        amps(requests_mock) == [10, 10] and s._evseTargetAmps == 10
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
            heartbeat(m, s, 10.0)
            offers.append(s.lastAmpsOffered)

        passes(r, c, 3600, each=cycle)
        if confirmed:
            assert amps(requests_mock) == [10] and offers[-1] == s.wiringMaxAmps
        else:
            assert (
                amps(requests_mock) == [10, 10] and max(offers[1:]) <= 10
            )  # the paid bound holds; the TWC carries the rate (the wiring rule)


# Row 23 (slim Task 10 M2): a spent re-arm lets the start go without the limit


def test_a_spent_rearm_lets_the_start_go_without_the_limit(requests_mock):
    m, r, v, c, api = install(fleet=True)
    car = FleetCar(requests_mock, state="asleep", limit=90, wakes=False)
    for field, value in (
        ("chargeLimit", 90),
        ("chargingState", "Stopped"),
        ("batteryLevel", 40),
    ):
        v.observe(field, value, "TeslaMateVehicle")
    m.setPolicyLimit(50, transition=True)  # a lower: never a wake; asleep, so 408
    passes(r, c, 600)
    c.t += 1
    v.observe(
        "chargeLimit", 90, "FleetTelemetryMQTT"
    )  # a reading that reached the car: the one re-arm
    passes(r, c, 1200)
    assert (
        commands(requests_mock) == ["set_charge_limit"] * 4
    )  # one send, its blind retry, then the re-arm's two: all 408
    assert m.desired.record(VIN, CHARGE_LIMIT).rearmed
    car.wakes = True
    m.startCarsCharging(VIN)
    passes(r, c, 600, each=lambda: m.startCarsCharging(VIN))
    assert commands(requests_mock) == ["set_charge_limit"] * 4 + [
        "charge_start"
    ]  # no fifth limit send rides the start's wake
    assert paid(requests_mock) == (1, 1, 5) and car.charge["charge_limit_soc"] == 90


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


# Row 25 (slim Task 12b _anonCars): a VIN-less Gen2 in mode 2 sets the rate


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
        r, c, 600, each=lambda: heartbeat(m, s, 0.0, state=0x03)
    )  # plugged in, asleep, no VIN
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
