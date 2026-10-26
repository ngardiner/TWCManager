"""
Pasted tokens go to the slot of their type; a slot holding a live token that
TeslaMate sync manages is protected from replacement until the user removes
it; the Settings page shows each slot and can remove its token.
"""

import os
import time
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import jinja2

from tests.unit.teslatokens import (
    AUTH_TOKEN_URL,
    FLEET_REFRESH_URL,
    fleet_token,
    make_jwt,
    new_api,
    owner_token,
)
from tests.unit.test_teslaapi_refresh import (
    OPAQUE_REFRESH,
    owner_refresh_token,
    save_settings,
    sync_on,
    token_response,
)

THEMES = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "lib",
    "TWCManager",
    "Control",
    "themes",
    "Default",
)


def login_template():
    """handle_teslalogin.html.j2 with the login form include stubbed (the
    real one needs the HTTP handler's teslaLogin global)."""
    loader = jinja2.DictLoader(
        {
            "handle_teslalogin.html.j2": open(
                os.path.join(THEMES, "handle_teslalogin.html.j2")
            ).read(),
            "request_teslalogin.html.j2": "LOGINFORM",
        }
    )
    return jinja2.Environment(loader=loader).get_template("handle_teslalogin.html.j2")


def synced_owner(api, expires_in=8 * 60 * 60):
    token = owner_token(expires_in=expires_in, jti="synced")
    api.storeToken(token, "", "synced")
    return token


def test_paste_routes_by_type_and_leaves_other_slot():
    api = new_api()
    login = fleet_token()
    api.storeToken(login, "R", "login")
    before = api.fleetSlot.snapshot()

    assert api.savePastedToken(owner_token()) == "success"

    assert api.ownerSlot.accessToken
    assert api.fleetSlot.snapshot() == before


def test_unknown_jwt_paste_refused():
    api = new_api()

    assert api.savePastedToken(make_jwt(aud=["x"])) == "unknown_type"

    assert api.ownerSlot.empty() and api.fleetSlot.empty()
    assert api.ownerSlot.refreshToken == api.fleetSlot.refreshToken == ""


def test_refresh_token_alone_routed_by_type(requests_mock):
    owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
    fleet = requests_mock.post(
        FLEET_REFRESH_URL,
        json={
            "access_token": fleet_token(),
            "refresh_token": "NA_new",
            "expires_in": 28800,
        },
    )
    api = new_api()

    assert api.savePastedToken(owner_refresh_token()) == "success"
    assert owner.call_count == 1 and fleet.call_count == 0
    assert not api.ownerSlot.empty() and api.fleetSlot.empty()

    assert api.savePastedToken(OPAQUE_REFRESH) == "success"
    assert fleet.call_count == 1
    assert not api.fleetSlot.empty()


def test_paste_over_synced_refused_while_sync_on(requests_mock):
    owner = requests_mock.post(AUTH_TOKEN_URL, json=token_response(owner_token()))
    fleet = requests_mock.post(FLEET_REFRESH_URL, json={})
    api = new_api()
    synced = synced_owner(api)
    sync_on(api)
    before = api.ownerSlot.snapshot()

    assert api.savePastedToken(owner_token(jti="u")) == "token_sync"
    assert api.pasteRefreshToken(owner_refresh_token()) == "token_sync"
    request = save_settings(api, {"carApiBearerToken": owner_token(jti="u2")})
    request.send_header.assert_any_call("Location", "/teslaAccount/token_sync")

    assert api.ownerSlot.snapshot() == before
    assert api.ownerSlot.accessToken == synced
    assert owner.call_count == 0 and fleet.call_count == 0
    assert requests_mock.call_count == 0


def test_paste_refusal_names_the_slot_and_never_the_token(caplog):
    api = new_api()
    synced_owner(api)
    sync_on(api)
    pasted = owner_token(jti="u")

    api.savePastedToken(pasted)

    assert "TeslaMate token sync manages the Owner API token" in caplog.text
    assert pasted not in caplog.text


def test_paste_into_other_slot_allowed_while_sync_on():
    api = new_api()
    synced_owner(api)
    sync_on(api)

    assert api.savePastedToken(fleet_token()) == "success"
    assert not api.fleetSlot.empty()


def test_paste_over_expired_synced_allowed():
    api = new_api()
    synced_owner(api, expires_in=-60)
    sync_on(api)
    assert api.ownerSlot.empty()

    assert api.savePastedToken(owner_token(jti="new")) == "success"
    assert api.ownerSlot.source == "pasted"


def test_paste_over_synced_allowed_when_sync_off():
    api = new_api()
    synced_owner(api)

    assert api.savePastedToken(owner_token(jti="new")) == "success"
    assert api.ownerSlot.source == "pasted"


def test_remove_token_clears_slot_and_saves():
    api = new_api()
    synced_owner(api)
    api.master.tasks.clear()

    assert api.removeToken("owner") is True

    assert api.ownerSlot.empty()
    assert {"cmd": "saveSettings"} in api.master.tasks
    api.master.tasks.clear()
    assert api.removeToken("x") is False
    assert api.master.tasks == []


def test_remove_synced_token_then_paste_works_while_sync_on():
    api = new_api()
    synced_owner(api)
    sync_on(api)
    api.removeToken("owner")

    assert api.savePastedToken(owner_token(jti="mine")) == "success"


def test_remove_token_route():
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

    api = new_api()
    api.storeToken(fleet_token(), "R", "login")
    handler = CreateHTTPHandlerClass(api.master)
    request = Mock()
    body = b"slot=fleet"
    request.path = "/teslaAccount/removeToken"
    request.fields = {}
    request.headers = {"content-length": str(len(body))}
    request.rfile.read.return_value = body
    request.getFieldValue = lambda key: {"slot": "fleet"}.get(key)

    handler.do_POST(request)

    assert api.fleetSlot.empty()
    request.send_response.assert_called_with(302)
    request.send_header.assert_any_call("Location", "/settings")


def test_fleet_login_allowed_while_sync_on_and_fleet_slot_free(requests_mock):
    from tests.unit.test_teslaapi_fleetlogin import PKCE_TOKEN_URL, TOKENS
    from tests.unit.test_teslaapi_fleetlogin import new_api as login_api

    api = login_api()
    api.master.getModuleByName = Mock(return_value=api)
    api.storeToken(owner_token(), "", "synced")
    api.master.tokenSyncEnabled.return_value = True
    adapter = requests_mock.post(PKCE_TOKEN_URL, json=TOKENS)
    state = parse_qs(urlsplit(api.getLoginURL("NA")).query)["state"][0]

    assert api.fleetTokenExchange("auth-code", state) == "success"

    assert adapter.call_count == 1
    assert api.fleetSlot.source == "login"
    assert api.ownerSlot.source == "synced"
    page = login_template().render(
        url=urlsplit("/teslaAccount/login"), master=api.master, apiAvailable=False
    )
    assert "LOGINFORM" in page


def test_login_form_shown_when_fleet_slot_free_and_hidden_when_synced():
    api = new_api()
    synced_owner(api)
    sync_on(api)
    api.master.modules["TeslaAPI"] = api
    kwargs = dict(
        url=urlsplit("/teslaAccount/x"), master=api.master, apiAvailable=False
    )
    template = login_template()

    assert "LOGINFORM" in template.render(**kwargs)

    api.storeToken(fleet_token(), "", "synced")
    assert "LOGINFORM" not in template.render(**kwargs)


def test_fleet_login_refused_before_exchange_when_fleet_slot_synced(requests_mock):
    from tests.unit.test_teslaapi_fleetlogin import PKCE_TOKEN_URL, TOKENS
    from tests.unit.test_teslaapi_fleetlogin import new_api as login_api

    api = login_api()
    api.storeToken(fleet_token(jti="s"), "", "synced")
    api.master.tokenSyncEnabled.return_value = True
    adapter = requests_mock.post(PKCE_TOKEN_URL, json=TOKENS)
    state = parse_qs(urlsplit(api.getLoginURL("NA")).query)["state"][0]
    before = api.fleetSlot.snapshot()

    assert api.fleetTokenExchange("auth-code", state) == "token_sync"

    assert adapter.call_count == 0
    assert api.fleetSlot.snapshot() == before


def test_settings_page_shows_slot_status():
    api = new_api()
    synced_owner(api)
    api.storeToken(fleet_token(), "R", "login")
    page = render_settings(api)

    assert "synced from TeslaMate" in page
    assert "Fleet API login" in page
    assert "removeToken-owner" in page and "removeToken-fleet" in page
    assert page.count("action='/teslaAccount/removeToken'") == 2
    # The Remove forms follow the main settings form: forms can't nest.
    assert page.index("</form>") < page.index("id='removeToken-owner'")


def test_settings_page_without_teslaapi_module():
    master = Mock()
    master.getModuleByName = Mock(return_value=None)
    page = render_settings(None, master)

    assert "removeToken" not in page
    assert "Manual Tesla API key override" in page


def render_settings(api, master=None):
    if master is None:
        master = api.master
    master.settings = getattr(master, "settings", {}) or {}

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(THEMES))
    # The handler's helper globals: their output is not under test.
    for name in (
        "addButton",
        "chargeScheduleDay",
        "checkBox",
        "checkForUpdates",
        "doChargeSchedule",
        "hoursDurationList",
        "locatedVehicles",
        "navbarItem",
        "optionList",
        "timeList",
        "vehicles",
    ):
        env.globals[name] = lambda *args, **kwargs: ""
    env.globals["ampsList"] = [0, 6]
    template = env.get_template("settings.html.j2")
    return template.render(master=master, url=urlsplit("/settings"))


def test_paste_mismatched_pair_refused_and_stores_nothing(caplog):
    api = new_api()
    access, refresh = fleet_token(), owner_refresh_token()
    before = (api.ownerSlot.snapshot(), api.fleetSlot.snapshot())

    assert api.storeToken(access, refresh, "pasted") == "mismatched_types"
    assert (api.ownerSlot.snapshot(), api.fleetSlot.snapshot()) == before
    assert access not in caplog.text and refresh not in caplog.text
    assert api.storeToken(owner_token(), refresh) == "success"  # matching pair

    # An opaque refresh token's type is unknown, so it is kept as before.
    assert api.storeToken(owner_token(), OPAQUE_REFRESH) == "success"
    assert api.ownerSlot.refreshToken == OPAQUE_REFRESH


def test_paste_mismatched_pair_from_settings_form_redirects():
    api = new_api()
    request = save_settings(
        api,
        {
            "carApiBearerToken": fleet_token(),
            "carApiRefreshToken": owner_refresh_token(),
        },
    )

    request.send_header.assert_any_call("Location", "/teslaAccount/mismatched_types")
    assert api.ownerSlot.empty() and api.fleetSlot.empty()
    page = login_template().render(
        url=urlsplit("/teslaAccount/mismatched_types"),
        master=api.master,
        apiAvailable=True,
    )
    assert (
        "The access token and refresh token are for different Tesla APIs "
        "(one Owner API, one Fleet API), so neither was stored. Paste them "
        "separately, or paste a matching pair." in " ".join(page.split())
    )


def test_identical_resync_keeps_pause_changes_and_logs(caplog):
    api = new_api()
    token = owner_token(jti="same")
    assert api.storeToken(token, "", "synced") == "success"
    api.ownerSlot.pause()
    paused, changes = api.ownerSlot.pausedUntil, api.ownerSlot.changes
    api.ownerSlot.scopeErrorsLogged.add("charge")
    caplog.clear()

    assert api.storeToken(token, "", "synced") == "success"

    assert api.ownerSlot.pausedUntil == paused
    assert api.ownerSlot.changes == changes
    assert api.ownerSlot.scopeErrorsLogged == {"charge"}
    assert caplog.text == ""
    # A different synced token still replaces it and ends the pause.
    assert api.storeToken(owner_token(jti="new"), "", "synced") == "success"
    assert api.ownerSlot.pausedUntil == 0


def test_login_form_shown_for_expired_synced_fleet_token():
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

    api = new_api()
    master = Mock()
    master.getModuleByName = lambda name: api if name == "TeslaAPI" else Mock()
    handler = CreateHTTPHandlerClass(master)

    def get_main():
        request = Mock()
        request.path = "/"
        request.headers = {}
        rendered = {}

        def render(values):
            rendered.update(values)
            return ""

        request.templateEnv.get_template.return_value.render = render
        handler.do_GET(request)
        return rendered["apiAvailable"]

    api.storeToken(fleet_token(expires_in=-60), "", "synced")
    assert api.fleetSlot.accessToken and api.fleetSlot.empty()
    assert get_main() is False
    api.storeToken(fleet_token(), "", "synced")
    assert get_main() is True
