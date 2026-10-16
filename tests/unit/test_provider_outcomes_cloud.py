import pytest
# tests/unit/test_provider_outcomes_cloud.py
import re
from unittest.mock import Mock
import requests_mock as rm
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from tests.unit.test_teslaapi_registry import api_with_registry, VIN
from tests.unit.teslatokens import fleet_token, owner_token
from tests.unit.test_homeassistant_registry import make_ha

@pytest.fixture(autouse=True)
def awake_car(request):
    # The Fleet stop asks the free online check first (Task 12a N2): the car is online
    if "requests_mock" in request.fixturenames:
        request.getfixturevalue("requests_mock").get(re.compile(r".*/api/1/vehicles/5YJ3E1EA1KF000001$"), json={"response": {"state": "online"}})

def fleet(requests_mock, status=200, result=True, reason=""):
    api, reg = api_with_registry(); api.addVehicle({"id": 1, "vin": VIN, "display_name": "C"}); api.storeToken(fleet_token())
    requests_mock.post(re.compile(r".*/command/.*"), status_code=status, json={"response": {"result": result, "reason": reason}})
    return api, reg, api.providers()[1]

def calls(requests_mock, part): return [r for r in requests_mock.request_history if part in r.url]

def test_fleet_command_outcomes_and_one_request_each(requests_mock):
    api, reg, p = fleet(requests_mock); assert p.setChargeLimit(VIN, 80).kind == O.DONE and len(calls(requests_mock, "set_charge_limit")) == 1
    assert calls(requests_mock, "set_charge_limit")[0].json() == {"percent": 80}
    api, reg, p = fleet(requests_mock, result=False, reason="already_set"); assert p.setChargeLimit(VIN, 80) == O.Outcome(O.SATISFIED, "already_set")
    api, reg, p = fleet(requests_mock, result=False, reason="could_not_wake_buses"); assert p.startCharging(VIN).kind == O.UNREACHABLE and len(calls(requests_mock, "charge_start")) == 1
    api, reg, p = fleet(requests_mock, status=500); assert p.stopCharging(VIN) == O.Outcome(O.ERROR, "500")
    assert p.setChargeLimit("5YJ3E1EA1KF000009", 80) == O.Outcome(O.UNSUPPORTED, "not listed")

def test_fleet_401_retires_the_slot_and_403_refuses_the_scope(requests_mock):
    api, reg, p = fleet(requests_mock, status=401); assert p.setAmps(VIN, 10).kind == O.REFUSED
    assert api.fleetSlot.empty() and p.ready(reg.get(VIN), C.SET_AMPS) is False and len(calls(requests_mock, "set_charging_amps")) == 1
    api, reg, p = fleet(requests_mock, status=403); assert p.setAmps(VIN, 10).kind == O.REFUSED and not api.fleetSlot.usable("command") and api.fleetSlot.usable("read")
    assert p.ready(reg.get(VIN), C.SET_AMPS) is False; api.storeToken(fleet_token()); assert api.fleetSlot.usable("command")     # a new token clears the refusal

def test_start_on_a_disconnected_car_is_unsupported(requests_mock):
    api, reg, p = fleet(requests_mock, result=False, reason="disconnected"); assert p.startCharging(VIN) == O.Outcome(O.UNSUPPORTED, "disconnected")

def test_owner_412_is_latched_until_the_token_changes(requests_mock):
    api, reg = api_with_registry(); api.storeToken(owner_token())
    requests_mock.get(re.compile(r".*/api/1/(vehicles|products).*"), status_code=412, json={})
    assert api.loadVehicleList() is False and api.ownerSlot.usable("read") is False
    api.ownerSlot.pausedUntil -= 7200; assert api.ownerSlot.usable("read") is False             # an hour later: still paused
    api.storeToken(owner_token()); assert api.ownerSlot.usable("read") is True

def test_low_amps_are_sent_twice_and_counted(requests_mock, monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as T
    api, reg, p = fleet(requests_mock); api.master.config = {"config": {}}; monkeypatch.setattr(T.time, "sleep", Mock())
    assert p.setAmps(VIN, 4).kind == O.DONE and len(calls(requests_mock, "set_charging_amps")) == 2
    assert p.setAmps(VIN, 10).kind == O.DONE and len(calls(requests_mock, "set_charging_amps")) == 3

def test_wake_checks_the_free_online_state_first_and_holds_between_wakes(requests_mock, monkeypatch):
    import TWCManager.Vehicle.TeslaAPI as T
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(1000.0); api, reg, p = fleet(requests_mock); car = api.registry.get(VIN).attachment("TeslaAPI"); p.clock = clk
    monkeypatch.setattr(T.time, "sleep", Mock()); api.now = clk                                                    # TeslaAPI's own clock hook follows the test clock; time.time itself is untouched
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "online"}})
    assert p.wake(VIN) == O.Outcome(O.SATISFIED, "online") and calls(requests_mock, "wake_up") == [] and len(calls(requests_mock, "/vehicles/" + VIN)) == 1   # free check, no $0.02
    assert p.wake(VIN) == O.Outcome(O.SATISFIED, "online") and len(calls(requests_mock, "/vehicles/" + VIN)) == 1   # 120 s cache: not even the free call
    clk.t += 121; requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    requests_mock.post(re.compile(r".*/wake_up$"), json={"response": {"state": "asleep"}})
    car.delayNextWakeAttempt = 1800; car.lastAPIAccessTime = clk()
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held") and calls(requests_mock, "wake_up") == [] and len(calls(requests_mock, "/vehicles/" + VIN)) == 2
    car.delayNextWakeAttempt = 0; api.wakeDelayMins = 0
    assert p.wake(VIN).kind == O.UNREACHABLE and len(calls(requests_mock, "wake_up")) == 1 and car.delayNextWakeAttempt == 1800
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held") and len(calls(requests_mock, "wake_up")) == 1   # not twice
    clk.t += 1801; assert p.wake(VIN).kind == O.UNREACHABLE and len(calls(requests_mock, "wake_up")) == 2        # the hold expired on the same clock

def test_ha_outcomes():
    ha, reg = make_ha(); (p,) = ha.providers(); v = reg.getOrCreate(VIN, "t")
    hv = Mock(entity_ids={"charge_switch": "switch.c", "charge_limit_number": None, "charge_current_number": "number.a"}, has_charge_switch=True, has_charge_limit_number=False, has_charge_current_number=True)
    v.attach("HomeAssistant", hv); ha._post_service = Mock(return_value=O.Outcome(O.DONE))
    assert p.stopCharging(VIN).kind == O.DONE and ha._post_service.call_args.args == ("switch", "turn_off", {"entity_id": "switch.c"})
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.UNSUPPORTED, "no charge_limit_number entity")
    assert p.setAmps(VIN, 40).kind == O.DONE and ha._post_service.call_args.args[2]["value"] == 32.0
    assert p.declarations()[C.STOP_CHARGING] == C.Declaration(C.HA_BILLED) and not p.declares(C.WAKE)   # the reconciler's MIN_SEND_GAP spaces it
    ha._post_service = ha.__class__._post_service.__get__(ha); import requests
    ha._session.post = Mock(side_effect=requests.exceptions.Timeout()); assert p.startCharging(VIN).kind == O.UNREACHABLE
    resp = Mock(status_code=403, text="secret-token"); ha._session.post = Mock(return_value=resp); resp.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError(response=resp))
    assert p.startCharging(VIN) == O.Outcome(O.REFUSED, "403")


# Beyond the brief: the paid-call edges

def test_an_expired_token_is_refreshed_and_the_command_sent_once_more(requests_mock):
    from tests.unit.teslatokens import FLEET_REFRESH_URL
    from tests.unit.test_teslaapi_refresh import token_response
    api, reg, p = fleet(requests_mock); api.storeToken(fleet_token(), "refresh-me")
    requests_mock.post(re.compile(r".*/command/.*"), [{"status_code": 401, "text": "token expired"}, {"json": {"response": {"result": True}}}])
    refresh = requests_mock.post(FLEET_REFRESH_URL, json=token_response(fleet_token(jti="new")))
    assert p.stopCharging(VIN).kind == O.DONE and len(calls(requests_mock, "charge_stop")) == 2 and refresh.call_count == 1
    requests_mock.post(re.compile(r".*/command/.*"), status_code=401, text="token expired")
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "401") and len(calls(requests_mock, "charge_stop")) == 4 and refresh.call_count == 2
    assert api.fleetSlot.empty()                                                   # refreshed once, refused again: retired

def test_a_401_for_a_token_replaced_meanwhile_sends_the_new_one_once(requests_mock):
    # Another thread pasted a token while the command was out: the 401 was for
    # the old one, which must not clear the new one
    api, reg, p = fleet(requests_mock); pasted = fleet_token(jti="pasted")
    def replacedThen401(request, context):
        api.storeToken(pasted, "", "pasted"); context.status_code = 401; return "invalid bearer token"
    requests_mock.post(re.compile(r".*/command/.*"), [{"text": replacedThen401}, {"json": {"response": {"result": True}}}])
    assert p.stopCharging(VIN).kind == O.DONE and api.fleetSlot.accessToken == pasted
    sent = calls(requests_mock, "charge_stop"); assert len(sent) == 2 and sent[1].headers["Authorization"] == "Bearer " + pasted
    def clearedThen401(request, context):
        api.fleetSlot.clear(); context.status_code = 401; return "invalid bearer token"
    requests_mock.post(re.compile(r".*/command/.*"), text=clearedThen401)
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "401") and len(calls(requests_mock, "charge_stop")) == 3

def test_backoff_429_and_412_send_nothing_more(requests_mock):
    api, reg, p = fleet(requests_mock, status=429); car = api.registry.get(VIN).attachment("TeslaAPI")
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.ERROR, "429")
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.UNREACHABLE, "backoff") and len(calls(requests_mock, "set_charge_limit")) == 1
    api, reg, p = fleet(requests_mock, status=412)
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "412") and not api.fleetSlot.usable("command") and api.fleetSlot.usable("read")

def test_no_wake_without_a_free_answer(requests_mock):
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(1000.0); api, reg, p = fleet(requests_mock); p.clock = clk; api.now = clk; api.wakeDelayMins = 0
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), status_code=503)
    wake = requests_mock.post(re.compile(r".*/wake_up$"), json={"response": {"state": "online"}})
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "online check failed") and wake.call_count == 0

def test_a_failed_wake_request_is_held_like_an_asleep_car(requests_mock):
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(1000.0); api, reg, p = fleet(requests_mock); p.clock = clk; api.now = clk; api.wakeDelayMins = 0
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    wake = requests_mock.post(re.compile(r".*/wake_up$"), status_code=500)
    assert p.wake(VIN) == O.Outcome(O.ERROR, "500") and wake.call_count == 1
    clk.t += 200; assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held") and wake.call_count == 1

def test_ha_never_logs_the_response_body(caplog):
    import requests
    ha, reg = make_ha(); (p,) = ha.providers(); v = reg.getOrCreate(VIN, "t")
    v.attach("HomeAssistant", Mock(entity_ids={"charge_switch": "switch.c"}))
    for code in (403, 500):
        resp = Mock(status_code=code, text="secret-token"); resp.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError(response=resp))
        ha._session.post = Mock(return_value=resp)
        assert p.startCharging(VIN).kind == (O.REFUSED if code == 403 else O.ERROR)
    assert "secret-token" not in caplog.text and caplog.text.count("check the long-lived token") == 1 and "HTTP 500" in caplog.text


# Fix round 1

def test_a_command_412_retires_commands_and_wakes_until_the_token_changes(requests_mock, monkeypatch):   # I1
    import TWCManager.Vehicle.TeslaAPI as T
    from tests.unit.reconcile_helpers import Clock
    monkeypatch.setattr(T.time, "sleep", Mock())
    clk = Clock(5000.0); api, reg, p = fleet(requests_mock, status=412); api.storeToken(owner_token())   # an Owner slot could answer the free check
    p.clock = clk; api.now = clk; api.wakeDelayMins = 0
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    requests_mock.post(re.compile(r".*/wake_up$"), status_code=412, json={})
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.REFUSED, "412")
    assert api.fleetSlot.usable("read") and api.fleetSlot.pausedUntil == 0              # a command 412 latches no reads
    before = len(requests_mock.request_history)
    for _ in range(40):                                                                   # 40 rounds over 40 minutes
        for call in (lambda: p.setChargeLimit(VIN, 80), lambda: p.startCharging(VIN), lambda: p.stopCharging(VIN), lambda: p.setAmps(VIN, 4), lambda: p.wake(VIN)):
            assert call() == O.Outcome(O.REFUSED, "no command token")
        clk.t += 60
    assert len(requests_mock.request_history) == before                                   # nothing more, not even the free check
    assert p.ready(reg.get(VIN), C.SET_AMPS) is False and api.readOnly
    api.storeToken(fleet_token(jti="new")); assert api.fleetSlot.usable("command")

def test_a_wake_412_retires_wakes_too(requests_mock):   # I1
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(5000.0); api, reg, p = fleet(requests_mock); api.storeToken(owner_token()); p.clock = clk; api.now = clk; api.wakeDelayMins = 0
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    wake = requests_mock.post(re.compile(r".*/wake_up$"), status_code=412, json={})
    for _ in range(360):                                                                   # an hour in 10 s steps
        p.wake(VIN); clk.t += 10
    assert wake.call_count == 1 and not api.fleetSlot.usable("command") and api.fleetSlot.usable("read")

def test_a_read_412_still_latches_reads_only(requests_mock):   # I1
    api, reg = api_with_registry(); api.storeToken(fleet_token())
    requests_mock.get(re.compile(r".*/api/1/vehicles$"), status_code=412, json={})
    assert api.loadVehicleList() is False
    assert api.fleetSlot.usable("read") is False and api.fleetSlot.usable("command") is True

def test_an_unrefreshable_expired_token_is_retired(requests_mock):   # M1
    api, reg, p = fleet(requests_mock); api.fleetSlot.refreshToken = "rt"; api.fleetSlot.clientID = ""
    requests_mock.post(re.compile(r".*/command/.*"), status_code=401, text="token expired")
    for _ in range(3):
        p.stopCharging(VIN)
    assert len(calls(requests_mock, "charge_stop")) == 1 and not api.fleetSlot.usable("command")
    assert api.fleetSlot.refreshToken == "rt" and api.master.tasks == []                 # refused, never cleared or saved

def test_an_ended_need_gets_a_fresh_grace_through_wake_outcome(requests_mock):   # M3/c4
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(5000.0); api, reg, p = fleet(requests_mock); p.clock = clk; api.now = clk      # wakeDelayMins 3
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    wake = requests_mock.post(re.compile(r".*/wake_up$"), json={"response": {"state": "asleep"}})
    car = api.registry.get(VIN).attachment("TeslaAPI")
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held") and car.firstChargeNeededTime == 5000.0
    clk.t += 601                                                                           # nobody asked for 10 minutes: that need ended
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held") and wake.call_count == 0 and car.firstChargeNeededTime == clk()
    clk.t += 120; assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "wake held")                # one continuing need: asked within 600 s
    clk.t += 61; assert p.wake(VIN).kind == O.UNREACHABLE and wake.call_count == 1          # its grace is over

def test_wake_due_reads_the_backoff_on_its_clock(requests_mock):   # M4
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(5000.0); api, reg, p = fleet(requests_mock); car = api.registry.get(VIN).attachment("TeslaAPI")
    car.lastErrorTime = clk() - 10; car.errorCount = 5                                     # a 4-minute backoff, 10 s in
    assert api.wakeDue(car, now=clk()) is False
    assert api.wakeDue(car, now=clk() + 240) is True

def test_no_free_check_during_the_backoff(requests_mock):   # L2
    from tests.unit.reconcile_helpers import Clock
    clk = Clock(5000.0); api, reg, p = fleet(requests_mock); car = api.registry.get(VIN).attachment("TeslaAPI"); p.clock = clk; api.now = clk
    check = requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    car.lastErrorTime = clk() - 10; car.errorCount = 5
    assert p.wake(VIN) == O.Outcome(O.UNREACHABLE, "backoff") and check.call_count == 0

def test_a_cached_failed_check_stays_unknown(requests_mock):   # L1
    from tests.unit.reconcile_helpers import Clock
    import time
    from TWCManager.Vehicle.Registry.Vehicle import Vehicle
    clk = Clock(5000.0); api, reg, p = fleet(requests_mock); v = reg.get(VIN); api.now = clk
    check = requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), status_code=503)
    assert api.is_awake(v, now=clk()) is False and api.onlineState(v, now=clk()) is None
    v.observe("state", "online", "TeslaMateVehicle"); assert api.onlineState(v, now=clk() + 5) is True   # a telemetry answer
    v._clock = lambda: time.time() + Vehicle.SYNC_TIMEOUT                                   # ... gone stale: the check falls to its cache
    assert api.is_awake(v, now=clk() + 10) is False and api.onlineState(v, now=clk() + 10) is None    # the cache hit keeps its own unknown
    assert check.call_count == 1

def test_odd_reasons_are_not_carried(requests_mock):   # L3
    api, reg, p = fleet(requests_mock, result=False, reason="x" * 300)
    assert p.stopCharging(VIN) == O.Outcome(O.ERROR, "not accepted")
    api, reg, p = fleet(requests_mock, result=False, reason="Some Text")
    assert p.stopCharging(VIN) == O.Outcome(O.ERROR, "not accepted")

def test_ha_http_error_without_a_response(requests_mock):   # L4
    import requests
    ha, reg = make_ha()
    resp = Mock(); resp.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError()); ha._session.post = Mock(return_value=resp)
    assert ha._post_service("switch", "turn_on", {}) == O.Outcome(O.ERROR, "HTTPError")


# Fix round 2: a refresh that fails transiently never deletes the tokens (N1)

def _expiring(requests_mock):
    from tests.unit.teslatokens import FLEET_REFRESH_URL
    api, reg, p = fleet(requests_mock); api.storeToken(fleet_token(), "refresh-me")
    requests_mock.post(re.compile(r".*/command/.*"), status_code=401, text="token expired")
    return api, reg, p, FLEET_REFRESH_URL

def test_a_503_refresh_keeps_the_tokens_and_stops_paying(requests_mock):
    api, reg, p, url = _expiring(requests_mock); access = api.fleetSlot.accessToken
    refresh = requests_mock.post(url, status_code=503)
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "401")
    assert api.fleetSlot.refreshToken == "refresh-me" and api.fleetSlot.accessToken == access
    for _ in range(3):
        assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "no command token") and p.wake(VIN).kind == O.REFUSED
    assert len(calls(requests_mock, "charge_stop")) == 1 and calls(requests_mock, "wake_up") == [] and refresh.call_count == 1

def test_a_later_successful_refresh_restores_commands(requests_mock):
    from tests.unit.test_teslaapi_refresh import token_response
    api, reg, p, url = _expiring(requests_mock)
    requests_mock.post(url, status_code=502); p.stopCharging(VIN); assert not api.fleetSlot.usable("command")
    requests_mock.post(url, json=token_response(fleet_token(jti="new")))
    assert api.refreshSlot(api.fleetSlot) == "success" and api.fleetSlot.usable("command")       # the slot's own refresh cycle
    requests_mock.post(re.compile(r".*/command/.*"), json={"response": {"result": True}})
    assert p.stopCharging(VIN).kind == O.DONE

def test_an_invalid_grant_clears_the_slot_as_base_did(requests_mock):
    api, reg, p, url = _expiring(requests_mock)
    requests_mock.post(url, status_code=401, json={"error": "invalid_grant"})              # refresh() itself clears on a 401
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "401") and api.fleetSlot.empty() and api.fleetSlot.refreshToken == ""
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "no command token") and len(calls(requests_mock, "charge_stop")) == 1
    api, reg, p, url = _expiring(requests_mock)
    requests_mock.post(url, status_code=400, json={"error": "invalid_grant"})              # base kept the slot on a 400
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "401") and api.fleetSlot.refreshToken == "refresh-me"


# 12b fix round 1: deleted-test GAPs and PARTIALs

def asleep(requests_mock, monkeypatch, api):
    # The free online check says asleep and no wake is held: a wake_up goes out
    import TWCManager.Vehicle.TeslaAPI as T
    monkeypatch.setattr(T.time, "sleep", Mock()); api.wakeDelayMins = 0
    requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), json={"response": {"state": "asleep"}})
    car = api.registry.get(VIN).attachment("TeslaAPI"); car.delayNextWakeAttempt = 0; return car

def test_paid_posts_time_out_after_ten_seconds(requests_mock, monkeypatch):   # GAP: test_post_has_timeout
    api, reg, p = fleet(requests_mock); asleep(requests_mock, monkeypatch, api)
    requests_mock.post(re.compile(r".*/wake_up$"), json={"response": {"state": "online"}})
    p.setAmps(VIN, 10); p.wake(VIN)
    assert [r.timeout for r in calls(requests_mock, "set_charging_amps") + calls(requests_mock, "wake_up")] == [10, 10]

@pytest.mark.parametrize("path", ["wake_up", "command/set_charging_amps"])
def test_a_wake_or_command_that_never_left_is_an_error_and_refreshes_nothing(requests_mock, monkeypatch, path):   # GAPs: wake never sent
    import requests
    from tests.unit.teslatokens import FLEET_REFRESH_URL
    api, reg, p = fleet(requests_mock); api.storeToken(fleet_token(), "refresh-me"); asleep(requests_mock, monkeypatch, api)   # a refresh would really go out
    requests_mock.post(re.compile(r".*/%s$" % path), exc=requests.exceptions.ConnectionError)
    refresh = requests_mock.post(FLEET_REFRESH_URL, json={})
    outcome = p.wake(VIN) if path == "wake_up" else p.setAmps(VIN, 10)
    assert outcome == O.Outcome(O.ERROR, "ConnectionError") and not refresh.called and not api.fleetSlot.empty()

def test_a_low_rate_is_done_only_when_both_sends_land(requests_mock, monkeypatch):   # PARTIALs: low rate resend fails / rejected
    import TWCManager.Vehicle.TeslaAPI as T
    monkeypatch.setattr(T.time, "sleep", Mock())
    ok, bad = {"json": {"response": {"result": True}}}, {"json": {"response": {"result": False, "reason": "nope"}}}
    for responses, kind, sent in (([ok, {"status_code": 500, "json": {}}], O.ERROR, 2), ([ok, bad], O.ERROR, 2), ([bad, ok], O.ERROR, 1)):
        api, reg, p = fleet(requests_mock); api.master.config = {"config": {}}
        requests_mock.post(re.compile(r".*/command/.*"), responses)
        before = len(calls(requests_mock, "set_charging_amps"))
        assert p.setAmps(VIN, 4).kind == kind and len(calls(requests_mock, "set_charging_amps")) - before == sent   # no second send after an unsettled first

def test_an_expired_token_on_wake_is_refreshed_at_fleet_auth_once(requests_mock, monkeypatch):   # PARTIAL: wake 401
    from tests.unit.teslatokens import FLEET_REFRESH_URL
    from tests.unit.test_teslaapi_refresh import AUTH_TOKEN_URL, token_response
    api, reg, p = fleet(requests_mock); api.storeToken(fleet_token(), "refresh-me"); asleep(requests_mock, monkeypatch, api)
    requests_mock.post(re.compile(r".*/wake_up$"), [{"status_code": 401, "text": "token expired"}, {"json": {"response": {"state": "online"}}}])
    fleetAuth = requests_mock.post(FLEET_REFRESH_URL, json=token_response(fleet_token(jti="new"))); owner = requests_mock.post(AUTH_TOKEN_URL, json={})
    assert p.wake(VIN).kind in O.SETTLED and len(calls(requests_mock, "wake_up")) == 2 and fleetAuth.call_count == 1 and owner.call_count == 0

def test_a_failed_online_check_still_sends_the_stop(requests_mock):   # m2: no answer is not asleep
    api, reg, p = fleet(requests_mock)
    check = requests_mock.get(re.compile(r".*/api/1/vehicles/%s$" % VIN), status_code=500, json={})
    assert p.stopCharging(VIN).kind == O.DONE and check.call_count == 1 and len(calls(requests_mock, "charge_stop")) == 1
