import time

import jwt
import pytest
import requests

from TWCManager.Vehicle import TeslaTokenSlot as slotmod
from TWCManager.Vehicle.TeslaTokenSlot import TeslaTokenSlot
from tests.unit.teslatokens import (
    AUTH_TOKEN_URL,
    FLEET_REFRESH_URL,
    OWNER_AUD,
    fleet_token,
    make_jwt,
    owner_token,
)

OWNER_URL = "https://owner-api.teslamotors.com"
FLEET_NA_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com"


def classify(token):
    aud = (slotmod.decodeToken(token) or {}).get("aud") or []
    if any("owner-api" in a for a in aud):
        return ("owner", OWNER_URL, True)
    if any("fleet-api" in a for a in aud):
        return ("fleet", FLEET_NA_URL, True)
    return ("unknown", "", True)


def owner_slot():
    return TeslaTokenSlot("owner", classify, AUTH_TOKEN_URL, "ownerapi", requests.post)


def fleet_slot(clientID="my-client-id"):
    return TeslaTokenSlot("fleet", classify, FLEET_REFRESH_URL, clientID, requests.post)


def decode_exp(token):
    return jwt.decode(token, options={"verify_signature": False})["exp"]


def test_synced_token_drops_refresh_and_uses_exp():
    s = owner_slot()
    tok = owner_token(expires_in=3600)
    s.setTokens(tok, "teslamate-refresh", 99999 * 99999 * 99999, "synced")
    assert s.refreshToken == "" and s.expireTime == decode_exp(tok)


def test_synced_slot_is_empty_after_exp():
    s = owner_slot()
    s.setTokens(owner_token(expires_in=-1), source="synced")
    assert s.empty() is True and s.usable("read") is False


def test_synced_slot_never_refreshes(requests_mock):
    hit = requests_mock.post(AUTH_TOKEN_URL, json={})
    s = owner_slot()
    s.setTokens(owner_token(expires_in=60), source="synced")
    assert s.needsRefresh() is False
    assert s.refresh() == "not_configured" and hit.called is False


def test_expired_pasted_token_stays_usable():
    s = fleet_slot()
    s.setTokens(fleet_token(expires_in=-60), "r")
    assert s.usable("command") is True and s.needsRefresh() is True


def test_usable_requires_matching_type():
    s = fleet_slot()
    s.setTokens(owner_token())
    assert s.usable("read") is False and s.usable("command") is False


def test_pause_blocks_reads_not_commands_for_an_hour():
    s = fleet_slot()
    s.setTokens(fleet_token(expires_in=7200))
    now = time.time()
    s.pause(now)
    assert s.usable("read", now + 3599) is False
    assert s.usable("command", now) is True
    assert s.usable("read", now + 3600) is True


def test_new_token_ends_pause():
    s = owner_slot()
    s.setTokens(owner_token(), source="synced")
    s.pause()
    s.setTokens(owner_token(jti="new"), source="synced")
    assert s.usable("read") is True


def test_access_token_alone_keeps_refresh_token():
    s = owner_slot()
    s.setTokens(owner_token(), "r1")
    s.setTokens(owner_token(jti="2"))
    assert s.refreshToken == "r1"


def test_empty_refresh_token_clears_it():
    s = owner_slot()
    s.setTokens(owner_token(), "r1")
    s.setTokens(owner_token(jti="2"), "")
    assert s.refreshToken == ""


def test_empty_access_token_is_not_classified():
    s = owner_slot()
    s.setTokens(owner_token(), "r1")
    s.setTokens("", "r1")
    assert s.tokenType == "" and s.empty() is True


def test_jwt_without_exp_gets_default_lifetime():
    s = owner_slot()
    before = time.time()
    s.setTokens(make_jwt(aud=OWNER_AUD))  # no exp claim
    assert before + 8 * 3600 <= s.expireTime <= time.time() + 8 * 3600


def test_changes_counts_every_mutation():
    s = owner_slot()
    s.setTokens(owner_token())
    s.clear()
    s.restore(s.snapshot())
    assert s.changes == 3


def test_fleet_token_missing_scope_not_usable_and_logs_once(caplog):
    s = fleet_slot()
    s.setTokens(fleet_token(scp=["openid", "vehicle_device_data"]))
    assert s.usable("read") is True
    assert s.usable("command") is False and s.usable("command") is False
    assert caplog.text.count("vehicle_charging_cmds") == 1
    assert s.usable("energy") is False and "energy_device_data" in caplog.text


def test_either_command_scope_suffices():
    s = fleet_slot()
    s.setTokens(fleet_token(scp=["vehicle_cmds"]))
    assert s.missingScope("command") is None


def test_token_without_scp_has_every_scope():
    s = fleet_slot()
    s.setTokens(fleet_token(scp=None))
    assert s.usable("read") and s.usable("command") and s.usable("energy")


def test_owner_token_scope_not_checked():
    s = owner_slot()
    s.setTokens(owner_token(scp=["openid"]))
    assert s.usable("read") and s.usable("energy")


def test_new_token_reevaluates_scope(caplog):
    s = fleet_slot()
    s.setTokens(fleet_token(scp=["vehicle_device_data"]))
    assert s.usable("command") is False
    s.setTokens(fleet_token(scp=["vehicle_charging_cmds"], jti="2"))
    assert s.usable("command") is True
    s.setTokens(fleet_token(scp=["vehicle_device_data"], jti="3"))
    assert s.usable("command") is False
    assert caplog.text.count("vehicle_charging_cmds") == 2


def test_owner_refresh_request(requests_mock):
    new = owner_token(jti="new")
    hit = requests_mock.post(
        AUTH_TOKEN_URL,
        json={"access_token": new, "refresh_token": "r2", "expires_in": 28800},
    )
    s = owner_slot()
    s.setTokens(owner_token(), "r1", source="login")
    now = time.time()
    assert s.refresh(now) == "success"
    assert hit.last_request.json() == {
        "client_id": "ownerapi",
        "grant_type": "refresh_token",
        "refresh_token": "r1",
        "scope": "offline_access",
    }
    assert s.accessToken == new and s.refreshToken == "r2"
    assert s.expireTime == now + 28800


def test_refresh_without_refresh_token_makes_no_request(requests_mock):
    hit = requests_mock.post(AUTH_TOKEN_URL, json={})
    s = owner_slot()
    s.setTokens(owner_token())
    assert s.refresh() == "not_configured" and hit.called is False


def test_refresh_401_clears_slot(requests_mock):
    requests_mock.post(AUTH_TOKEN_URL, status_code=401)
    s = owner_slot()
    s.setTokens(owner_token(), "r1")
    changes = s.changes
    assert s.refresh() == "rejected"
    assert s.accessToken == "" and s.refreshToken == "" and s.changes > changes


def test_refresh_other_http_error_keeps_tokens(requests_mock):
    requests_mock.post(AUTH_TOKEN_URL, status_code=500)
    s = owner_slot()
    tok = owner_token()
    s.setTokens(tok, "r1")
    assert s.refresh() == "rejected"
    assert s.accessToken == tok and s.refreshToken == "r1"


def test_refresh_bad_body_is_rejected(requests_mock):
    requests_mock.post(AUTH_TOKEN_URL, text="not json")
    s = owner_slot()
    s.setTokens(owner_token(), "r1")
    assert s.refresh() == "rejected" and s.refreshToken == "r1"


def test_refresh_unreachable_keeps_tokens(requests_mock):
    requests_mock.post(AUTH_TOKEN_URL, exc=requests.exceptions.ConnectionError)
    s = owner_slot()
    tok = owner_token()
    s.setTokens(tok, "r1")
    assert s.refresh() == "unreachable"
    assert s.accessToken == tok and s.refreshToken == "r1"


def test_fleet_refresh_without_client_id_is_not_configured(requests_mock, caplog):
    hit = requests_mock.post(FLEET_REFRESH_URL, json={})
    s = fleet_slot(clientID="")
    s.setTokens(fleet_token(), "r1")
    assert s.refresh() == "not_configured" and hit.called is False
    assert "no teslaApiClientID configured" in caplog.text


def test_refresh_success_keeps_source(requests_mock):
    requests_mock.post(
        FLEET_REFRESH_URL,
        json={
            "access_token": fleet_token(jti="n"),
            "refresh_token": "r2",
            "expires_in": 100,
        },
    )
    s = fleet_slot()
    s.setTokens(fleet_token(), "r1", source="login")
    assert s.refresh() == "success" and s.source == "login"


def test_refresh_logs_no_token(requests_mock, caplog):
    caplog.set_level(1)
    new = fleet_token(jti="brand-new")
    requests_mock.post(
        FLEET_REFRESH_URL,
        json={"access_token": new, "refresh_token": "r2-secret", "expires_in": 100},
    )
    s = fleet_slot()
    s.setTokens(fleet_token(), "r1-secret")
    assert s.refresh() == "success"
    assert new not in caplog.text and "r2-secret" not in caplog.text
    assert "r1-secret" not in caplog.text


def test_snapshot_restore_round_trip():
    s = owner_slot()
    tok = owner_token()
    s.setTokens(tok, "r1", source="login")
    s.pause()
    snap = s.snapshot()
    s.clear()
    s.restore(snap)
    assert s.snapshot() == snap and s.accessToken == tok


def test_status_of_empty_and_filled_slot():
    s = owner_slot()
    assert s.status() == {
        "holdsToken": False,
        "tokenType": "",
        "source": "",
        "expires": "",
        "paused": False,
    }
    tok = owner_token()
    s.setTokens(tok, source="login")
    s.pause()
    st = s.status()
    assert st["holdsToken"] is True and st["tokenType"] == "owner"
    assert st["source"] == "login" and st["paused"] is True
    assert st["expires"] == time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(decode_exp(tok))
    )


def test_owner_slot_never_grants_commands_and_logs_nothing(caplog):
    s = owner_slot()
    s.setTokens(owner_token())
    assert s.grants("read") and s.grants("energy") and not s.grants("command")
    assert s.usable("command") is False and "scope" not in caplog.text


def test_nested_scp_claim_is_treated_as_no_claim():
    from tests.unit.teslatokens import make_jwt

    slot = owner_slot()
    slot.setTokens(make_jwt(scp=[["a"], "b"], exp=4102444800), None, None, "pasted")
    assert slot.scopes is None
    slot.setTokens(make_jwt(scp=["a", "b"], exp=4102444800), None, None, "pasted")
    assert slot.scopes == frozenset({"a", "b"})


def test_refresh_post_has_timeout(requests_mock):
    hit = requests_mock.post(
        AUTH_TOKEN_URL,
        json={
            "access_token": owner_token(jti="n"),
            "refresh_token": "r2",
            "expires_in": 1,
        },
    )
    s = owner_slot()
    s.setTokens(owner_token(), "r1", source="login")
    assert s.refresh() == "success"
    assert hit.last_request.timeout == slotmod.REFRESH_TIMEOUT == 30


def test_refresh_timeout_is_unreachable_and_keeps_tokens(requests_mock):
    requests_mock.post(FLEET_REFRESH_URL, exc=requests.exceptions.ConnectTimeout)
    s = fleet_slot()
    access = fleet_token()
    s.setTokens(access, "r1", source="login")
    assert s.refresh() == "unreachable"
    assert s.accessToken == access and s.refreshToken == "r1"
