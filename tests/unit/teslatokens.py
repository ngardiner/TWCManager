"""
Shared helpers for the TeslaAPI token tests.

Builds JWTs shaped like the tokens TWCManager sees in the field, and a small
master stand-in whose tokenSyncEnabled() follows the TeslaMate module the way
TWCMaster.tokenSyncEnabled() does.
"""

import time

import jwt
import pytest

SIGNING_KEY = "unit-test-signing-key-0123456789abcdef"

OWNER_AUD = [
    "https://auth.tesla.com/oauth2/v3/userinfo",
    "https://owner-api.teslamotors.com/",
]
FLEET_AUD = [
    "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "https://fleet-api.prd.eu.vn.cloud.tesla.com",
]

FLEET_REFRESH_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUTH_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
OWNER_API_URL = "https://owner-api.teslamotors.com/api/1/vehicles"
FLEET_NA_URL = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles"


def make_jwt(**claims):
    """Encode claims as an HS256 JWT. TeslaAPI never verifies the signature."""
    return jwt.encode(claims, SIGNING_KEY, algorithm="HS256")


FLEET_SCOPES = [
    "openid",
    "user_data",
    "vehicle_device_data",
    "vehicle_cmds",
    "vehicle_charging_cmds",
    "energy_device_data",
    "energy_cmds",
    "offline_access",
]


def owner_token(
    expires_in=8 * 60 * 60, scp=("email", "offline_access", "openid"), **extra
):
    """A live-shaped TeslaMate access token: Owner API aud, azp ownerapi,
    and (as observed live on 2026-10-10) an ou_code of NA."""
    claims = {
        "iss": "https://auth.tesla.com/oauth2/v3",
        "aud": OWNER_AUD,
        "azp": "ownerapi",
        "ou_code": "NA",
        "exp": int(time.time() + expires_in),
    }
    if scp is not None:
        claims["scp"] = list(scp)
    claims.update(extra)
    return make_jwt(**claims)


def fleet_token(expires_in=8 * 60 * 60, scp=FLEET_SCOPES, **extra):
    """A Fleet API access token as issued to a registered third-party app."""
    claims = {
        "iss": "https://auth.tesla.com/oauth2/v3/nts",
        "aud": FLEET_AUD,
        "azp": "my-client-id",
        "ou_code": "NA",
        "exp": int(time.time() + expires_in),
    }
    if scp is not None:
        claims["scp"] = list(scp)
    claims.update(extra)
    return make_jwt(**claims)


class FakeMaster:
    """Just enough of TWCMaster for TeslaAPI and TeslaMateVehicle."""

    def __init__(self, **config_overrides):
        cfg = {"teslaApiClientID": "my-client-id"}
        cfg.update(config_overrides)
        self.config = {"config": cfg, "vehicle": {"teslaAPI": {"enabled": True}}}
        self.settings = {}
        self.modules = {}
        self.tasks = []
        self.stats = {"moduleSuccess": {}, "moduleFailures": {}}

    def getModuleByName(self, name):
        return self.modules.get(name)

    def tokenSyncEnabled(self):
        # Mirrors TWCMaster.tokenSyncEnabled()
        teslamate = self.modules.get("TeslaMateVehicle")
        return bool(teslamate and teslamate.syncTokens)

    def queue_background_task(self, task):
        self.tasks.append(task)

    def getHomeLatLon(self):
        return (37.0, -122.0)


def new_api(master=None, **config_overrides):
    """A TeslaAPI registered on a FakeMaster, with no inherited class state."""
    from TWCManager.Vehicle.TeslaAPI import TeslaAPI

    master = master or FakeMaster(**config_overrides)
    api = TeslaAPI(master)
    api.carApiVehicles = []
    master.modules["TeslaAPI"] = api
    return api


class _NoThread:
    daemon = False

    def start(self):
        pass


@pytest.fixture
def no_threads(monkeypatch):
    """Stop CarApiVehicle from starting its sync-monitoring thread."""
    import TWCManager.Vehicle.TeslaAPI as teslaapi

    monkeypatch.setattr(teslaapi, "Thread", lambda *a, **k: _NoThread())


def add_home_vehicle(api, command_signing=None, vin="VIN1", name="Car", id=1):
    """Add a vehicle (VIN1 by default), at home and recently contacted, so
    ready() is True without an HTTP call. command_signing is the /products
    field; None leaves it out. Use with the no_threads fixture."""
    entry = {"id": id, "vin": vin, "display_name": name}
    if command_signing is not None:
        entry["command_signing"] = command_signing
    api.addVehicle(entry)
    vehicle = api.getCarApiVehicles()[-1]
    vehicle.atHome = True
    vehicle.lastAPIAccessTime = time.time()
    return vehicle
