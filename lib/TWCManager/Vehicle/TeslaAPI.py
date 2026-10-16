import base64
import hashlib
import json
import logging
import re
import requests
from requests.adapters import HTTPAdapter
import secrets
import ssl
from threading import RLock
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib3.util.ssl_ import create_urllib3_context
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Vehicle.Providers.TeslaAPIProviders import (
    FleetAPIProvider,
    OwnerAPIProvider,
)
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimits
from TWCManager.Vehicle.Reconcile.Outcome import (
    DONE,
    ERROR,
    REFUSED,
    SATISFIED,
    UNREACHABLE,
    Outcome,
)
from TWCManager.Vehicle.Registry.Fields import TELEMETRY_SOURCES
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaTokenSlot import TeslaTokenSlot, decodeToken

logger = LoggerFactory.get_logger("TeslaAPI", "Vehicle")

# _classify refreshed an expired token: the request is sent once more
_REFRESHED = Outcome(DONE, "refreshed")


class RegistryField:
    """A CarApiVehicle attribute that is a view onto a registry field: reads
    return the latest observation's value (or the legacy default), writes
    record an observation."""

    def __init__(self, field: str, default: Any, source: str = "TeslaAPI"):
        self.field = field
        self.default = default
        self.source = source

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        vehicle = instance._liveVehicle()
        obs = vehicle.latest(self.field) if vehicle is not None else None
        return self.default if obs is None else obs.value

    def __set__(self, instance, value):
        instance.vehicle.observe(self.field, value, self.source)


class TLS13Adapter(HTTPAdapter):
    """HTTPAdapter that enforces TLS 1.3, as release/1.3.3 does for Tesla
    Owner API support: auth.tesla.com requires TLS 1.3 for the Owner API
    token refresh."""

    def init_poolmanager(self, *args, **kwargs):
        context = create_urllib3_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


class TeslaAPI:
    # The energy-site list (/products) is fetched at most this often (seconds)
    ENERGY_SITES_TTL = 3600
    # A command refused for one of these reasons found the car already
    # where it was asked to be
    COMMAND_SATISFIED_REASONS = (
        "already_set",
        "charging",
        "is_charging",
        "not_charging",
        "complete",
        "disconnected",
        "requested",
    )
    # A command refused for one of these reasons did not reach the car
    COMMAND_UNREACHABLE_REASONS = ("could_not_wake_buses", "vehicle_unavailable")
    # Seconds between failed wake_up attempts on one car (a wake is $0.02)
    WAKE_RETRY_SECONDS = 30 * 60
    # fleetSlot.changes when the module-wide read-only line was last logged
    refusalLoggedAt = None
    regionURL = {
        "NA": "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles",
        "EU": "https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/vehicles",
        "CN": "https://fleet-api.prd.cn.vn.cloud.tesla.cn/api/1/vehicles",
        # Not a Fleet region: the Owner API used by TeslaMate tokens. It reads
        # every car but commands only cars Tesla lists with command_signing
        # "off" (see commandSlotFor). fleetRegions() leaves it out of every
        # region list and lookup.
        "OwnerAPI": "https://owner-api.teslamotors.com/api/1/vehicles",
    }
    # teslaProxy base URL and certificate from config.json, used for Fleet
    # API tokens (the Vehicle Command proxy only takes Fleet tokens).
    proxyURL = ""
    proxyCert = True
    # One slot per token type; built in __init__.
    ownerSlot = None
    fleetSlot = None
    # fleetSlot.changes when the read-only hint was last logged
    refusalLoggedAt = None
    refreshClientID = ""
    # FleetAPI OAuth (authorization code) login. Two variants, auto-selected by
    # whether a client secret is configured:
    #   - PKCE / public client (default, no secret): code_challenge, no audience,
    #     tokens exchanged/refreshed at auth.tesla.com. This is what most
    #     registered third-party apps use.
    #   - Confidential client (teslaApiClientSecret set): client_secret + audience,
    #     tokens exchanged at the regional fleet-auth endpoint.
    authorizeURL = "https://auth.tesla.com/oauth2/v3/authorize"
    authTokenURL = "https://auth.tesla.com/oauth2/v3/token"
    # iss of an Owner API refresh token (a JWT). Fleet tokens use .../v3/nts,
    # and Fleet refresh tokens are opaque.
    ownerTokenIssuer = "https://auth.tesla.com/oauth2/v3"
    clientSecret = ""
    redirectURI = ""
    region = "NA"
    scope = (
        "openid offline_access vehicle_device_data vehicle_cmds "
        "vehicle_location vehicle_charging_cmds"
    )
    __loginState = None
    __loginRegion = None
    __loginVerifier = None
    carApiLastErrorTime = 0
    # The last reason loadList logged for having no usable token
    _noTokenLogged = None
    wakeDelayMins = 3
    # The never-expires expiry earlier releases saved with a synced token.
    # Only compared against, when restoring saved settings.
    LEGACY_SYNCED_EXPIRY = 99999 * 99999 * 99999
    lastChargeCheck = 0
    chargeUpdateInterval = 1800
    carApiVehicles = []
    vehicleListLock = RLock()
    enabled = True
    config = None
    master = None
    __email = None
    errorCount = 0
    maxLoginRetries = 10
    params = None
    __password = None
    fleetRefreshURL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    __resp = None
    session = None

    # Transient errors are ones that usually disappear if we retry the car API
    # command a minute or less later.
    # 'vehicle unavailable:' sounds like it implies the car is out of connection
    # range, but I once saw it returned by drive_state after wake_up returned
    # 'online'. In that case, the car is reachable, but drive_state failed for some
    # reason. Thus we consider it a transient error.
    # Error strings below need only match the start of an error response such as:
    # {'response': None, 'error_description': '',
    # 'error': 'operation_timedout for txid `4853e3ad74de12733f8cc957c9f60040`}'}
    carApiTransientErrors = [
        "upstream internal error",
        "operation_timedout",
        "vehicle unavailable",
        "context deadline exceeded",
    ]

    def __init__(self, master):
        self.master = master
        # The module's clock for the command and wake paths (wakeDue,
        # wakeOutcome, CarApiVehicle.is_awake): a test replaces it instead of
        # the process-wide time.time
        self.now = time.time
        self.registry = VehicleRegistry.forMaster(master)
        # slot.changes when a skipped sync was last logged, per slot.
        self.syncSkipLoggedAt = {"owner": None, "fleet": None}
        # Energy-site state per slot kind, for the Wall Connector VIN read.
        # (slot.changes, time, sites) of the last good /products answer
        self._energySites = {}
        # time.time() when the slot's sites were found to hold no Wall
        # Connector; ready again after ENERGY_SITES_TTL
        self._energySitesEmpty = {}
        # Site IDs whose live_status had no wall_connectors key, until the
        # site list is fetched again
        self._wallConnectorSitesDropped = {}
        # slot.changes when the slot's token was refused energy data
        self._wallConnectorsRefused = {}

        try:
            self.config = master.config
            cfg = self.config.get("vehicle", {}).get("teslaAPI", {}) or {}
            self._enabled = cfg.get("enabled", True)

            proxyURL = self.config["config"].get("teslaProxy", "")
            if proxyURL:
                self.proxyURL = proxyURL + "/api/1/vehicles"
                self.proxyCert = self.config["config"].get("teslaProxyCert", True)
            self.refreshClientID = self.config["config"].get("teslaApiClientID", "")
            self.clientSecret = self.config["config"].get("teslaApiClientSecret", "")
            self.redirectURI = self.config["config"].get("teslaApiRedirectUri", "")
            self.region = self.config["config"].get("teslaApiRegion", "NA")
            self.scope = self.config["config"].get("teslaApiScope", self.scope)
            self.chargeUpdateInterval = self.config["config"].get(
                "cloudUpdateInterval", 1800
            )
            self.wakeDelayMins = self.config["config"].get("wakeDelayMins", 3)
        except KeyError:
            pass

        # The Owner API refresh goes through this session, as in release/1.3.3.
        self.session = requests.Session()
        self.session.mount("https://auth.tesla.com", TLS13Adapter())

        # Owner tokens refresh as in release/1.3.3 (client_id "ownerapi",
        # through the TLS 1.3 session); Fleet tokens with teslaApiClientID.
        self.ownerSlot = TeslaTokenSlot(
            "owner", self.tokenRoute, self.authTokenURL, "ownerapi", self.session.post
        )
        self.fleetSlot = TeslaTokenSlot(
            "fleet",
            self.tokenRoute,
            self.fleetRefreshURL,
            self.refreshClientID,
            requests.post,
        )

    def enabled(self) -> bool:
        return self._enabled

    def providers(self):
        return [OwnerAPIProvider(self), FleetAPIProvider(self)]

    def addVehicle(self, json):
        if "vin" in json:
            try:
                car = CarApiVehicle(json, self, self.config)
            except ValueError:
                # The registry refuses a missing or malformed VIN.
                logger.debug("Skipping a listed vehicle without a usable VIN")
                return
            self.carApiVehicles.append(car)

    def refreshTokenTypeKnown(self, token):
        # True when the refresh token's type can be told from the token
        # itself: only an Owner API refresh token (a JWT) is recognisable.
        decoded = self.decodeToken(token)
        return decoded is not None and decoded.get("iss") == self.ownerTokenIssuer

    def refreshTokenType(self, token):
        # "owner" for an Owner API refresh token, a JWT issued by
        # auth.tesla.com/oauth2/v3; "fleet" for anything else, including an
        # opaque token (Fleet refresh tokens are opaque).
        decoded = self.decodeToken(token)
        if decoded is not None and decoded.get("iss") == self.ownerTokenIssuer:
            return "owner"
        return "fleet"

    def refreshTokenIfNeeded(self):
        # Proactively refresh each slot's token if it's missing or close to
        # expiry. Called before the vehicle list loads and from read-only
        # callers such as Storm Watch, which otherwise have no other path
        # that keeps the token alive. A synced
        # slot holds no refresh token, so it never refreshes.
        for slot in (self.ownerSlot, self.fleetSlot):
            if slot.needsRefresh():
                logger.log(logging.INFO8, "Attempting token refresh")
                self.refreshSlot(slot)

    def refreshSlot(self, slot, saveOnFailure=True):
        # Refreshes one slot's tokens. Returns slot.refresh()'s result:
        # "success", "rejected", "unreachable" or "not_configured".
        # saveOnFailure=False leaves saving a failure's state to the caller.
        result = slot.refresh()
        if result == "success":
            self.master.queue_background_task({"cmd": "saveSettings"})
        elif result != "not_configured":
            # A rejected refresh token (401) has cleared the slot, so no
            # further attempts are made with it until the user supplies
            # fresh tokens: retrying a bad token might make Tesla block
            # remote access to the car.
            self.updateCarApiLastErrorTime()
            if saveOnFailure:
                self.master.queue_background_task({"cmd": "saveSettings"})
        return result

    def _request(
        self,
        slot,
        method,
        path,
        purpose,
        body=None,
        timeout=10,
        vehicle=None,
        tries=3,
    ):
        # One request with slot's token to path under the API root (where
        # .../api/1/vehicles lives), bounded by timeout. Returns (response,
        # None) for an HTTP 200, else (response or None, the Outcome
        # _classify gave). tries is how many requests and token refreshes
        # this call may make, one each, refreshing at most once: 3 refreshes
        # an expired token and sends again, 2 refreshes but leaves the resend
        # to the caller's next round, 1 never refreshes (a 401 retires the
        # slot). A transport error is logged and carried by its type alone,
        # never its text (it can hold the header).
        while True:
            try:
                req = getattr(requests, method)(
                    slot.baseURL[: -len("vehicles")] + path,
                    headers={
                        "accept": "application/json",
                        "Authorization": "Bearer " + slot.accessToken,
                    },
                    json=body,
                    verify=slot.verifyCert,
                    timeout=timeout,
                )
            except requests.exceptions.RequestException as e:
                return None, Outcome(ERROR, type(e).__name__)
            tries -= 1
            logger.log(
                logging.INFO8,
                "Car API %s %s: HTTP %s" % (method, path, req.status_code),
            )
            if (
                purpose in ("command", "wake")
                and slot is self.ownerSlot
                and vehicle is not None
                and self.noteOwnerCommandRefused(vehicle, req)
            ):
                # Tesla refused the Owner API for this car (logged, latched):
                # no error backoff, so the next provider sends at once
                return req, Outcome(REFUSED, "owner refused")
            outcome = self._classify(
                slot, req.status_code, purpose, req.text, vehicle, tries > 0
            )
            if outcome is not _REFRESHED:
                return req, outcome
            tries = min(tries - 1, 1)
            if tries < 1:
                return req, Outcome(REFUSED, "401")

    def _classify(self, slot, status, purpose, body, vehicle=None, refresh=True):
        # What an answer means, acted on: None for an HTTP 200, else an
        # Outcome. purpose is "read", "list" (the vehicle list), "command",
        # "wake" or "energy" (the Wall Connector VIN read). Energy refusals
        # latch that read alone: Storm Watch and vehicle reads keep the token.
        # Elsewhere a 401 refreshes an expired token once (_REFRESHED: send
        # again) and otherwise retires the slot; 403 refuses commands or wakes
        # and pauses reads for an hour; 412 retires commands, or latches reads
        # until the token changes; 410 pauses reads; 429 backs off; 408 is an
        # asleep or unreachable car. None of these touches the API backoff
        # but 429, so the next poll can use the other slot at once.
        if status == 200:
            return None
        if purpose == "energy":
            if status not in (401, 403, 410, 412):
                return Outcome(ERROR, str(status))
            self._refuseWallConnectors(slot, status)
            return Outcome(REFUSED, str(status))
        paid = purpose in ("command", "wake")
        label = slot.kind.capitalize()
        if status == 401:
            if (
                refresh
                and "expired" in body
                and slot.source != "synced"
                and slot.refreshToken
            ):
                result = self.refreshSlot(slot)
                if result == "success":
                    return _REFRESHED
                if result == "unreachable":
                    return Outcome(UNREACHABLE, "token refresh")
                if paid and not slot.empty():
                    # Expired and the refresh failed (a 5xx, no client ID):
                    # the tokens are kept, as refresh() alone clears on a
                    # 401, but no later round pays for another 401. A
                    # successful refresh (setTokens) lifts the refusal.
                    logger.error(
                        "Tesla %s API token has expired and could not be "
                        "refreshed (%s); not sending commands with it until "
                        "it is refreshed or replaced." % (label, result)
                    )
                    slot.refuse("command")
                return Outcome(REFUSED, "401")
            # Retrying a bad token might make Tesla block remote access to
            # the car: nothing more is sent with it until the user supplies
            # fresh tokens
            logger.error(
                "Tesla %s API token was rejected (HTTP 401); removing it." % label
            )
            slot.clear()
            self.master.queue_background_task({"cmd": "saveSettings"})
            return Outcome(REFUSED, "401")
        if paid and status == 403:
            slot.reportScopeRefused(purpose, refused=True)
            if purpose == "wake":
                # Not REFUSED, which would exclude Fleet for commands too
                return Outcome(UNREACHABLE, "wake refused")
            return Outcome(REFUSED, "403")
        if paid and status == 412:
            # A token kind Tesla will keep refusing commands (e.g. a
            # registration it lacks): no paid command or wake is sent with
            # it again until it changes. Its reads are untouched.
            logger.error(
                "Tesla %s API refused a command (HTTP 412); not sending commands "
                "with this token until it is replaced." % label
            )
            slot.refuse("command")
            return Outcome(REFUSED, "412")
        if not paid and status in (403, 410, 412):
            # A 412 ("only available on fleetapi") will keep coming, so it
            # holds until the token changes
            hold = "until it is replaced" if status == 412 else "for an hour"
            logger.error(
                "Tesla %s API refused a read (HTTP %d); not using that token "
                "for reads %s." % (label, status, hold)
            )
            slot.pause(latch=status == 412)
            return Outcome(REFUSED, str(status))
        if status == 408:
            return Outcome(UNREACHABLE, "408")
        if status == 429:
            # Explicitly told to back off
            target = vehicle or self
            target.errorCount = max(30, target.errorCount)
            self.updateCarApiLastErrorTime(vehicle)
        return Outcome(ERROR, str(status))

    def tokenRoute(self, token):
        # (tokenType, baseURL, verifyCert) for an access token: where its
        # requests go. Both slots classify their tokens with this.
        tokenType, region = self.classifyToken(self.decodeToken(token))
        if tokenType == "owner":
            # The Vehicle Command proxy only takes Fleet tokens.
            baseURL, verifyCert = self.regionURL["OwnerAPI"], True
        elif tokenType == "fleet" and self.proxyURL:
            baseURL, verifyCert = self.proxyURL, self.proxyCert
        elif tokenType == "fleet":
            baseURL, verifyCert = self.regionURL[region], True
        else:
            baseURL, verifyCert = "", True
            logger.error(
                "Unable to determine the Tesla API token type (Owner API or "
                "Fleet API). No Tesla API calls will be made until a "
                "recognised token is supplied."
            )
        logger.info(
            "Tesla API token type: %s, base URL host: %s"
            % (tokenType, urlsplit(baseURL).netloc or "none")
        )
        return tokenType, baseURL, verifyCert

    def slotOfType(self, tokenType):
        if tokenType == "owner":
            return self.ownerSlot
        if tokenType == "fleet":
            return self.fleetSlot
        return None

    def slotFor(self, purpose):
        # The slot a request of this purpose ("read", "command" or "energy")
        # uses, or None when no slot is usable for it. Only a Fleet token
        # serves "command" here; commands to a car choose with commandSlotFor.
        for slot in (self.ownerSlot, self.fleetSlot):
            if slot.usable(purpose):
                return slot
        return None

    def ownerTokenHeld(self):
        # True when the owner slot holds a live Owner API token. A read
        # pause does not count: it is for reads only.
        return not self.ownerSlot.empty() and self.ownerSlot.tokenType == "owner"

    def ownerCommandsAllowed(self, vehicle):
        # True when an Owner API token may command this car: Tesla listed it
        # with command_signing exactly "off" and has not since refused an
        # Owner API command for it. "required", a missing field and any
        # other value are read-only (fail closed).
        return (
            self.ownerTokenHeld()
            and getattr(vehicle, "commandSigning", None) == "off"
            and not getattr(vehicle, "ownerCommandsRefused", False)
        )

    def commandSlotFor(self, vehicle):
        # The slot a command to this car uses, or None when it is read-only.
        # Fleet first, so a working Fleet (and proxy) setup is unchanged;
        # the Owner token only carries commands when Fleet can't.
        if self.fleetSlot.usable("command"):
            return self.fleetSlot
        if self.ownerCommandsAllowed(vehicle):
            return self.ownerSlot
        return None

    def readOnlyFor(self, vehicle):
        # True when no slot can send this car commands.
        return self.commandSlotFor(vehicle) is None

    @property
    def readOnly(self):
        # True when no listed car can be sent commands. With an Owner token
        # and no cars listed yet it is False: whether the token can command
        # is only known once they are listed, and only a command (or
        # telemetry) lists them. Commands still check each car.
        if self.fleetSlot.usable("command"):
            return False
        vehicles = self.getCarApiVehicles()
        if not vehicles:
            return not self.ownerTokenHeld()
        return not any(self.ownerCommandsAllowed(v) for v in vehicles)

    @property
    def ownerTokenOnly(self):
        # True when only an Owner API token is held for commands, so which
        # cars can be commanded depends on each car's command_signing.
        return not self.fleetSlot.usable("command") and self.ownerTokenHeld()

    def loadVehicleList(self):
        # Populate carApiVehicles from the vehicle list of the read slot
        # (Owner API /products, else the Fleet API vehicle list). Returns True
        # if vehicles are (now) known, False if the fetch failed or no slot is
        # usable for reads. Safe to call from any thread; concurrent callers
        # will not double-add vehicles.
        with self.vehicleListLock:
            if self.getVehicleCount() > 0:
                return True
            slot = self.slotFor("read")
            if slot is None:
                return False
            listed = self._vehicleList(slot)
            if listed is None:
                return False
            if listed is False:
                logger.log(
                    logging.INFO2,
                    "ERROR: Can't get list of vehicles via Tesla car API.  Will try again in "
                    + str(self.getCarApiErrorRetryMins())
                    + " minutes.",
                )
                self.updateCarApiLastErrorTime()
                return False
            for car in listed:
                self.addVehicle(car)
            self.resetCarApiLastErrorTime()
        # Outside the lock: its other slot's GET never holds it
        self.resolveHeldLimits(slot, listed)
        return True

    def listPending(self) -> bool:
        # The vehicle list is not loaded, or held limits wait on it
        held = getattr(self.master, "heldLimits", None)
        return self.getVehicleCount() < 1 or (
            isinstance(held, HeldLimits) and held.waiting()
        )

    def loadList(self):
        # The vehicle list (a free call) loads here, and held limits waiting
        # on it resolve: the cloud providers serve only listed cars (C1)
        if self.getVehicleCount() > 0:
            self.resolveHeldLimits()
        elif not self.getCarApiRetryRemaining():
            self.refreshTokenIfNeeded()
            if self.slotFor("read") is not None:
                self.loadVehicleList()
            else:
                self._noTokenReason(time.time())

    def _noTokenReason(self, now):
        # Why no slot can read, logged once per change of reason
        slots = (self.ownerSlot, self.fleetSlot)
        if any(s.accessToken and s.tokenType != s.kind for s in slots):
            reason = "token type unknown"
        elif any(now < s.pausedUntil for s in slots):
            reason = "paused"
        else:
            reason = "no Tesla API token yet"
        if reason != self._noTokenLogged:
            self._noTokenLogged = reason
            logger.log(logging.INFO8, "No usable Tesla API token (%s)" % reason)

    def resolveHeldLimits(self, listedSlot=None, listed=None):
        # Held limits older releases keyed by vehicle ID move under the VIN
        # (free calls, only while any wait). Owner and Fleet may be different
        # accounts: every usable slot's list counts, and a list that failed
        # defers it to the next check (I9)
        held = getattr(self.master, "heldLimits", None)
        if not isinstance(held, HeldLimits) or not held.waiting():
            return
        lists = [
            listed if slot is listedSlot else self._vehicleList(slot)
            for slot in (self.ownerSlot, self.fleetSlot)
            if slot is listedSlot or slot.usable("read")
        ]
        if lists and all(isinstance(cars, list) for cars in lists):
            held.resolveLegacyIds(
                {car.get("id"): car.get("vin") for cars in lists for car in cars}
            )

    def _vehicleList(self, slot):
        # One free GET of the slot's vehicle list: the listed cars; None when
        # the slot refused (it is cleared or paused, with no API error
        # backoff, so the next poll can use the other slot at once); False on
        # any other failure. The Owner API no longer lists vehicles under
        # /vehicles (it answers 412 "only available on fleetapi"); /products
        # still does, with energy sites that addVehicle skips (no VIN).
        path = "products" if slot.tokenType == "owner" else "vehicles"
        # After a refresh the list is not sent again: the next poll is
        req, outcome = self._request(slot, "get", path, "list", tries=2)
        if outcome is not None:
            return None if outcome.kind == REFUSED else False
        try:
            listed = json.loads(req.text)
            cars = listed["response"][: listed["count"]]
            return [car for car in cars if isinstance(car, dict)]
        except (ValueError, KeyError, TypeError):
            # Not JSON, or no 'response' or 'count' in the answer
            return False

    def is_location_within_radius(self, lat, lon, radius):
        if (
            abs(self.master.getHomeLatLon()[0] - lat) > radius
            or abs(self.master.getHomeLatLon()[1] - lon) > radius
        ):
            return False

        return True

    def is_location_home(self, lat, lon, gpsAge=None):
        if self.master.getHomeLatLon()[0] == 10000:
            # Don't seed home from a stale GPS fix. A sleeping car reports its
            # last known coordinates, which can be hours old and kilometres
            # away; seeding retries on a later poll once the fix is fresh.
            if gpsAge is not None and gpsAge > 600:
                logger.info(
                    "Home location for vehicles has never been set, but the "
                    "vehicle's GPS fix is %d seconds old. Waiting for a fresh "
                    "fix before setting home." % gpsAge
                )
                return False
            logger.info(
                "Home location for vehicles has never been set.  "
                + "We'll assume home is where we found the first vehicle currently parked.  "
                + "Home set to lat="
                + str(lat)
                + ", lon="
                + str(lon)
            )
            self.master.setHomeLat(lat)
            self.master.setHomeLon(lon)
            self.master.queue_background_task({"cmd": "saveSettings"})
            self.master.queue_background_task({"cmd": "sunrise"})
            return True

        # 1 lat or lon = ~364488.888 feet. The exact feet is different depending
        # on the value of latitude, but this value should be close enough for
        # our rough needs.
        # 1/364488.888 * 10560 = 0.0289.
        # So if vehicle is within 0289 lat and lon of homeLat/Lon,
        # it's within ~10560 feet (2 miles) of home and we'll consider it to be
        # at home.
        # I originally tried using 0.00548 (~2000 feet) but one night the car
        # consistently reported being 2839 feet away from home despite being
        # parked in the exact spot I always park it.  This is very odd because
        # GPS is supposed to be accurate to within 12 feet.  Tesla phone app
        # also reports the car is not at its usual address.  I suspect this
        # is another case of a bug that's been causing car GPS to freeze  the
        # last couple months.
        # Desired maximum destination from home is now configurable. As the
        # parameter may not be set, we catch the key error and use the default
        # value documented above.
        atHomeRadius = (
            1 / 364488.888 * float(self.config["config"].get("atHomeRadius", 10560))
        )
        return self.is_location_within_radius(lat, lon, atHomeRadius)

    def is_far_from_home(self, lat, lon):
        return not self.is_location_within_radius(lat, lon, 1.4)

    def getCarApiErrorRetryMins(self, vehicle=None):
        errorCount = self.errorCount
        if vehicle:
            errorCount = max(vehicle.errorCount, errorCount)
        errorCount = max(errorCount - 1, 0)
        return min(errorCount, 10)

    def getCarApiLastErrorTime(self):
        return self.carApiLastErrorTime

    # Energy sites and the Wall Connector VINs. Only the free /products and
    # /energy_sites/<id>/live_status are ever requested here: never a vehicle
    # endpoint, vehicle_data, a wake or a command. Background thread only.
    # Unverified on hardware: that live_status lists wall_connectors, each
    # with a din and, while a car is connected, its vin (tesla-fleet-api npm
    # types), and whether the Owner API serves live_status at all (a refusal
    # falls to the Fleet slot).

    def _refuseWallConnectors(self, slot, status):
        # Latches the Wall Connector read off for slot until its token changes
        if status == 403:
            # Only a 403 says the scope is missing; a 401 is authentication
            slot.reportScopeRefused("energy", refused=True)
        self._wallConnectorsRefused[slot.kind] = slot.changes
        logger.info(
            "Wall Connector VINs: %s token refused energy data (HTTP %d); not "
            "asking again until the token changes" % (slot.kind, status)
        )

    def _fetchEnergySites(self, slot) -> Optional[List[dict]]:
        # The slot's energy sites, cached for ENERGY_SITES_TTL; None when
        # they could not be read. Never raises.
        now = time.time()
        cached = self._energySites.get(slot.kind)
        if (
            cached is not None
            and cached[0] == slot.changes
            and now - cached[1] < self.ENERGY_SITES_TTL
        ):
            return cached[2]
        req, outcome = self._request(slot, "get", "products", "energy")
        if outcome is not None:
            return None
        try:
            sites = [
                entry
                for entry in json.loads(req.text)["response"]
                if isinstance(entry, dict) and "energy_site_id" in entry
            ]
        except (ValueError, KeyError, TypeError):
            return None
        self._energySites[slot.kind] = (slot.changes, now, sites)
        self._wallConnectorSitesDropped[slot.kind] = set()
        return sites

    def energySites(self, slot=None) -> List[dict]:
        # The /products entries with an energy_site_id; [] on failure
        slot = slot or self.slotFor("energy")
        if slot is None:
            return []
        sites = self._fetchEnergySites(slot)
        return [] if sites is None else list(sites)

    def wallConnectorVinsReady(self, slot) -> bool:
        # Cache reads only, no I/O (a provider's ready() asks this). The
        # scope is checked first: usable() would log a missing one, and this
        # is asked of every token whether or not a Gen3 is configured.
        if slot is None or not slot.grants("energy") or not slot.usable("energy"):
            return False
        if self._wallConnectorsRefused.get(slot.kind) == slot.changes:
            return False
        emptyAt = self._energySitesEmpty.get(slot.kind)
        return emptyAt is None or time.time() - emptyAt >= self.ENERGY_SITES_TTL

    def wallConnectorVins(self, slot) -> Optional[Dict[str, str]]:
        # {din: vin} over every energy site's live_status wall_connectors
        # that report a vin. None when nothing could be read or the token
        # was refused. Never raises.
        if slot is None:
            return None
        self.refreshTokenIfNeeded()
        sites = self._fetchEnergySites(slot)
        if sites is None:
            return None
        dropped = self._wallConnectorSitesDropped.setdefault(slot.kind, set())
        siteIDs = [s["energy_site_id"] for s in sites]
        siteIDs = [i for i in siteIDs if i not in dropped]
        if not siteIDs:
            self._energySitesEmpty[slot.kind] = time.time()
            return {}
        self._energySitesEmpty.pop(slot.kind, None)
        vins = {}
        answered = False
        for siteID in siteIDs:
            req, outcome = self._request(
                slot, "get", "energy_sites/%s/live_status" % siteID, "energy"
            )
            if req is not None and req.status_code == 404 and slot.kind == "owner":
                # Taken as the Owner API not serving live_status (unverified),
                # so the Fleet slot is asked instead
                self._refuseWallConnectors(slot, 404)
                return None
            if outcome is not None:
                if outcome.kind == REFUSED:
                    return None
                continue
            try:
                response = json.loads(req.text)["response"]
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(response, dict):
                continue
            answered = True
            connectors = response.get("wall_connectors")
            if connectors is None:
                # No Wall Connector at this site (a Powerwall alone)
                dropped.add(siteID)
                continue
            for wc in connectors if isinstance(connectors, list) else []:
                if not isinstance(wc, dict):
                    continue
                din, vin = wc.get("din"), wc.get("vin")
                if isinstance(din, str) and din and isinstance(vin, str) and vin:
                    vins[din] = vin
        if all(i in dropped for i in siteIDs):
            # No site has a Wall Connector: not ready until the list is due
            self._energySitesEmpty[slot.kind] = time.time()
        return vins if answered else None

    def getCarApiRetryRemaining(self, vehicle=None, now=None):
        # Calculate the amount of time remaining until the API can be queried
        # again. This is the api backoff time minus the difference between now
        # and the last error time

        # The optional vehicleLast parameter allows passing the last error time
        # for an individual vehicle, rather than the entire API.
        lastError = self.getCarApiLastErrorTime()
        if vehicle:
            lastError = max(vehicle.lastErrorTime, lastError)

        if lastError == 0:
            return 0
        else:
            backoff = self.getCarApiErrorRetryMins(vehicle) * 60
            lasterrortime = (time.time() if now is None else now) - lastError
            if lasterrortime >= backoff:
                return 0
            else:
                logger.log(
                    logging.DEBUG2,
                    "Backoff is "
                    + str(backoff)
                    + ", lasterror delta is "
                    + str(lasterrortime)
                    + ", last error was "
                    + str(lastError),
                )
                return int(backoff - lasterrortime)

    def getVehicleByVIN(self, vin):
        # Returns the vehicle object identified by the given VIN
        if not isinstance(vin, str):
            return False
        for vehicle in self.getCarApiVehicles():
            if str(vehicle.VIN).upper() == vin.upper():
                return vehicle
        return False

    def getVehicleCount(self):
        # Returns the number of currently tracked vehicles
        return int(len(self.carApiVehicles))

    def getCarApiVehicles(self):
        return self.carApiVehicles

    def resetCarApiLastErrorTime(self, vehicle=None):
        self.carApiLastErrorTime = 0
        if vehicle:
            vehicle.lastErrorTime = 0
            vehicle.errorCount = 0
        self.errorCount = 0
        return True

    def usePKCE(self):
        # Public-client PKCE flow is used when no client secret is configured.
        return not bool(self.clientSecret)

    def fleetRegions(self):
        # The Fleet API regions, in regionURL order; never "OwnerAPI".
        return [region for region in self.regionURL if region != "OwnerAPI"]

    def regionAudience(self, region):
        # Confidential-client only: the token audience is the regional FleetAPI
        # base URL without the /api/1/vehicles path suffix used elsewhere.
        if region not in self.fleetRegions():
            region = "NA"
        return self.regionURL[region].replace("/api/1/vehicles", "")

    def loginConfigured(self):
        # Both flows need a client id and redirect URI; the secret is optional
        # (its presence selects the confidential flow).
        return bool(self.refreshClientID and self.redirectURI)

    def teslaLoginInfo(self):
        # Consumed by the web UI to render the FleetAPI login page.
        return {
            "configured": self.loginConfigured(),
            "client_id": self.refreshClientID,
            "redirect_uri": self.redirectURI,
            "pkce": self.usePKCE(),
            "regions": self.fleetRegions(),
            "region": self.region,
        }

    def getLoginURL(self, region=None):
        # Build the authorization URL and remember the state (and PKCE verifier /
        # region) so the later token exchange matches.
        if not self.loginConfigured():
            return ""
        if region not in self.fleetRegions():
            region = self.region
        self.__loginState = secrets.token_urlsafe(16)
        self.__loginRegion = region
        params = {
            "response_type": "code",
            "client_id": self.refreshClientID,
            "redirect_uri": self.redirectURI,
            "scope": self.scope,
            "state": self.__loginState,
        }
        if self.usePKCE():
            self.__loginVerifier = secrets.token_urlsafe(64)
            params["code_challenge"] = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(self.__loginVerifier.encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            params["code_challenge_method"] = "S256"
        else:
            params["audience"] = self.regionAudience(region)
        return self.authorizeURL + "?" + urlencode(params)

    def fleetTokenExchange(self, code, state):
        # Exchange an authorization code for tokens. Returns a status string used
        # by the web UI ("success", "error", or a Tesla error code).
        if self.syncedSlotLocked(self.fleetSlot):
            # TeslaMate owns the Fleet slot. Refuse before the exchange, so
            # nothing is stored and the code is not used up.
            logger.warning(
                "Tesla login refused: TeslaMate token sync manages the Fleet "
                "API token. Remove that token on the Settings page, or turn "
                "off syncTokens, to log in here."
            )
            return "token_sync"
        if not self.loginConfigured():
            logger.error("Tesla FleetAPI login is not configured.")
            return "not_configured"
        if not code:
            logger.error("Tesla login: no authorization code provided.")
            return "error"
        if not state or state != self.__loginState:
            logger.error("Tesla login: state mismatch; please retry the login.")
            return "state_mismatch"

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "authorization_code",
            "client_id": self.refreshClientID,
            "code": code,
            "redirect_uri": self.redirectURI,
        }
        if self.usePKCE():
            # Public client: prove possession of the PKCE verifier; no secret,
            # no audience, exchange at auth.tesla.com.
            data["code_verifier"] = self.__loginVerifier or ""
            tokenURL = self.authTokenURL
        else:
            # Confidential client: secret + regional audience at fleet-auth.
            data["client_secret"] = self.clientSecret
            data["audience"] = self.regionAudience(self.__loginRegion or self.region)
            tokenURL = self.fleetRefreshURL
        # storeToken classifies the token and picks its slot, and the slot
        # the proxy or the region URL for a Fleet token.

        req = None
        try:
            req = requests.post(tokenURL, headers=headers, data=data)
            params = json.loads(req.text)
        except requests.exceptions.RequestException:
            logger.error("Request Exception during Tesla token exchange.")
            return "error"
        except (ValueError, json.decoder.JSONDecodeError):
            logger.error("Could not parse Tesla token exchange response.")
            return "error"

        if "error" in params:
            # Do not log the full response (may echo request detail); log the code.
            logger.log(
                logging.INFO2, "Tesla token exchange error: " + str(params["error"])
            )
            return params["error"]

        if "access_token" in params and "refresh_token" in params:
            result = self.storeToken(
                params["access_token"],
                params["refresh_token"],
                "login",
                time.time() + params.get("expires_in", 8 * 60 * 60),
            )
            if result != "success":
                return result
            self.__loginState = None
            self.__loginVerifier = None
            self.master.queue_background_task({"cmd": "saveSettings"})
            logger.log(logging.INFO2, "Tesla FleetAPI tokens stored successfully.")
            return "success"

        logger.error("Tesla token exchange returned no access token.")
        return "response_no_token"

    def saveApiToken(self, url):
        # Paste-back path: extract code and state from the redirect URL the user
        # copied from their browser, then perform the token exchange.
        if isinstance(url, bytes):
            url = url.decode("UTF-8")
        qs = parse_qs(urlsplit(url).query)
        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]
        if code is None and "?" not in url and not urlsplit(url.strip()).scheme:
            # Not a URL: a bare token, e.g. an Owner API token. (A URL without
            # a query is still a failed login, and must not be stored.)
            return self.savePastedToken(url.strip())
        return self.fleetTokenExchange(code, state)

    def savePastedToken(self, token):
        # Stores a token pasted on its own: an access token in the slot of
        # its type, anything else as a refresh token (which refreshes by its
        # type). Returns a status string for the web UI, as saveApiToken does.
        if not token:
            return "error"
        decoded = self.decodeToken(token)
        tokenType = self.classifyToken(decoded)[0]
        isOwnerRefresh = decoded is not None and (
            decoded.get("iss") == self.ownerTokenIssuer and tokenType != "owner"
        )
        if (
            tokenType == "unknown"
            and decoded is not None
            and decoded.get("iss") != self.ownerTokenIssuer
        ):
            # A JWT that is neither token type: nothing to store, and it
            # is not a refresh token either (Fleet's are opaque).
            self.tokenRoute(token)
            return "unknown_type"
        if tokenType != "unknown" and not isOwnerRefresh:
            # refreshToken=None keeps the slot's refresh token; the other
            # slot is untouched.
            result = self.storeToken(token)
            if result == "success":
                self.master.queue_background_task({"cmd": "saveSettings"})
            return result
        return self.pasteRefreshToken(token)

    def pasteRefreshToken(self, token):
        # A refresh token entered on its own (Settings page or paste-back).
        # It goes to the slot of its type, replacing that slot's tokens, and
        # is refreshed at once. The other slot is untouched.
        # Returns a status for the web UI, as saveApiToken does:
        #   "success";
        #   "token_rejected" or "not_configured": Tesla rejected the token,
        #     or it needs a teslaApiClientID; the previous tokens are kept;
        #   "unreachable": no response from Tesla; the pasted token is kept
        #     and the usual refresh retries it;
        #   "token_sync": refused, nothing changed.
        if not token:
            return "error"
        slot = self.slotOfType(self.refreshTokenType(token))
        if self.syncedSlotLocked(slot):
            self.logSyncedSlotLocked(slot)
            return "token_sync"
        previous = slot.snapshot()
        previousErrors = (self.carApiLastErrorTime, self.errorCount)
        slot.clear()
        slot.refreshToken = token
        slot.source = "pasted"
        # Nothing is saved on failure until the outcome is known, so a
        # queued save never writes the cleared state.
        result = self.refreshSlot(slot, saveOnFailure=False)
        if result == "success":
            return "success"
        if result == "unreachable":
            logger.warning(
                "Could not reach Tesla to check the pasted refresh token; "
                "keeping it and retrying later."
            )
            self.master.queue_background_task({"cmd": "saveSettings"})
            return "unreachable"
        slot.restore(previous)
        self.carApiLastErrorTime, self.errorCount = previousErrors
        self.master.queue_background_task({"cmd": "saveSettings"})
        logger.warning(
            "Pasted Tesla API refresh token not accepted; previous tokens kept."
        )
        return "token_rejected" if result == "rejected" else result

    def decodeToken(self, token):
        return decodeToken(token)

    def syncedSlotLocked(self, slot):
        # True when TeslaMate sync manages this slot's token: sync is on and
        # the slot holds a live synced token. The one place the refusal rule
        # lives; the user replaces such a token by removing it first.
        return bool(
            slot is not None
            and self.master.tokenSyncEnabled()
            and slot.source == "synced"
            and not slot.empty()
        )

    def logSyncedSlotLocked(self, slot):
        logger.warning(
            "Tesla API token not stored: TeslaMate token sync manages the %s "
            "API token. Remove that token on the Settings page, or turn off "
            "syncTokens, to replace it." % slot.kind.capitalize()
        )

    def removeToken(self, kind):
        # Settings page "Remove token": clears the Owner or Fleet slot, a
        # synced one included. False for any other kind.
        slot = self.slotOfType(kind)
        if slot is None:
            return False
        slot.clear()
        self.master.queue_background_task({"cmd": "saveSettings"})
        return True

    def storeToken(
        self, accessToken, refreshToken=None, source="pasted", expireTime=None
    ):
        # Stores tokens in the slot of their type: the access token's, or
        # the refresh token's when there is no access token. refreshToken
        # None keeps the slot's refresh token. source is "synced", "pasted"
        # or "login". Returns "success", "error" (no token), "unknown_type"
        # (nothing stored), "kept" (sync met a pasted or login token) or
        # "token_sync" (the slot holds a live synced token while
        # sync is on) or "mismatched_types" (an access and refresh token
        # of different known types; nothing stored). The caller queues
        # saveSettings.
        if not accessToken and not refreshToken:
            return "error"
        if accessToken:
            tokenType = self.classifyToken(self.decodeToken(accessToken))[0]
        else:
            tokenType = self.refreshTokenType(refreshToken)
        slot = self.slotOfType(tokenType)
        if slot is None:
            if source == "synced":
                logger.error(
                    "TeslaMate token sync: the token's type (Owner API or "
                    "Fleet API) was not recognised; not using it."
                )
            else:
                # Logs the unknown-type ERROR.
                self.tokenRoute(accessToken)
            return "unknown_type"
        if (
            accessToken
            and refreshToken
            and self.refreshTokenTypeKnown(refreshToken)
            and self.refreshTokenType(refreshToken) != tokenType
        ):
            # The wrong-type refresh token would later 401 and clear the
            # valid access token it was stored with.
            logger.error(
                "The access token and refresh token are for different Tesla "
                "APIs (Owner and Fleet); not storing either."
            )
            return "mismatched_types"
        if (
            source == "synced"
            and slot.source == "synced"
            and slot.accessToken == accessToken
        ):
            # An unchanged resync: keep the slot's pause and counters.
            return "success"
        if (
            source == "synced"
            and not slot.empty()
            and slot.source in ("pasted", "login")
        ):
            # Sync never replaces a token the user supplied.
            if slot.changes != self.syncSkipLoggedAt[tokenType]:
                logger.info(
                    "TeslaMate token sync: keeping the %s API token you %s; "
                    "not replacing it."
                    % (
                        "Owner" if tokenType == "owner" else "Fleet",
                        "pasted" if slot.source == "pasted" else "logged in with",
                    )
                )
                self.syncSkipLoggedAt[tokenType] = slot.changes
            return "kept"
        if source != "synced" and self.syncedSlotLocked(slot):
            self.logSyncedSlotLocked(slot)
            return "token_sync"
        slot.setTokens(accessToken, refreshToken, expireTime, source)
        return "success"

    def migrateSavedTokens(self, settings, syncConfigured):
        # One-time move of a saved Owner API token from the carApi* keys,
        # where earlier releases kept their only token, to the ownerApi*
        # keys. Mutates settings; returns True if it changed anything.
        # syncConfigured: TeslaMate's syncTokens is set in config.json,
        # whether or not sync is running (it turns itself off when the
        # database is unreachable at startup). Then a saved Owner or Fleet
        # token counts as synced, as does one saved with the sentinel.
        # carApiTokenSource marks settings already migrated, unless an
        # Owner token or the never-expires sentinel is back in carApi*
        # (written by a downgraded release): those always migrate, so the
        # Fleet slot never holds TeslaMate's refresh token.
        access = settings.get("carApiBearerToken", "") or ""
        refresh = settings.get("carApiRefreshToken", "") or ""
        expireTime = settings.get("carApiTokenExpireTime", 0)
        if not access and not refresh:
            return False
        if access:
            tokenType = self.classifyToken(self.decodeToken(access))[0]
        else:
            tokenType = self.refreshTokenType(refresh)
        legacySynced = expireTime == self.LEGACY_SYNCED_EXPIRY
        if (
            "carApiTokenSource" in settings
            and tokenType != "owner"
            and not legacySynced
        ):
            return False
        before = dict(settings)
        synced = legacySynced or (syncConfigured and tokenType in ("owner", "fleet"))
        if synced:
            # The refresh token is TeslaMate's: never keep it. The token
            # expires at its own exp, not at the sentinel.
            refresh = ""
            decoded = self.decodeToken(access) if access else None
            exp = decoded.get("exp") if decoded else None
            if isinstance(exp, (int, float)) and not isinstance(exp, bool):
                expireTime = exp
            elif legacySynced:
                expireTime = 0
        source = "synced" if synced else "pasted"
        if tokenType == "owner":
            if access or refresh:
                # Nothing is left of a synced refresh-only token, so it
                # doesn't wipe a saved Owner token.
                settings["ownerApiBearerToken"] = access
                settings["ownerApiRefreshToken"] = refresh
                settings["ownerApiTokenExpireTime"] = expireTime
                settings["ownerApiTokenSource"] = source
            settings["carApiBearerToken"] = ""
            settings["carApiRefreshToken"] = ""
            settings["carApiTokenExpireTime"] = 0
            settings["carApiTokenSource"] = ""
        else:
            # Fleet stays (nothing records a Fleet login, so it is never
            # marked "login"). Unknown stays too; restoring the slot logs
            # the unknown-type ERROR.
            settings["carApiRefreshToken"] = refresh
            settings["carApiTokenExpireTime"] = expireTime
            settings["carApiTokenSource"] = source if (access or refresh) else ""
        changed = settings != before
        if changed:
            logger.info(
                "Migrated the saved Tesla API token (%s, %s) to the new settings."
                % (tokenType, source)
            )
        return changed

    def restoreSlot(self, slot, accessToken, refreshToken, expireTime, source="pasted"):
        # Settings-load path (TWCMaster.loadSettings): puts saved tokens back
        # as they were, without policy checks or a refresh. A saved
        # LEGACY_SYNCED_EXPIRY (or no usable expiry) gives way to the
        # token's own exp, or to now when that can't be read. A saved synced
        # token gives way to a slot that is already filled: the startup
        # sync, which runs before loadSettings, put a fresher one there.
        if not accessToken and not refreshToken:
            return
        if source == "synced" and not slot.empty():
            return
        if (
            expireTime == self.LEGACY_SYNCED_EXPIRY
            or not isinstance(expireTime, (int, float))
            or isinstance(expireTime, bool)
        ):
            decoded = self.decodeToken(accessToken) if accessToken else None
            exp = decoded.get("exp") if decoded else None
            if isinstance(exp, (int, float)) and not isinstance(exp, bool):
                expireTime = exp
            else:
                expireTime = time.time()
        slot.setTokens(accessToken, refreshToken, expireTime, source)

    def classifyToken(self, decoded):
        # Returns (tokenType, region) for a token's decoded JWT claims.
        # tokenType is "owner", "fleet" or "unknown"; region is a Fleet region
        # key for "fleet" and None otherwise. An Owner API token can also
        # carry ou_code, so aud is checked for owner-api first.
        if not isinstance(decoded, dict):
            return ("unknown", None)
        # aud may be absent or null (treated as no aud), a string or a list.
        # Any other shape is unexpected, so the token type is unknown.
        aud = decoded.get("aud") or []
        if isinstance(aud, str):
            aud = [aud]
        if not isinstance(aud, list):
            return ("unknown", None)
        aud = [str(entry) for entry in aud]
        if any("owner-api" in entry for entry in aud):
            return ("owner", None)
        ouCode = decoded.get("ou_code", "")
        if ouCode in self.fleetRegions():
            return ("fleet", ouCode)
        if any("fleet-api" in entry for entry in aud):
            for region in self.fleetRegions():
                host = urlsplit(self.regionURL[region]).netloc
                if any(host in entry for entry in aud):
                    return ("fleet", region)
            return ("fleet", "NA")
        return ("unknown", None)

    def refuseCommandIfReadOnly(self, command):
        # True when no listed car can be sent commands. Logs at INFO once per
        # state change (per car when an Owner token is held), makes no HTTP
        # call and records no API error (an error would start the backoff
        # that also blocks reads).
        if not self.readOnly:
            return False
        if self.ownerTokenHeld():
            for vehicle in self.getCarApiVehicles():
                self.refuseCommandFor(vehicle, command)
        elif self.fleetSlot.changes != self.refusalLoggedAt:
            logger.info(
                "Tesla API is read-only (no usable Fleet API token); not sending "
                + command
                + " or any other vehicle command. Commands need a Fleet API "
                "token or the TeslaBLE module."
            )
            self.refusalLoggedAt = self.fleetSlot.changes
        return True

    def refusalState(self, vehicle):
        # What a car's refusal log line depends on; it is logged again only
        # when this changes. Not the owner slot's changes: a resync of the
        # same Owner token is not news.
        return (
            self.fleetSlot.changes,
            vehicle.commandSigning,
            vehicle.ownerCommandsRefused,
        )

    def refuseCommandFor(self, vehicle, command):
        # True when no slot can send this car commands. Like
        # refuseCommandIfReadOnly: no HTTP call, no API error, and one INFO
        # line per state change, naming the car and why.
        if not self.readOnlyFor(vehicle):
            return False
        if not self.ownerTokenHeld():
            # No token can command any car: the module-wide line.
            self.refuseCommandIfReadOnly(command)
            return True
        state = self.refusalState(vehicle)
        if vehicle.refusalLoggedAt == state:
            return True
        vehicle.refusalLoggedAt = state
        signing = vehicle.commandSigning
        if vehicle.ownerCommandsRetired:
            why = "Tesla's Owner API no longer takes commands for it."
        elif vehicle.ownerCommandsRefused:
            why = (
                "Tesla refused an Owner API command for it although it "
                "reported command_signing: off."
            )
        elif signing == "required":
            why = (
                "Tesla reports "
                + vehicle.name
                + " requires signed commands (command_signing: required)."
            )
        elif signing is None:
            why = (
                "Tesla did not report command_signing for it, so it is "
                "treated as read-only."
            )
        else:
            why = (
                "Tesla reports command_signing: "
                + str(signing)
                + " for it, a value TWCManager does not know, so it is "
                "treated as read-only."
            )
        logger.info(
            "Tesla API is read-only for "
            + vehicle.name
            + "; not sending "
            + command
            + " or any other vehicle command to it. "
            + why
            + " Charge commands need a Fleet API token with the Vehicle "
            "Command proxy (teslaProxy) or the TeslaBLE module."
        )
        return True

    def noteOwnerCommandRefused(self, vehicle, req):
        # True when Tesla refused an Owner API command for this car. Two
        # causes: HTTP 403 or an error naming the Vehicle Command Protocol
        # (the car needs signed commands after all), or HTTP 410/412 or
        # "only available on fleetapi" (the Owner API no longer takes
        # commands). Stops Owner API commands to it until TWCManager
        # restarts, and logs that once. Records no API error, which would
        # also back off reads; the caller returns its failure, so
        # VehiclePriority tries the next module.
        if req is None:
            return False
        text = str(getattr(req, "text", "") or "")
        try:
            error = str((json.loads(text) or {}).get("error") or "")
        except (ValueError, AttributeError, TypeError):
            error = ""
        if "Vehicle Command Protocol required" in error:
            retired = False
        elif (
            req.status_code in (410, 412)
            or "only available on fleetapi" in text.lower()
        ):
            retired = True
        elif req.status_code == 403:
            retired = False
        else:
            return False
        vehicle.ownerCommandsRefused = True
        vehicle.ownerCommandsRetired = retired
        vehicle.refusalLoggedAt = self.refusalState(vehicle)
        answer = " (HTTP " + str(req.status_code) + (": " + error if error else "")
        if retired:
            logger.warning(
                "Tesla's Owner API no longer takes commands for "
                + vehicle.name
                + answer
                + "). Not sending it further Owner API commands; charge "
                "commands need a Fleet API token or the TeslaBLE module."
            )
        else:
            logger.warning(
                "Tesla refused an Owner API command for "
                + vehicle.name
                + answer
                + ") although it reported command_signing: off; it needs "
                "signed commands. Not sending it further Owner API commands; "
                "charge commands need a Fleet API token with the Vehicle "
                "Command proxy (teslaProxy) or the TeslaBLE module."
            )
        return True

    def applySyncedToken(self, accessToken):
        # Entry point for token sync sources (TeslaMate): the access token
        # only, so a synced slot never refreshes. Accepted whether or not
        # tokenSyncEnabled() is already true. Returns storeToken's result.
        return self.storeToken(accessToken, "", "synced")

    # Command paths for the providers: each returns an Outcome and makes at
    # most one billed request (two only when an expired token is refreshed
    # in between). Nothing here retries a billed request.

    def command(self, vehicle, name: str, body: Optional[dict] = None) -> Outcome:
        # One Tesla API command (name, e.g. "set_charge_limit") to vehicle,
        # through the slot that may command this car (commandSlotFor)
        slot = self.commandSlotFor(vehicle)
        if slot is None:
            # Says once why this car is read-only
            self.refuseCommandFor(vehicle, name)
            return Outcome(REFUSED, "no command token")
        if self.getCarApiRetryRemaining(vehicle, now=self.now()):
            return Outcome(UNREACHABLE, "backoff")
        outcome, response = self._paidPost(vehicle, slot, "command/" + name, body)
        if outcome is None:
            reason = response.get("reason") or ""
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,40}", reason):
                # Only a short reason code is carried or logged
                reason = "not accepted" if reason else ""
            if response.get("result") is True:
                outcome = Outcome(DONE)
            elif reason in self.COMMAND_SATISFIED_REASONS:
                outcome = Outcome(SATISFIED, reason)
            elif reason in self.COMMAND_UNREACHABLE_REASONS:
                outcome = Outcome(UNREACHABLE, reason)
            else:
                outcome = Outcome(ERROR, reason or "not accepted")
            if outcome.kind in (DONE, SATISFIED):
                # The car answered: it is awake (ready() reads this)
                vehicle.lastAPIAccessTime = self.now()
        logger.log(
            logging.INFO8,
            "Car API cmd %s for %s: %s %s"
            % (name, vehicle.name, outcome.kind, outcome.detail),
        )
        return outcome

    def _paidPost(self, vehicle, slot, path, body=None, purpose="command"):
        # POSTs vehicles/<VIN>/<path> with slot's token. Returns (Outcome,
        # None) when the request failed, or (None, response) for an HTTP 200
        # whose "response" object the caller classifies. purpose ("command"
        # or "wake") is what a refusal retires (_classify).
        req, outcome = self._request(
            slot,
            "post",
            "vehicles/%s/%s" % (vehicle.VIN, path),
            purpose,
            body,
            vehicle=vehicle,
        )
        if outcome is not None:
            return outcome, None
        try:
            response = json.loads(req.text)["response"]
            if not isinstance(response, dict):
                raise TypeError("response")
        except (ValueError, KeyError, TypeError) as e:
            return Outcome(ERROR, type(e).__name__), None
        return None, response

    def wakeDue(self, vehicle, now=None) -> bool:
        # Whether the hold schedule lets a wake_up go to vehicle now: not
        # during the API error backoff, not until wakeDelayMins after the
        # charge need was first seen (the car may start on the TWC's offer),
        # and not within delayNextWakeAttempt of the last wake_up.
        now = self.now() if now is None else now
        if self.getCarApiRetryRemaining(vehicle, now=now):
            return False
        if (
            vehicle.firstChargeNeededTime
            and not self._chargeNeedEnded(vehicle, now)
            and now - vehicle.firstChargeNeededTime < self.wakeDelayMins * 60
        ):
            return False
        if (
            vehicle.delayNextWakeAttempt
            and now - vehicle.lastAPIAccessTime <= vehicle.delayNextWakeAttempt
        ):
            return False
        return True

    def wakeOutcome(self, vehicle, now=None) -> Outcome:
        # Wakes vehicle through the Fleet API, but only after the free
        # online check (vehicles/{vin}, cached 120 s) says it is not online
        # and wakeDue allows it. One wake_up at most.
        now = self.now() if now is None else now
        slot = self.commandSlotFor(vehicle)
        if slot is None:
            self.refuseCommandFor(vehicle, "wake_up")
            return Outcome(REFUSED, "no command token")
        if self.getCarApiRetryRemaining(vehicle, now=now):
            # Told to back off: not even the free check
            return Outcome(UNREACHABLE, "backoff")
        self._askChargeNeed(vehicle, now)
        # The command slot's account, unless it cannot read; the Owner API
        # answers the same free check
        checkSlot = slot if slot.usable("read") else self.slotFor("read")
        if vehicle.is_awake(slot=checkSlot, now=now):
            self._wakeSettled(vehicle)
            return Outcome(SATISFIED, "online")
        if not vehicle.lastVehicleAwakeKnown:
            # No free answer (no read token, an error): never wake blind
            return Outcome(UNREACHABLE, "online check failed")
        if slot is self.fleetSlot and not slot.usable("wake"):
            # Tesla refused a wake with this token (403): no wake_up until
            # the token changes. Checked after the free online check, so an
            # online car still gets its command. An Owner refusal is noted
            # per car instead
            return Outcome(UNREACHABLE, "wake refused")
        if vehicle.firstChargeNeededTime == 0:
            vehicle.firstChargeNeededTime = now
            if self.wakeDelayMins:
                logger.info(
                    vehicle.name
                    + ": deferring wake for "
                    + str(self.wakeDelayMins)
                    + " minutes"
                )
        if not self.wakeDue(vehicle, now):
            return Outcome(UNREACHABLE, "wake held")

        # The wake hold is measured from here
        vehicle.lastAPIAccessTime = now
        outcome, response = self._paidPost(vehicle, slot, "wake_up", purpose="wake")
        if outcome == Outcome(UNREACHABLE, "wake refused"):
            return outcome
        state = "error"
        if outcome is None:
            state = response.get("state", "")
            if not isinstance(state, str):
                state = "error"
        if state == "online":
            self.resetCarApiLastErrorTime()
            self._wakeSettled(vehicle)
            # The wake's answer replaces the cached "asleep": the read that
            # follows it goes out (C1)
            vehicle.lastVehicleAwakeState = vehicle.lastVehicleAwakeChecked = True
            vehicle.lastVehicleAwakeTime = now
            # A command sent at once after a wake can fail with
            # could_not_wake_buses; let the car settle
            time.sleep(5)
            return Outcome(DONE)

        # Every failed wake waits WAKE_RETRY_SECONDS before the next: rapid
        # retries cost $0.02 each and do not help an asleep or offline car
        if vehicle.firstWakeAttemptTime == 0:
            vehicle.firstWakeAttemptTime = now
        vehicle.delayNextWakeAttempt = self.WAKE_RETRY_SECONDS
        if state in ("asleep", "waking", "offline"):
            self.resetCarApiLastErrorTime()
            outcome = Outcome(UNREACHABLE, state)
        elif outcome is None:
            # HTTP 200 with an unexpected state
            self.updateCarApiLastErrorTime(vehicle)
            outcome = Outcome(ERROR, "state")
        logger.info(
            "Car API wake %s failed (%s %s). Will try again in %d seconds."
            % (vehicle.name, outcome.kind, outcome.detail, vehicle.delayNextWakeAttempt)
        )
        if (
            vehicle.firstWakeAttemptTime > 0
            and now - vehicle.firstWakeAttemptTime > 60 * 60
        ):
            # It should never take over an hour to wake a car
            logger.info(
                "ERROR: We have failed to wake %s (%s) for %.1f hours.\n"
                "Please file an issue at https://github.com/ngardiner/TWCManager/."
                % (
                    vehicle.name,
                    outcome.detail,
                    (now - vehicle.firstWakeAttemptTime) / 60 / 60,
                )
            )
        return outcome

    # A charge need (the wakeDelayMins grace, firstChargeNeededTime) has
    # ended when nothing asked for a wake for this long. Ending it can only
    # delay a wake, never bring one forward. A need also ends when the free
    # online check finds the car awake (_wakeSettled); this is the backstop.
    CHARGE_NEED_IDLE_SECONDS = 600

    def _chargeNeedEnded(self, vehicle, now):
        return now - vehicle.lastChargeNeededTime > self.CHARGE_NEED_IDLE_SECONDS

    def _askChargeNeed(self, vehicle, now):
        # A wake is asked for: a need that ended starts again with its grace
        if vehicle.firstChargeNeededTime and self._chargeNeedEnded(vehicle, now):
            vehicle.firstChargeNeededTime = 0
        vehicle.lastChargeNeededTime = now

    @staticmethod
    def _wakeSettled(vehicle):
        # The car is online: the next need starts a new hold schedule
        vehicle.firstWakeAttemptTime = 0
        vehicle.firstChargeNeededTime = 0
        vehicle.delayNextWakeAttempt = 0

    def updateCarApiLastErrorTime(self, vehicle=None):
        timestamp = time.time()
        logger.log(
            logging.INFO8,
            "updateCarApiLastErrorTime() called due to Tesla API Error. Updating timestamp from "
            + str(self.carApiLastErrorTime)
            + " to "
            + str(timestamp),
        )
        if vehicle:
            vehicle.lastErrorTime = timestamp
            vehicle.errorCount += 1
        else:
            self.carApiLastErrorTime = timestamp
            self.errorCount += 1

        return True

    def updateChargeAtHome(self):
        # checkCharge: the vehicle list (free), then each car at home
        self.loadList()
        for car in self.carApiVehicles:
            if car.atHome:
                car.update_charge()
        self.lastChargeCheck = time.time()

    def vehicleScheduledChargingPending(self, vin=None):
        # True if the named (or any at-home) vehicle reports it is waiting on
        # its in-car charging schedule (Scheduled Charging / Departure).
        for vehicle in self.carApiVehicles:
            if vin and vehicle.VIN != vin:
                continue
            if not vin and not vehicle.atHome:
                continue
            if vehicle.scheduledChargingPending:
                return True
        return False

    def vehicleIsDefinitelyHome(self, vin):
        for car in self.carApiVehicles:
            if car.VIN == vin:
                if not car.atHome:
                    # Force a real fetch; the status deferral would otherwise
                    # return cached (possibly stale) coordinates unqueried.
                    car.statusDeferral = 0
                    updated = car.update_location(0)
                    if updated and not car.atHome:
                        logger.log(
                            logging.INFO2,
                            car.name
                            + " was connected to a TWC, so is definitely home.",
                        )
                        if car.gpsAsOf and time.time() - car.gpsAsOf > 600:
                            # The car is home (the TWC reports its VIN), but
                            # its GPS fix is stale - don't move home to
                            # wherever the car last had reception.
                            logger.log(
                                logging.INFO2,
                                "Not resetting home location: "
                                + car.name
                                + "'s GPS fix is stale.",
                            )
                        else:
                            logger.log(
                                logging.INFO2,
                                "Resetting home location to ("
                                + str(car.lat)
                                + ", "
                                + str(car.lon)
                                + ")",
                            )
                            self.master.setHomeLat(car.lat)
                            self.master.setHomeLon(car.lon)
                            self.master.queue_background_task({"cmd": "sunrise"})
                            self.master.queue_background_task({"cmd": "saveSettings"})
                        car.atHome = True
                return

    @property
    def numCarsAtHome(self):
        return len([car for car in self.carApiVehicles if car.atHome])

    @property
    def minBatteryLevelAtHome(self):
        if time.time() - self.lastChargeCheck > self.chargeUpdateInterval:
            self.master.queue_background_task({"cmd": "checkCharge"})
        return min(
            [car.batteryLevel for car in self.carApiVehicles if car.atHome],
            default=10000,
        )

    def updateSettings(self):
        return True


class CarApiVehicle:
    carapi = None
    __config = None
    debuglevel = 0
    ID = None
    VIN = ""

    firstWakeAttemptTime = 0
    firstChargeNeededTime = 0
    lastAPIAccessTime = 0
    delayNextWakeAttempt = 0

    errorCount = 0
    lastErrorTime = 0
    lastVehicleStatusTime = 0
    lastVehicleAwakeTime = 0
    lastVehicleAwakeState = False
    # Whether lastVehicleAwakeState came from an answer (a sync source or
    # vehicles/{vin}), not a failed check
    lastVehicleAwakeKnown = False
    # Whether the cached vehicles/{vin} check (lastVehicleAwakeTime) answered
    lastVehicleAwakeChecked = False
    # When a wake was last asked for (TeslaAPI's charge-need lifetime)
    lastChargeNeededTime = 0
    statusDeferral = 0
    # command_signing from Tesla's vehicle list: Owner API commands go only
    # to cars where it is "off". ownerCommandsRefused is set when Tesla
    # refuses one anyway; ownerCommandsRetired too when the refusal says the
    # Owner API no longer takes commands (410/412, "only available on
    # fleetapi") rather than that the car needs signing. refusalLoggedAt is
    # TeslaAPI.refusalState when the car's read-only line was last logged.
    commandSigning = None
    ownerCommandsRefused = False
    ownerCommandsRetired = False
    refusalLoggedAt = None
    # update_charge reuses a charge reading up to this old (seconds).
    READ_MAX_AGE = 300

    # Per-vehicle fields are views onto the registry Vehicle: reads give the
    # latest observation (or the legacy default), writes observe.
    name = RegistryField("name", "")
    batteryLevel = RegistryField("batteryLevel", 10000)
    chargeLimit = RegistryField("chargeLimit", -1)
    chargingState = RegistryField("chargingState", "Unknown")
    availableCurrent = RegistryField("availableCurrent", 0)
    chargeCurrentRequestMax = RegistryField("chargeCurrentRequestMax", None)
    chargeCurrentRequest = RegistryField("chargeCurrentRequest", None)
    voltage = RegistryField("voltage", 0)
    phases = RegistryField("phases", 0)
    timeToFullCharge = RegistryField("timeToFullCharge", 0.0)
    scheduledChargingPending = RegistryField("scheduledChargingPending", False)
    lat = RegistryField("lat", 10000)
    lon = RegistryField("lon", 10000)
    gpsAsOf = RegistryField("gpsAsOf", 0)
    pluggedIn = RegistryField("pluggedIn", None)
    _actualCurrent = RegistryField("actualCurrent", 0)

    atHome = False
    lastCurrentChangeTime = 0

    # Telemetry (an external module such as TeslaMate) is the sync source
    # while its latest "state" observation is younger than this. Age is
    # liveness-aware: on a connected, healthy feed a car that has sent
    # nothing for hours (asleep) stays telemetry-sourced.
    syncTimeout = 60 * 60
    _vehicle = None

    def __init__(self, json, carapi, config):
        self.carapi = carapi
        self.__config = config
        self.ID = json["id"]
        self.VIN = json["vin"]
        self._bind(carapi.registry.getOrCreate(self.VIN, "TeslaAPI"), listed=True)
        # A cloud account listing is a sighting.
        self.name = json["display_name"] or self.VIN or str(self.ID) or "unknown"
        self.commandSigning = json.get("command_signing")
        self.ownerCommandsRefused = False
        self.ownerCommandsRetired = False

    def _bind(self, vehicle, listed=False):
        # Joins the registry vehicle with both claims. The Owner claim
        # follows this car's command_signing and the Fleet claim the Fleet
        # slot, live, so a token pasted or synced after the listing manages
        # the car without a re-list. A listing attaches this object; a rebind (after a forget)
        # only takes an empty attachment, so a stale object never displaces a
        # newer one.
        if vehicle is not self._vehicle:
            if listed or vehicle.attachment("TeslaAPI") is None:
                vehicle.attach("TeslaAPI", self)
            carapi = self.carapi
            vehicle.claim(
                "OwnerAPI", lambda: carapi.ownerCommandsAllowed(self), "listed"
            )
            vehicle.claim(
                "FleetAPI", lambda: carapi.fleetSlot.usable("command"), "listed"
            )
            self._vehicle = vehicle
        return vehicle

    def _liveVehicle(self):
        # The registry's current vehicle for this VIN, or None if it was
        # forgotten. No side effects: a read is not a sighting.
        return self.carapi.registry.get(self.VIN)

    @property
    def vehicle(self):
        # Resolved on each use, so a forgotten (or re-created) registry
        # vehicle is never written to. The account still lists this car, so
        # a write after a forget rejoins it, as the next listing would.
        vehicle = self._liveVehicle()
        if vehicle is None:
            vehicle = self.carapi.registry.getOrCreate(self.VIN, "TeslaAPI")
        return self._bind(vehicle)

    # Sync state is the latest "state" observation's source and age.

    def _telemetryState(self):
        vehicle = self._liveVehicle()
        if vehicle is None:
            return None, None
        obs = vehicle.latest("state")
        if obs is None or obs.source not in TELEMETRY_SOURCES:
            return obs, None
        return obs, vehicle.age("state")

    @property
    def syncSource(self):
        obs, age = self._telemetryState()
        if age is not None and age <= self.syncTimeout:
            return obs.source
        return "TeslaAPI"

    @property
    def syncTimestamp(self):
        vehicle = self._liveVehicle()
        obs = vehicle.latest("state") if vehicle is not None else None
        return obs.at if obs is not None else 0

    def ready(self):
        if self.carapi.getCarApiRetryRemaining(self):
            # It's been under carApiErrorRetryMins minutes since the car API
            # generated an error on this vehicle. Return that car is not ready.
            logger.log(
                logging.INFO8,
                self.name
                + " not ready because of recent lastErrorTime "
                + str(self.lastErrorTime),
            )
            return False

        if (
            self.firstWakeAttemptTime == 0
            and time.time() - self.lastAPIAccessTime < 2 * 60
        ):
            # If it's been less than 2 minutes since we successfully woke this car, it
            # should still be awake.  No need to check.  It returns to sleep state about
            # two minutes after the last command was issued.
            return True

        # This can check whether the car is online; if so, it will likely stay online for
        # two minutes.
        if self.is_awake():
            self.firstWakeAttemptTime = 0
            return True

        logger.log(
            logging.INFO8,
            self.name + " not ready because it wasn't woken in the last 2 minutes.",
        )
        return False

    def is_awake(self, slot=None, now=None):
        # Free sources only: telemetry, else the vehicle state GET
        # (vehicles/{vin}; not charged on the Fleet API). Never vehicle_data.
        # The newest observation wins: telemetry (any state) is trusted only
        # while fresh and newer than our own last state GET; otherwise use
        # that GET's cached result or GET again.
        # Fresh is the registry's age, so a live feed keeps an old reading
        # fresh; it still loses to a state GET made after it.
        # now (default: the module's clock) measures the 120 s cache, so a
        # caller on a test clock drives it
        now = self.carapi.now() if now is None else now
        obs, age = self._telemetryState()
        if (
            age is not None
            and age <= self.syncTimeout / 4
            and obs.at >= self.lastVehicleAwakeTime
        ):
            self.lastVehicleAwakeKnown = True
            return obs.value in ("online", "charging", "updating", "driving")

        # Don't check more often than every 2 minutes
        if now - self.lastVehicleAwakeTime < 120:
            self.lastVehicleAwakeKnown = self.lastVehicleAwakeChecked
            return self.lastVehicleAwakeState
        result, response = self.get_car_api(
            str(self.VIN), checkReady=False, provesOnline=False, slot=slot
        )
        awakeState = bool(result) and response.get("state", "") == "online"
        self.lastVehicleAwakeState = awakeState
        self.lastVehicleAwakeChecked = bool(result)
        self.lastVehicleAwakeKnown = bool(result)
        self.lastVehicleAwakeTime = now
        return awakeState

    def get_car_api(self, path, checkReady=True, provesOnline=True, slot=None):
        # path is relative to the read slot's base URL, e.g. "<VIN>" or
        # "<VIN>/vehicle_data?endpoints=...". slot (a read source's) overrides
        # slotFor("read").
        if slot is None:
            slot = self.carapi.slotFor("read")
        elif not slot.usable("read"):
            # Cleared or paused since the source chose it
            return False, None
        if slot is None:
            # No usable token
            return False, None

        if checkReady and not self.ready():
            return False, None

        apiResponseDict = {}
        # Up to 3 requests while the car's buses wake or a transient error
        # clears; a resend after a refresh (at most one) is one of them
        sent, refreshed = 0, False
        while sent < 3:
            token = slot.accessToken
            req, outcome = self.carapi._request(
                slot,
                "get",
                "vehicles/" + path,
                "read",
                vehicle=self,
                tries=3 - sent + (not refreshed),
            )
            if outcome is not None:
                # Refused, asleep (408), backed off (429) or never sent
                return False, None
            if slot.accessToken != token:
                sent, refreshed = sent + 1, True
            sent += 1
            try:
                apiResponseDict = json.loads(req.text)
            except ValueError:
                pass
            try:
                # A failed read can answer 200 with, e.g.:
                #   {'response': {'reason': 'could_not_wake_buses', 'result': False}}
                #   {'response': None, 'error': 'vehicle unavailable: ...'}
                response = apiResponseDict["response"]
                if (
                    "reason" in response
                    and response["reason"] == "could_not_wake_buses"
                ):
                    # The car's buses are still waking: retry after 5 seconds
                    time.sleep(5)
                    continue
            except (KeyError, TypeError):
                error = (apiResponseDict or {}).get("error", "")
                if not any(
                    error.startswith(e) for e in self.carapi.carApiTransientErrors
                ):
                    logger.info(
                        "ERROR: Can't access vehicle status for %s.  Will try "
                        "again later." % self.name
                    )
                    self.carapi.updateCarApiLastErrorTime(self)
                    return False, None
                # Transient errors (vehicle unavailable, operation_timedout,
                # etc.) retry after a short delay, without incrementing
                # errorCount into the full exponential backoff (closes #377).
                logger.log(
                    logging.INFO8,
                    "Transient error for %s: %s.  Will retry shortly."
                    % (self.name, error),
                )
                time.sleep(5)
                continue
            if provesOnline:
                self.lastAPIAccessTime = time.time()
            return True, response
        self.carapi.updateCarApiLastErrorTime(self)
        return False, None

    def _readFresh(self, field, maxAge):
        # Reads field through the registry cascade (BLE, then the cloud API;
        # never a wake). Success is a newer observation, one within maxAge,
        # or an API status deferral (which reports stored values unfetched,
        # as update_vehicle_data always has). A stale value handed back after
        # every source failed is a failure.
        vehicle = self.vehicle
        before = vehicle.latest(field)
        obs = vehicle.read(field, maxAge=maxAge)
        if obs is None:
            return None
        # Each observe stores a new Observation, so identity says a reading
        # landed during the read without comparing clocks.
        fetched = obs is not before
        if not (
            fetched or vehicle.age(field) <= maxAge or time.time() < self.statusDeferral
        ):
            return None
        if fetched and obs.source == "TeslaBLE":
            # A BLE reply proves the car awake, as an API reply does in
            # get_car_api.
            self.lastAPIAccessTime = time.time()
        return obs

    def update_location(self, minInterval=0):
        # minInterval 0 forces a fresh read (vehicleIsDefinitelyHome relies
        # on it).
        obs = self._readFresh("lat", minInterval)
        if obs is None:
            return False
        gps = self.vehicle.latest("gpsAsOf")
        # A fix time from an older read than this position says nothing
        # about it.
        gpsAsOf = gps.value if gps is not None and gps.at >= obs.at else 0
        gpsAge = (time.time() - gpsAsOf) if gpsAsOf else None
        self.atHome = self.carapi.is_location_home(self.lat, self.lon, gpsAge)
        return True

    def locate(self):
        # Free: atHome from the latest position any source observed. None
        # when the position or home is unknown (never seeds home)
        if self.lat == 10000 or self.carapi.master.getHomeLatLon()[0] == 10000:
            return None
        self.atHome = self.carapi.is_location_home(self.lat, self.lon)
        return self.atHome

    def update_vehicle_data(self, slot=None):
        path = (
            str(self.VIN)
            + "/vehicle_data?endpoints="
            + "%3B".join(
                ["location_data", "charge_state", "drive_state", "vehicle_state"]
            )
        )

        now = time.time()

        if now < self.statusDeferral:
            return True

        result, response = self.get_car_api(path, slot=slot)

        if result:
            try:
                drive = response["drive_state"]
            except (KeyError, TypeError):
                logger.error(
                    "update_vehicle_data: drive_state missing from API response for %s"
                    % self.VIN
                )
                return False

            try:
                charge = response["charge_state"]
            except (KeyError, TypeError):
                logger.error(
                    "update_vehicle_data: charge_state missing from API response for %s"
                    % self.VIN
                )
                return False

            try:
                self.lat = drive["latitude"]
                self.lon = drive["longitude"]
            except (KeyError, TypeError):
                logger.error(
                    "update_vehicle_data: latitude/longitude missing from drive_state for %s"
                    % self.VIN
                )
                return False

            # Only keys present in the response are observed: a default or a
            # carried-over value is not a fresh TeslaAPI reading.
            gpsAsOf = drive.get("gps_as_of") or 0
            if gpsAsOf:
                self.gpsAsOf = gpsAsOf
            gpsAge = (now - gpsAsOf) if gpsAsOf else None
            self.atHome = self.carapi.is_location_home(self.lat, self.lon, gpsAge)

            try:
                vehicleName = response["vehicle_state"]["vehicle_name"]
            except (KeyError, TypeError):
                vehicleName = None
            if vehicleName:
                self.name = vehicleName

            for key, attr in (
                ("charge_limit_soc", "chargeLimit"),
                ("battery_level", "batteryLevel"),
                ("time_to_full_charge", "timeToFullCharge"),
                ("charger_pilot_current", "availableCurrent"),
                ("charge_current_request_max", "chargeCurrentRequestMax"),
                ("charge_current_request", "chargeCurrentRequest"),
                ("charger_actual_current", "actualCurrent"),
                ("charger_phases", "phases"),
                ("charger_voltage", "voltage"),
                ("charging_state", "chargingState"),
                ("scheduled_charging_pending", "scheduledChargingPending"),
            ):
                if key in charge:
                    setattr(self, attr, charge[key])

            if not self.atHome:
                if self.carapi.is_far_from_home(self.lat, self.lon):
                    # Car is further from home than can be driven in an
                    # hour; no point re-checking sooner than that.
                    self.statusDeferral = now + 3600
                elif (
                    drive.get("shift_state") == "P" or drive.get("shift_state") is None
                ):
                    # Car is not driving, so we can check infrequently. May
                    # be able to sleep.
                    self.statusDeferral = now + 1800
                else:
                    self.statusDeferral = now + 300
            else:
                # Car is at home; need to watch charging
                self.statusDeferral = now + 300

        return result

    def update_charge(self):
        current = self._actualCurrent
        ok = self._readFresh("batteryLevel", self.READ_MAX_AGE) is not None
        if self._actualCurrent != current:
            # A read source observes the field directly, bypassing the
            # actualCurrent setter.
            self.lastCurrentChangeTime = time.time()
        return ok

    @property
    def actualCurrent(self):
        return self._actualCurrent

    @actualCurrent.setter
    def actualCurrent(self, value):
        changed = value != self._actualCurrent
        # Observed even when unchanged: a repeat reading is still fresh.
        self._actualCurrent = value
        if changed:
            self.lastCurrentChangeTime = time.time()
