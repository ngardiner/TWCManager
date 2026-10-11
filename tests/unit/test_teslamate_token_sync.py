"""
TeslaMate token sync: tokens pushed by TeslaMateVehicle.doSyncTokens must be
accepted on every sync, not only the first one, and TWCManager must never
refresh a synced token itself (TeslaMate owns the refresh): a synced slot
holds no refresh token and expires at the token's own exp.
"""

import time
from unittest.mock import Mock

import jwt
import pytest

from tests.unit.teslatokens import (
    AUTH_TOKEN_URL,
    FLEET_REFRESH_URL,
    FakeMaster,
    fleet_token,
    new_api,
    owner_token,
)

# The "never expires" sentinel earlier releases saved with a synced token.
SYNCED_EXPIRY = 99999 * 99999 * 99999


def exp(token):
    return jwt.decode(token, options={"verify_signature": False})["exp"]


def make_teslamate(master, access, registered=True):
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
    tm.rows = {"access": access}
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
        conn.cursor.return_value.fetchone.return_value = (rows["access"],)
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
        tm = make_teslamate(master, first, registered=False)
        fake_db["tm"] = tm

        # Startup sync runs from TeslaMateVehicle.__init__, before the
        # module is registered, so tokenSyncEnabled() is still False.
        tm.doSyncTokens(True)
        assert api.ownerSlot.accessToken == first

        # The module is now registered; this is what resyncTokens() sees.
        master.modules["TeslaMateVehicle"] = tm
        assert master.tokenSyncEnabled() is True
        tm.rows = {"access": second}
        tm.doSyncTokens()

        assert api.ownerSlot.accessToken == second
        assert api.ownerSlot.refreshToken == ""
        assert api.ownerSlot.expireTime == exp(second)

    def test_hand_entered_token_still_refused_while_sync_on(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        synced = owner_token()
        fake_db["tm"] = make_teslamate(master, synced)
        fake_db["tm"].doSyncTokens()

        assert api.storeToken(owner_token(jti="typed")) == "token_sync"
        assert api.ownerSlot.accessToken == synced


class TestNoRefreshForSyncedTokens:
    def test_synced_token_expires_at_its_exp_and_is_not_refreshed(
        self, fake_db, refresh_endpoints
    ):
        master = FakeMaster()
        api = new_api(master)
        # TeslaMate has not refreshed for a while: 10 minutes left.
        token = owner_token(expires_in=600)
        tm = make_teslamate(master, token, registered=False)
        fake_db["tm"] = tm

        tm.doSyncTokens(True)
        api.refreshTokenIfNeeded()

        assert api.ownerSlot.expireTime == exp(token)
        assert api.ownerSlot.refreshToken == ""
        assert all(ep.call_count == 0 for ep in refresh_endpoints)

    def test_sync_drops_a_held_refresh_token(self, fake_db):
        # TWCManager never holds a refresh token beside a synced token, so
        # nothing can refresh it, even before sync is registered.
        master = FakeMaster()
        api = new_api(master)
        api.ownerSlot.refreshToken = "before-sync"
        tm = make_teslamate(master, owner_token(), registered=False)
        fake_db["tm"] = tm

        tm.doSyncTokens(True)

        assert api.ownerSlot.refreshToken == ""
        assert api.ownerSlot.source == "synced"

    def test_refresh_paths_do_nothing_while_sync_on(self, fake_db, refresh_endpoints):
        master = FakeMaster()
        api = new_api(master)
        fake_db["tm"] = make_teslamate(master, owner_token())
        fake_db["tm"].doSyncTokens()
        # Even with an expiry in the past, nothing may refresh.
        api.ownerSlot.expireTime = time.time() - 60

        # The proactive refresh and the direct refresh used on a 401.
        api.refreshTokenIfNeeded()
        assert api.refreshSlot(api.ownerSlot) == "not_configured"

        assert all(ep.call_count == 0 for ep in refresh_endpoints)
        assert api.ownerSlot.refreshToken == ""


def master_with_settings(tmp_path, settings, **config_overrides):
    """A real TWCMaster with settings.json written, and a TeslaAPI registered.
    config_overrides go into config["config"].
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
    config["config"].update(config_overrides)
    master = TWCMaster("AB", config)
    # Not new_api(): TWCMaster.modules has its own structure.
    api = TeslaAPI(master)
    api.carApiVehicles = []
    modules = {"TeslaAPI": api}
    master.getModuleByName = modules.get
    return master, api, modules


class TestSettingsLoadAfterSyncTurnedOff:
    """Earlier releases saved a never-expires sentinel with a synced token.
    With sync off at load, that token is still marked synced: it keeps no
    refresh token and expires at its own exp."""

    def load(self, tmp_path, settings):
        master, api, _ = master_with_settings(tmp_path, settings)
        master.loadSettings()
        return api

    def test_sentinel_replaced_by_token_exp_when_sync_off(self, tmp_path):
        token = fleet_token()
        api = self.load(
            tmp_path,
            {
                "carApiBearerToken": token,
                "carApiRefreshToken": "refresh-1",
                "carApiTokenExpireTime": SYNCED_EXPIRY,
            },
        )
        assert api.fleetSlot.accessToken == token
        assert api.fleetSlot.expireTime == exp(token)
        assert api.fleetSlot.refreshToken == ""
        assert api.fleetSlot.source == "synced"

    def test_sentinel_with_undecodable_token_is_unusable(self, tmp_path):
        api = self.load(
            tmp_path,
            {
                "carApiBearerToken": "not-a-jwt-at-all",
                "carApiRefreshToken": "",
                "carApiTokenExpireTime": SYNCED_EXPIRY,
            },
        )
        assert api.fleetSlot.tokenType == "unknown"
        assert api.fleetSlot.source == "synced"
        assert api.fleetSlot.refreshToken == ""
        assert api.fleetSlot.expireTime != SYNCED_EXPIRY
        assert not api.fleetSlot.usable("read")
        assert api.slotFor("read") is None

    def test_ordinary_saved_expiry_unchanged(self, tmp_path):
        saved = int(time.time()) + 5 * 60 * 60
        api = self.load(
            tmp_path,
            {
                "carApiBearerToken": fleet_token(),
                "carApiRefreshToken": "refresh-1",
                "carApiTokenExpireTime": saved,
            },
        )
        assert api.fleetSlot.expireTime == saved
        assert api.fleetSlot.refreshToken == "refresh-1"
        assert api.fleetSlot.source == "pasted"


class TestSettingsLoadWhileSyncOn:
    """TWCManager.py loads settings after every module is registered, so
    TeslaMateVehicle's startup sync has already filled a slot and
    tokenSyncEnabled() is already True. A saved synced token doesn't replace
    the startup sync's fresher one; a saved pasted one does."""

    def start(self, tmp_path, fake_db, settings, fresh):
        master, api, modules = master_with_settings(tmp_path, settings)
        # Module load: TeslaMateVehicle.__init__ syncs, then it is registered.
        tm = make_teslamate(master, fresh, registered=False)
        fake_db["tm"] = tm
        tm.doSyncTokens(True)
        modules["TeslaMateVehicle"] = tm
        assert master.tokenSyncEnabled() is True
        master.loadSettings()
        return api

    def test_saved_synced_token_does_not_replace_startup_sync(self, tmp_path, fake_db):
        fresh = owner_token(jti="fresh")
        api = self.start(
            tmp_path,
            fake_db,
            {
                "ownerApiBearerToken": owner_token(jti="old", expires_in=3600),
                "ownerApiRefreshToken": "",
                "ownerApiTokenExpireTime": int(time.time()) + 3600,
                "ownerApiTokenSource": "synced",
                "carApiTokenSource": "",
            },
            fresh,
        )
        assert api.ownerSlot.accessToken == fresh
        assert api.ownerSlot.refreshToken == ""
        assert api.ownerSlot.expireTime == exp(fresh)
        assert api.ownerSlot.source == "synced"
        assert api.fleetSlot.accessToken == ""

    def test_saved_pasted_token_replaces_startup_sync(self, tmp_path, fake_db):
        pasted = owner_token(jti="pasted")
        saved = int(time.time()) + 3600
        api = self.start(
            tmp_path,
            fake_db,
            {
                "ownerApiBearerToken": pasted,
                "ownerApiRefreshToken": "my-refresh",
                "ownerApiTokenExpireTime": saved,
                "ownerApiTokenSource": "pasted",
                "carApiTokenSource": "",
            },
            owner_token(jti="fresh"),
        )
        assert api.ownerSlot.accessToken == pasted
        assert api.ownerSlot.refreshToken == "my-refresh"
        assert api.ownerSlot.expireTime == saved
        assert api.ownerSlot.source == "pasted"


class TestSettingsPageWhileSyncOn:
    """Settings has manual token fields. While sync is on, a token typed there
    is refused, nothing is stored, and the browser is sent to a page saying
    so."""

    def save(self, master, fields):
        from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

        handler = CreateHTTPHandlerClass(master)
        request = Mock()
        request.fields = fields
        request.getFieldValue = fields.get
        handler.process_save_settings(request)
        return request

    def test_refresh_token_refused(self, fake_db, refresh_endpoints, caplog):
        master = FakeMaster()
        api = new_api(master)
        synced = owner_token()
        fake_db["tm"] = make_teslamate(master, synced)
        fake_db["tm"].doSyncTokens()

        request = self.save(
            master, {"carApiRefreshToken": "typed-refresh", "chargeNowAmps": "6"}
        )

        assert api.ownerSlot.refreshToken == ""
        assert api.fleetSlot.refreshToken == ""
        assert api.ownerSlot.accessToken == synced
        assert api.ownerSlot.expireTime == exp(synced)
        assert all(ep.call_count == 0 for ep in refresh_endpoints)
        assert "carApiRefreshToken" not in master.settings
        # Other settings are still saved.
        assert master.settings["chargeNowAmps"] == "6"
        request.send_header.assert_any_call("Location", "/teslaAccount/token_sync")
        assert any("token sync is on" in r.getMessage() for r in caplog.records)

    def test_refresh_token_accepted_while_sync_off(self, refresh_endpoints):
        master = FakeMaster()
        api = new_api(master)
        api.storeToken(owner_token())

        request = self.save(master, {"carApiRefreshToken": "typed-refresh"})

        # Pasted on its own, the refresh token goes to the slot of its type
        # (opaque, so Fleet) and is refreshed there; for this public client
        # 1.3.4 sends the Fleet refresh to auth.tesla.com.
        fleet = refresh_endpoints[1]
        assert fleet.call_count == 1
        assert fleet.last_request.json()["client_id"] == "my-client-id"
        assert fleet.last_request.json()["refresh_token"] == "typed-refresh"
        request.send_header.assert_any_call("Location", "/")


class TestTokensTableLocation:
    """TeslaMate keeps its tokens in private.tokens on current releases and in
    public tokens on older ones; the sync must find either."""

    def _db(self, monkeypatch, present):
        import psycopg2
        import TWCManager.Vehicle.TeslaMateVehicle as tmmod

        queries = []

        def connect(**kwargs):
            conn = Mock()
            cur = conn.cursor.return_value

            def execute(sql):
                queries.append(sql)
                if (" " + present + " ") not in sql:
                    raise psycopg2.errors.UndefinedTable("missing")

            cur.execute.side_effect = execute
            cur.fetchone.return_value = (owner_token(),)
            return conn

        monkeypatch.setattr(tmmod.psycopg2, "connect", connect)
        return queries

    @pytest.mark.parametrize("present", ["private.tokens", "tokens"])
    def test_sync_reads_either_location(self, monkeypatch, present):
        queries = self._db(monkeypatch, present)
        master = FakeMaster()
        api = new_api(master)
        tm = make_teslamate(master, None)
        tm.doSyncTokens()
        assert api.ownerSlot.tokenType == "owner"
        assert api.ownerSlot.source == "synced"
        assert "FROM private.tokens " in queries[0]

    def test_no_tokens_table_logs_and_keeps_tokens(self, monkeypatch):
        self._db(monkeypatch, "nowhere")
        master = FakeMaster()
        api = new_api(master)
        tm = make_teslamate(master, None)
        tm.doSyncTokens()
        assert not api.ownerSlot.accessToken and not api.fleetSlot.accessToken


class TestDecryptTeslaMateVault:
    """TeslaMate stores tokens as Cloak AES.GCM.V1 blobs in bytea columns;
    psycopg2 returns those as memoryview, not Base64 text."""

    KEY = "an-encryption-key"

    def _blob(self, plaintext):
        import os
        from hashlib import sha256

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        tag = b"AES.GCM.V1"
        iv = os.urandom(12)
        # Cloak's AES.GCM authenticates the fixed AAD "AES256GCM".
        sealed = AESGCM(sha256(self.KEY.encode()).digest()).encrypt(
            iv, plaintext.encode(), b"AES256GCM"
        )
        ciphertext, ciphertag = sealed[:-16], sealed[-16:]
        return bytes([1, len(tag)]) + tag + iv + ciphertag + ciphertext

    def _tm(self):
        from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle

        tm = TeslaMateVehicle.__new__(TeslaMateVehicle)
        tm.encryption_key = self.KEY
        return tm

    def test_bytea_memoryview(self):
        blob = self._blob("secret-token")
        assert self._tm().decrypt_data(memoryview(blob)) == "secret-token"

    def test_base64_text_still_works(self):
        import base64

        blob = base64.b64encode(self._blob("secret-token")).decode()
        assert self._tm().decrypt_data(blob) == "secret-token"


class TestSyncBySlot:
    """TeslaMate sync copies the access token only, into the slot of its
    type, and never replaces a token the user pasted or logged in with."""

    def test_refresh_column_neither_selected_nor_decrypted(self, monkeypatch):
        import TWCManager.Vehicle.TeslaMateVehicle as tmmod

        sql = []
        conn = Mock()
        cur = conn.cursor.return_value
        cur.execute.side_effect = sql.append
        cur.fetchone.return_value = (owner_token(),)
        monkeypatch.setattr(tmmod.psycopg2, "connect", lambda **kw: conn)
        master = FakeMaster()
        new_api(master)
        tm = make_teslamate(master, None)
        decrypted = []
        tm.decrypt_data = lambda data: decrypted.append(data) or data

        tm.doSyncTokens()

        assert sql and all("refresh" not in s for s in sql)
        assert len(decrypted) == 1

    def test_sync_fills_empty_owner_slot(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        fake_db["tm"] = make_teslamate(master, owner_token())
        fake_db["tm"].doSyncTokens()
        assert api.ownerSlot.source == "synced"
        assert api.ownerSlot.refreshToken == ""

    def test_fleet_token_from_teslamate_fills_empty_fleet_slot(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        token = fleet_token()
        fake_db["tm"] = make_teslamate(master, token)
        fake_db["tm"].doSyncTokens()
        assert api.fleetSlot.accessToken == token
        assert api.fleetSlot.source == "synced"
        assert api.fleetSlot.refreshToken == ""
        assert not api.ownerSlot.accessToken

    def test_sync_replaces_its_own_token(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        tm = make_teslamate(master, owner_token(jti="1"))
        fake_db["tm"] = tm
        tm.doSyncTokens()
        second = owner_token(jti="2")
        tm.rows = {"access": second}
        tm.doSyncTokens()
        assert api.ownerSlot.accessToken == second

    @pytest.mark.parametrize("source", ["pasted", "login"])
    def test_sync_never_replaces_pasted_or_login(self, fake_db, caplog, source):
        import logging

        caplog.set_level(logging.INFO)
        master = FakeMaster()
        api = new_api(master)
        mine = owner_token(jti="mine")
        api.ownerSlot.setTokens(mine, "my-refresh", None, source)
        synced = owner_token(jti="synced")
        tm = make_teslamate(master, synced)
        fake_db["tm"] = tm
        for _ in range(3):
            tm.doSyncTokens()
        assert api.ownerSlot.accessToken == mine
        assert api.ownerSlot.source == source
        verb = "pasted" if source == "pasted" else "logged in with"
        expected = (
            "TeslaMate token sync: keeping the Owner API token you %s; "
            "not replacing it." % verb
        )
        assert [r.getMessage() for r in caplog.records].count(expected) == 1
        assert mine not in caplog.text and synced not in caplog.text
        assert "my-refresh" not in caplog.text

    def test_unknown_synced_token_skipped_with_error(self, fake_db, caplog):
        master = FakeMaster()
        api = new_api(master)
        bad = make_jwt_unknown()
        tm = make_teslamate(master, bad)
        fake_db["tm"] = tm
        tm.doSyncTokens()
        assert not api.ownerSlot.accessToken and not api.fleetSlot.accessToken
        assert (
            "TeslaMate token sync: the token's type (Owner API or Fleet API) "
            "was not recognised; not using it." in caplog.text
        )
        assert bad not in caplog.text

    def test_healing_cleared_slot_refilled_by_next_sync(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        api.ownerSlot.setTokens(owner_token(jti="mine"), "r", None, "pasted")
        tm = make_teslamate(master, owner_token(jti="synced"))
        fake_db["tm"] = tm
        api.ownerSlot.clear()  # as a 401 would
        tm.doSyncTokens()
        assert api.ownerSlot.source == "synced"

    def test_sync_beside_fleet_login(self, fake_db):
        master = FakeMaster()
        api = new_api(master)
        login = fleet_token(jti="login")
        api.fleetSlot.setTokens(login, "fleet-refresh", None, "login")
        tm = make_teslamate(master, owner_token())
        fake_db["tm"] = tm
        tm.doSyncTokens()
        assert api.ownerSlot.source == "synced"
        assert api.fleetSlot.accessToken == login
        assert api.fleetSlot.source == "login"
        assert api.readOnly is False


def make_jwt_unknown():
    from tests.unit.teslatokens import make_jwt

    return make_jwt(aud="https://example.com/", exp=int(time.time() + 3600))
