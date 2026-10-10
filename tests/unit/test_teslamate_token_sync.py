"""
TeslaMate token sync: tokens pushed by TeslaMateVehicle.doSyncTokens must be
accepted on every sync, not only the first one, and TWCManager must never
refresh a synced token itself (TeslaMate owns the refresh).
"""

import time
from unittest.mock import Mock

import pytest

from tests.unit.teslatokens import (
    AUTH_TOKEN_URL,
    FLEET_REFRESH_URL,
    FakeMaster,
    new_api,
    owner_token,
)

# The "never expires" sentinel TeslaMateVehicle.doSyncTokens has always set,
# so that TWCManager never refreshes a token TeslaMate owns.
SYNCED_EXPIRY = 99999 * 99999 * 99999


def make_teslamate(master, access, refresh, registered=True):
    """A TeslaMateVehicle wired to a fake TeslaMate database.

    Built with __new__ so __init__ does not start MQTT or the hourly
    resync Timer. registered=True puts it in master.modules, which is what
    makes master.tokenSyncEnabled() return True.
    """
    from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle

    tm = TeslaMateVehicle.__new__(TeslaMateVehicle)
    tm.master = master
    tm.syncTokens = True
    tm._TeslaMateVehicle__db_host = "db"
    tm._TeslaMateVehicle__db_name = "teslamate"
    tm._TeslaMateVehicle__db_user = "teslamate"
    tm._TeslaMateVehicle__db_pass = "secret"
    tm.encryption_key = "key"
    tm.rows = {"access": access, "refresh": refresh}
    # The fake database stores plaintext; decryption is the identity.
    tm.decrypt_data = lambda data: data
    if registered:
        master.modules["TeslaMateVehicle"] = tm
    return tm


@pytest.fixture
def fake_db(monkeypatch):
    """Patch psycopg2.connect to return the current tm.rows."""
    import TWCManager.Vehicle.TeslaMateVehicle as tmmod

    state = {"tm": None}

    def connect(**kwargs):
        conn = Mock()
        rows = state["tm"].rows
        conn.cursor.return_value.fetchone.return_value = (
            rows["access"],
            rows["refresh"],
        )
        return conn

    monkeypatch.setattr(tmmod.psycopg2, "connect", connect)
    return state


@pytest.fixture
def refresh_endpoints(requests_mock):
    """Mock both token endpoints so a refresh attempt is observable."""
    body = {"access_token": "X", "refresh_token": "Y", "expires_in": 28800}
    return [
        requests_mock.post(FLEET_REFRESH_URL, json=body),
        requests_mock.post(AUTH_TOKEN_URL, json=body),
    ]


class TestHourlyResync:
    def test_resync_after_registration_replaces_bearer_token(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        first, second = owner_token(), owner_token(jti="rotated")
        tm = make_teslamate(master, first, "refresh-1", registered=False)
        fake_db["tm"] = tm

        # Startup sync runs from TeslaMateVehicle.__init__, before the
        # module is registered, so tokenSyncEnabled() is still False.
        tm.doSyncTokens(True)
        assert api.getCarApiBearerToken() == first

        # The module is now registered; this is what resyncTokens() sees.
        master.modules["TeslaMateVehicle"] = tm
        assert master.tokenSyncEnabled() is True
        tm.rows = {"access": second, "refresh": "refresh-2"}
        tm.doSyncTokens()

        assert api.getCarApiBearerToken() == second
        assert api.getCarApiRefreshToken() == "refresh-2"

    def test_hand_entered_token_still_refused_while_sync_on(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        synced = owner_token()
        fake_db["tm"] = make_teslamate(master, synced, "refresh-1")
        fake_db["tm"].doSyncTokens()

        assert api.setCarApiBearerToken(owner_token(jti="typed")) is False
        assert api.getCarApiBearerToken() == synced


class TestNoRefreshForSyncedTokens:
    def test_sync_sets_never_expires_after_the_token(self, fake_db, refresh_endpoints):
        master = FakeMaster()
        api = new_api(master)
        # TeslaMate has not refreshed for a while: 10 minutes left.
        tm = make_teslamate(
            master, owner_token(expires_in=600), "refresh-1", registered=False
        )
        fake_db["tm"] = tm

        tm.doSyncTokens(True)

        assert api.getCarApiTokenExpireTime() == SYNCED_EXPIRY
        assert all(ep.call_count == 0 for ep in refresh_endpoints)

    def test_sentinel_set_before_the_refresh_token(self, fake_db):
        # Between the refresh token and the sentinel, a concurrent
        # refreshTokenIfNeeded() at startup (sync not yet registered) could
        # see the new refresh token with the decoded exp and refresh it.
        master = FakeMaster()
        api = new_api(master)
        api.carApiRefreshToken = "before-sync"
        tm = make_teslamate(master, owner_token(), "refresh-1", registered=False)
        fake_db["tm"] = tm
        seen = []
        setExpiry = api.setCarApiTokenExpireTime

        def record(value):
            seen.append((value, api.getCarApiRefreshToken()))
            return setExpiry(value)

        api.setCarApiTokenExpireTime = record

        tm.doSyncTokens(True)

        assert (SYNCED_EXPIRY, "before-sync") in seen
        assert api.getCarApiRefreshToken() == "refresh-1"

    def test_refresh_paths_do_nothing_while_sync_on(self, fake_db, refresh_endpoints):
        master = FakeMaster()
        api = new_api(master)
        fake_db["tm"] = make_teslamate(master, owner_token(), "refresh-1")
        fake_db["tm"].doSyncTokens()
        # Even with an expiry in the past, nothing may refresh.
        api.setCarApiTokenExpireTime(time.time() - 60)

        # The settings-load path (TWCMaster.loadSettings) and the direct
        # refresh used by wakeVehicle on a 401.
        api.setCarApiRefreshToken(api.getCarApiRefreshToken())
        assert api.apiRefresh() is False

        assert all(ep.call_count == 0 for ep in refresh_endpoints)
        assert api.getCarApiRefreshToken() == "refresh-1"


def master_with_settings(tmp_path, settings):
    """A real TWCMaster with settings.json written, and a TeslaAPI registered.
    Returns (master, api, modules); add to modules to register more."""
    import json

    from TWCManager.TWCMaster import TWCMaster
    from TWCManager.Vehicle.TeslaAPI import TeslaAPI

    (tmp_path / "settings.json").write_text(json.dumps(settings))
    config = {
        "config": {
            "settingsPath": str(tmp_path),
            "wiringMaxAmpsAllTWCs": 80,
            "teslaApiClientID": "my-client-id",
        },
        "vehicle": {"teslaAPI": {"enabled": True}},
    }
    master = TWCMaster("AB", config)
    # Not new_api(): TWCMaster.modules has its own structure.
    api = TeslaAPI(master)
    api.carApiBearerToken = ""
    api.carApiRefreshToken = ""
    api.carApiVehicles = []
    modules = {"TeslaAPI": api}
    master.getModuleByName = modules.get
    return master, api, modules


class TestSettingsLoadAfterSyncTurnedOff:
    """settings.json keeps the never-expires sentinel after a sync. If sync is
    later turned off, loading it must not leave a token that never refreshes."""

    def load(self, tmp_path, settings):
        master, api, _ = master_with_settings(tmp_path, settings)
        master.loadSettings()
        return api

    def test_sentinel_replaced_by_token_exp_when_sync_off(self, tmp_path):
        import jwt

        token = owner_token()
        exp = jwt.decode(token, options={"verify_signature": False})["exp"]
        api = self.load(
            tmp_path,
            {
                "carApiBearerToken": token,
                "carApiRefreshToken": "refresh-1",
                "carApiTokenExpireTime": SYNCED_EXPIRY,
            },
        )
        assert api.getCarApiTokenExpireTime() == exp

    def test_sentinel_with_undecodable_token_expires_now(self, tmp_path):
        before = time.time()
        api = self.load(
            tmp_path,
            {
                "carApiBearerToken": "not-a-jwt-at-all",
                "carApiRefreshToken": "",
                "carApiTokenExpireTime": SYNCED_EXPIRY,
            },
        )
        assert before <= api.getCarApiTokenExpireTime() <= time.time()

    def test_ordinary_saved_expiry_unchanged(self, tmp_path):
        saved = int(time.time()) + 5 * 60 * 60
        api = self.load(
            tmp_path,
            {
                "carApiBearerToken": owner_token(),
                "carApiRefreshToken": "refresh-1",
                "carApiTokenExpireTime": saved,
            },
        )
        assert api.getCarApiTokenExpireTime() == saved


class TestSettingsLoadWhileSyncOn:
    """TWCManager.py loads settings after every module is registered, so
    TeslaMateVehicle's startup sync has already set the tokens and
    tokenSyncEnabled() is already True. loadSettings must not put the saved
    (pre-sync) tokens or expiry back over them."""

    def test_saved_tokens_do_not_replace_startup_sync(self, tmp_path, fake_db):
        # Saved by an earlier run: an old token, its refresh token and an
        # expiry that has since passed.
        master, api, modules = master_with_settings(
            tmp_path,
            {
                "carApiBearerToken": owner_token(jti="old", expires_in=-3600),
                "carApiRefreshToken": "old-refresh",
                "carApiTokenExpireTime": int(time.time()) - 3600,
            },
        )

        # Module load: TeslaMateVehicle.__init__ syncs, then it is registered.
        fresh = owner_token(jti="fresh")
        tm = make_teslamate(master, fresh, "synced-refresh", registered=False)
        fake_db["tm"] = tm
        tm.doSyncTokens(True)
        modules["TeslaMateVehicle"] = tm
        assert master.tokenSyncEnabled() is True

        master.loadSettings()

        assert api.getCarApiBearerToken() == fresh
        assert api.getCarApiRefreshToken() == "synced-refresh"
        assert api.getCarApiTokenExpireTime() == SYNCED_EXPIRY
