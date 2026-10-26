"""
Saved token sources and the one-time migration of a saved Owner API token
from the carApi* keys (where earlier releases kept their only token) to the
Owner slot's ownerApi* keys. TWCMaster.loadSettings migrates and restores
both slots whether or not TeslaMate token sync is on, and must never leave
TeslaMate's refresh token where TWCManager could spend it.
"""

import json
import logging
import time

import pytest

from tests.unit.teslatokens import fleet_token, make_jwt, new_api, owner_token
from tests.unit.test_teslamate_token_sync import (  # noqa: F401 (fixtures)
    exp,
    fake_db,
    make_teslamate,
    master_with_settings,
    refresh_endpoints,
)

SYNCED_EXPIRY = 99999 * 99999 * 99999


def unknown_token():
    return make_jwt(aud="https://example.com/", exp=int(time.time() + 3600))


def owner_refresh_token():
    return make_jwt(iss="https://auth.tesla.com/oauth2/v3", aud="ownerapi")


def load(tmp_path, settings, syncTokensConfigured=False):
    master, api, modules = master_with_settings(
        tmp_path, settings, syncTokensConfigured
    )
    master.loadSettings()
    return master, api


def start_with_sync(tmp_path, settings, fake_db, synced):
    """Startup order of TWCManager.py: TeslaMateVehicle's first sync runs
    from its __init__ before it is registered, then loadSettings."""
    master, api, modules = master_with_settings(
        tmp_path, settings, syncTokensConfigured=True
    )
    tm = make_teslamate(master, synced, registered=False)
    fake_db["tm"] = tm
    tm.doSyncTokens(True)
    modules["TeslaMateVehicle"] = tm
    assert master.tokenSyncEnabled() is True
    master.loadSettings()
    return master, api


def test_owner_token_moves_to_owner_slot(tmp_path):
    token = owner_token()
    saved = int(time.time()) + 3 * 60 * 60
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "r1",
            "carApiTokenExpireTime": saved,
        },
    )
    assert api.ownerSlot.accessToken == token
    assert api.ownerSlot.refreshToken == "r1"
    assert api.ownerSlot.expireTime == saved
    assert api.ownerSlot.source == "pasted"
    assert api.fleetSlot.empty()
    assert master.settings["carApiBearerToken"] == ""
    assert master.settings["carApiRefreshToken"] == ""
    assert master.settings["carApiTokenExpireTime"] == 0
    assert master.settings["carApiTokenSource"] == ""
    assert master.settings["ownerApiTokenSource"] == "pasted"


def test_fleet_token_stays(tmp_path):
    token = fleet_token()
    owner = owner_token(jti="kept")
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "fleet-refresh",
            "carApiTokenExpireTime": int(time.time()) + 3600,
            "ownerApiBearerToken": owner,
            "ownerApiRefreshToken": "owner-refresh",
            "ownerApiTokenExpireTime": int(time.time()) + 3600,
        },
    )
    assert api.fleetSlot.accessToken == token
    assert api.fleetSlot.refreshToken == "fleet-refresh"
    assert api.fleetSlot.source == "pasted"
    assert master.settings["carApiTokenSource"] == "pasted"
    assert master.settings["ownerApiBearerToken"] == owner
    assert master.settings["ownerApiRefreshToken"] == "owner-refresh"
    assert api.ownerSlot.accessToken == owner


def test_unknown_token_stays_with_error(tmp_path, caplog):
    token = unknown_token()
    with caplog.at_level(logging.ERROR):
        master, api = load(
            tmp_path,
            {
                "carApiBearerToken": token,
                "carApiRefreshToken": "",
                "carApiTokenExpireTime": int(time.time()) + 3600,
            },
        )
    assert api.fleetSlot.accessToken == token
    assert api.fleetSlot.tokenType == "unknown"
    assert api.ownerSlot.empty()
    assert any(
        r.levelno == logging.ERROR and "token type" in r.getMessage()
        for r in caplog.records
    )
    assert token not in caplog.text


@pytest.mark.parametrize("make_token", [fleet_token, unknown_token])
def test_synced_fleet_or_unknown_token_drops_refresh(tmp_path, make_token):
    token = make_token()
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": SYNCED_EXPIRY,
        },
    )
    assert api.fleetSlot.accessToken == token
    assert api.fleetSlot.refreshToken == ""
    assert api.fleetSlot.source == "synced"
    assert api.fleetSlot.expireTime == exp(token)
    assert master.settings["carApiRefreshToken"] == ""
    assert master.settings["carApiTokenSource"] == "synced"


def test_refresh_only_owner_routed_by_refresh_type(tmp_path):
    refresh = owner_refresh_token()
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": "",
            "carApiRefreshToken": refresh,
            "carApiTokenExpireTime": 0,
        },
    )
    assert master.settings["ownerApiRefreshToken"] == refresh
    assert master.settings["carApiRefreshToken"] == ""
    assert api.ownerSlot.refreshToken == refresh
    assert api.fleetSlot.refreshToken == ""


def test_migration_is_idempotent():
    api = new_api()
    s = {
        "carApiBearerToken": owner_token(),
        "carApiRefreshToken": "r1",
        "carApiTokenExpireTime": int(time.time()) + 3600,
    }
    assert api.migrateSavedTokens(s, False) is True
    copy = dict(s)
    assert api.migrateSavedTokens(s, False) is False
    assert s == copy


def test_migration_is_idempotent_for_synced_fleet():
    api = new_api()
    s = {
        "carApiBearerToken": fleet_token(),
        "carApiRefreshToken": "teslamate-refresh",
        "carApiTokenExpireTime": SYNCED_EXPIRY,
    }
    assert api.migrateSavedTokens(s, False) is True
    copy = dict(s)
    assert api.migrateSavedTokens(s, False) is False
    assert s == copy


def test_nothing_to_migrate():
    api = new_api()
    s = {"carApiBearerToken": "", "carApiRefreshToken": ""}
    assert api.migrateSavedTokens(s, True) is False
    assert s == {"carApiBearerToken": "", "carApiRefreshToken": ""}
    assert api.migrateSavedTokens({}, True) is False


def test_synced_owner_marked_and_refresh_dropped_when_sync_on(
    tmp_path, fake_db, refresh_endpoints
):
    # The live box: a TeslaMate-synced Owner token in carApi*, with
    # TeslaMate's refresh token and the never-expires sentinel.
    saved = owner_token(jti="saved", expires_in=-3600)
    fresh = owner_token(jti="fresh")
    master, api = start_with_sync(
        tmp_path,
        {
            "carApiBearerToken": saved,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": SYNCED_EXPIRY,
        },
        fake_db,
        fresh,
    )
    assert api.ownerSlot.source == "synced"
    assert api.ownerSlot.refreshToken == ""
    # The saved synced token didn't overwrite the startup sync's.
    assert api.ownerSlot.accessToken == fresh
    assert api.ownerSlot.expireTime == exp(fresh)
    assert api.fleetSlot.empty()
    assert "teslamate-refresh" not in json.dumps(master.settings)

    api.refreshTokenIfNeeded()
    assert all(ep.call_count == 0 for ep in refresh_endpoints)


def test_synced_owner_marked_when_sync_on_without_sentinel(tmp_path, fake_db):
    saved = owner_token(jti="saved")
    fresh = owner_token(jti="fresh")
    master, api = start_with_sync(
        tmp_path,
        {
            "carApiBearerToken": saved,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": int(time.time()) + 3600,
        },
        fake_db,
        fresh,
    )
    assert master.settings["ownerApiTokenSource"] == "synced"
    assert master.settings["ownerApiRefreshToken"] == ""
    assert api.ownerSlot.accessToken == fresh
    assert api.ownerSlot.refreshToken == ""


def test_sentinel_marks_synced_when_sync_off_at_load(tmp_path, refresh_endpoints):
    # Review Focus 1: the same live-box settings, but sync is off at load
    # (no TeslaMate module, or the startup sync couldn't reach its DB).
    # Nearly expired, so a token that kept TeslaMate's refresh token would
    # be refreshed at once.
    token = owner_token(expires_in=600)
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": SYNCED_EXPIRY,
        },
    )
    assert master.tokenSyncEnabled() is False
    assert api.ownerSlot.accessToken == token
    assert api.ownerSlot.source == "synced"
    assert api.ownerSlot.refreshToken == ""
    assert api.ownerSlot.expireTime == exp(token)
    assert api.fleetSlot.empty()
    assert api.fleetSlot.refreshToken == ""

    api.refreshTokenIfNeeded()
    assert all(ep.call_count == 0 for ep in refresh_endpoints)
    assert "teslamate-refresh" not in json.dumps(master.settings)


def test_owner_token_back_in_carapi_after_downgrade_migrates_again(
    tmp_path, refresh_endpoints
):
    # Migrated once (carApiTokenSource saved as ""), then a downgraded
    # release's sync wrote a synced Owner token, TeslaMate's refresh token
    # and the sentinel back into carApi*, keeping the stray source key.
    # The Fleet slot must not end up holding TeslaMate's refresh token.
    token = owner_token(expires_in=600)
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": SYNCED_EXPIRY,
            "carApiTokenSource": "",
            "ownerApiBearerToken": "",
            "ownerApiRefreshToken": "",
            "ownerApiTokenExpireTime": 0,
            "ownerApiTokenSource": "",
        },
    )
    assert api.fleetSlot.empty()
    assert api.fleetSlot.refreshToken == ""
    assert api.ownerSlot.accessToken == token
    assert api.ownerSlot.source == "synced"
    assert api.ownerSlot.refreshToken == ""

    api.refreshTokenIfNeeded()
    assert all(ep.call_count == 0 for ep in refresh_endpoints)


def test_saved_pasted_token_wins_over_startup_sync(tmp_path, fake_db):
    pasted = owner_token(jti="pasted")
    synced = owner_token(jti="synced")
    master, api = start_with_sync(
        tmp_path,
        {
            "ownerApiBearerToken": pasted,
            "ownerApiRefreshToken": "my-refresh",
            "ownerApiTokenExpireTime": int(time.time()) + 3600,
            "ownerApiTokenSource": "pasted",
            "carApiTokenSource": "",
        },
        fake_db,
        synced,
    )
    assert api.ownerSlot.accessToken == pasted
    assert api.ownerSlot.refreshToken == "my-refresh"
    assert api.ownerSlot.source == "pasted"


def test_owner_key_overwritten_by_newer_carapi_owner_token(tmp_path):
    old = owner_token(jti="old")
    newer = owner_token(jti="newer")
    master, api = load(
        tmp_path,
        {
            "ownerApiBearerToken": old,
            "ownerApiRefreshToken": "old-refresh",
            "ownerApiTokenExpireTime": int(time.time()) + 3600,
            "carApiBearerToken": newer,
            "carApiRefreshToken": "new-refresh",
            "carApiTokenExpireTime": int(time.time()) + 7200,
        },
    )
    assert master.settings["ownerApiBearerToken"] == newer
    assert master.settings["ownerApiRefreshToken"] == "new-refresh"
    assert api.ownerSlot.accessToken == newer
    assert api.ownerSlot.refreshToken == "new-refresh"


def test_sources_round_trip_through_save(tmp_path):
    fleet = fleet_token()
    owner = owner_token()
    master, api = load(tmp_path, {})
    api.fleetSlot.setTokens(fleet, "fleet-refresh", None, "login")
    api.ownerSlot.setTokens(owner, "", None, "synced")
    master.saveSettings()

    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["carApiTokenSource"] == "login"
    assert saved["ownerApiTokenSource"] == "synced"

    master2, api2 = load(tmp_path, saved)
    assert api2.fleetSlot.accessToken == fleet
    assert api2.fleetSlot.source == "login"
    assert api2.ownerSlot.accessToken == owner
    assert api2.ownerSlot.source == "synced"

    # An empty slot saves an empty source.
    api2.fleetSlot.clear()
    master2.saveSettings()
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["carApiTokenSource"] == ""


def test_synced_refresh_only_owner_keeps_saved_owner_token():
    # The sentinel marks it synced, so its refresh token is dropped and
    # nothing is left: the saved Owner token must not be wiped.
    api = new_api()
    owner = owner_token()
    s = {
        "carApiBearerToken": "",
        "carApiRefreshToken": owner_refresh_token(),
        "carApiTokenExpireTime": SYNCED_EXPIRY,
        "ownerApiBearerToken": owner,
        "ownerApiRefreshToken": "owner-refresh",
        "ownerApiTokenExpireTime": 123,
        "ownerApiTokenSource": "pasted",
    }
    assert api.migrateSavedTokens(s, False) is True
    assert s["ownerApiBearerToken"] == owner
    assert s["ownerApiRefreshToken"] == "owner-refresh"
    assert s["ownerApiTokenSource"] == "pasted"
    assert s["carApiRefreshToken"] == ""
    assert s["carApiTokenSource"] == ""


def test_owner_token_synced_when_sync_configured_but_off_at_load(
    tmp_path, refresh_endpoints
):
    # syncTokens is set in config.json, but the startup sync couldn't reach
    # TeslaMate's database and turned sync off. The saved token (an
    # earlier release's, with an ordinary expiry) is still TeslaMate's.
    token = owner_token(expires_in=600)
    master, api, modules = master_with_settings(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": int(time.time()) + 600,
        },
        syncTokensConfigured=True,
    )
    # Registered, but its startup sync turned syncTokens off.
    tm = make_teslamate(master, owner_token(jti="unused"), registered=False)
    tm.syncTokens = False
    modules["TeslaMateVehicle"] = tm
    assert master.tokenSyncEnabled() is False
    master.loadSettings()

    assert api.ownerSlot.accessToken == token
    assert api.ownerSlot.source == "synced"
    assert api.ownerSlot.refreshToken == ""
    assert api.ownerSlot.expireTime == exp(token)
    assert api.fleetSlot.empty()
    assert "teslamate-refresh" not in json.dumps(master.settings)

    api.refreshTokenIfNeeded()
    assert all(ep.call_count == 0 for ep in refresh_endpoints)


def test_fleet_token_synced_when_sync_configured(tmp_path, refresh_endpoints):
    token = fleet_token(expires_in=600)
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": int(time.time()) + 600,
        },
        syncTokensConfigured=True,
    )
    assert api.fleetSlot.accessToken == token
    assert api.fleetSlot.source == "synced"
    assert api.fleetSlot.refreshToken == ""
    assert master.settings["carApiTokenSource"] == "synced"
    assert "teslamate-refresh" not in json.dumps(master.settings)

    api.refreshTokenIfNeeded()
    assert all(ep.call_count == 0 for ep in refresh_endpoints)


def test_saved_fleet_token_does_not_replace_startup_sync(tmp_path, fake_db):
    # A TeslaMate Fleet token saved in carApi* with an ordinary expiry,
    # sync on: the startup sync's fresher Fleet token is kept.
    fresh = fleet_token(jti="fresh")
    master, api = start_with_sync(
        tmp_path,
        {
            "carApiBearerToken": fleet_token(jti="saved"),
            "carApiRefreshToken": "teslamate-refresh",
            "carApiTokenExpireTime": int(time.time()) + 3600,
        },
        fake_db,
        fresh,
    )
    assert api.fleetSlot.accessToken == fresh
    assert api.fleetSlot.source == "synced"
    assert api.fleetSlot.refreshToken == ""


def test_owner_token_stays_pasted_when_sync_not_configured(tmp_path):
    token = owner_token()
    saved = int(time.time()) + 3600
    master, api = load(
        tmp_path,
        {
            "carApiBearerToken": token,
            "carApiRefreshToken": "r1",
            "carApiTokenExpireTime": saved,
        },
    )
    assert api.ownerSlot.accessToken == token
    assert api.ownerSlot.source == "pasted"
    assert api.ownerSlot.refreshToken == "r1"
    assert api.ownerSlot.expireTime == saved


def test_a_disabled_tesla_api_starts_and_keeps_its_saved_tokens(tmp_path):
    # vehicle.teslaAPI.enabled=false: getModuleByName answers None, and
    # startup and every settings save must still run, leaving the saved
    # tokens for when the module is turned back on
    token = owner_token()
    master, api, modules = master_with_settings(
        tmp_path, {"ownerApiBearerToken": token, "ownerApiTokenSource": "pasted"}
    )
    del modules["TeslaAPI"]
    master.loadSettings()
    master.saveSettings()
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert saved["ownerApiBearerToken"] == token
