# tests/unit/test_provider_outcomes_cloud.py
import re
from unittest.mock import Mock
import requests_mock as rm
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Reconcile import Outcome as O
from tests.unit.test_carapivehicle_registry import api_with_registry, VIN
from tests.unit.teslatokens import fleet_token, owner_token
from tests.unit.test_homeassistant_registry import make_ha

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
    clk = Clock(1000.0); api, reg, p = fleet(requests_mock); car = api.getCarApiVehicles()[0]; p.clock = clk
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

def test_ha_outcomes_and_per_car_rate_limit():
    ha, reg = make_ha(); (p,) = ha.providers(); v = reg.getOrCreate(VIN, "t")
    hv = Mock(entity_ids={"charge_switch": "switch.c", "charge_limit_number": None, "charge_current_number": "number.a"}, has_charge_switch=True, has_charge_limit_number=False, has_charge_current_number=True)
    v.attach("HomeAssistant", hv); ha._post_service = Mock(return_value=O.Outcome(O.DONE))
    assert p.stopCharging(VIN).kind == O.DONE and ha._post_service.call_args.args == ("switch", "turn_off", {"entity_id": "switch.c"})
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.UNSUPPORTED, "no charge_limit_number entity")
    assert p.setAmps(VIN, 40).kind == O.DONE and ha._post_service.call_args.args[2]["value"] == 32.0
    assert p.declarations()[C.STOP_CHARGING].rateLimit == C.RateLimit(1, 10) and not p.declares(C.WAKE)
    ha._post_service = ha.__class__._post_service.__get__(ha); import requests
    ha._session.post = Mock(side_effect=requests.exceptions.Timeout()); assert p.startCharging(VIN).kind == O.UNREACHABLE
    resp = Mock(status_code=403, text="secret-token"); ha._session.post = Mock(return_value=resp); resp.raise_for_status = Mock(side_effect=requests.exceptions.HTTPError(response=resp))
    assert p.startCharging(VIN) == O.Outcome(O.REFUSED, "403") and ha._call_service("switch", "turn_on", {}) is False


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

def test_backoff_429_and_412_send_nothing_more(requests_mock):
    api, reg, p = fleet(requests_mock, status=429); car = api.getCarApiVehicles()[0]
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.ERROR, "429")
    assert p.setChargeLimit(VIN, 80) == O.Outcome(O.UNREACHABLE, "backoff") and len(calls(requests_mock, "set_charge_limit")) == 1
    api, reg, p = fleet(requests_mock, status=412)
    assert p.stopCharging(VIN) == O.Outcome(O.REFUSED, "412") and api.fleetSlot.pausedUntil == float("inf")

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
