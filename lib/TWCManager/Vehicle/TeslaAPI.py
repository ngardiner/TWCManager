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
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib3.util.ssl_ import create_urllib3_context
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Vehicle.Presence import homeCars
from TWCManager.Vehicle.Providers.Capability import FLEET_DATA
from TWCManager.Vehicle.Providers.TeslaAPIProviders import (
    FleetAPIProvider,
    FleetCachedProvider,
    OwnerAPIProvider,
)
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimits
from TWCManager.Vehicle.Reconcile import Outcome as O
from TWCManager.Vehicle.Registry.Fields import APPEARANCE, READ_MAX_AGE
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaTokenSlot import TeslaTokenSlot, decodeToken

logger = LoggerFactory.get_logger("TeslaAPI", "Vehicle")

# _classify refreshed an expired token: the request is sent once more
_REFRESHED = O.Outcome(O.DONE, "refreshed")
# Tesla refused an Owner API command or wake for one car: it wants signed
# commands (noteOwnerCommandRefused)
_SIGNING_REQUIRED = O.Outcome(O.REFUSED, "signing required")
# Tesla takes no Owner API commands at all: no Owner command or wake
# goes to any car (noteOwnerCommandsRetired)
_OWNER_RETIRED = O.Outcome(O.REFUSED, "owner commands retired")

# What a Fleet API request costs, by endpoint (vehicles/{vin}/<endpoint>)
_PAID = {"vehicle_data": 0.002, "wake_up": 0.02, "command": 0.001}
_PAID_PATH = re.compile(r"/(vehicle_data|wake_up|command/\w+)$")


class CarApiState:
    """TeslaAPI's own bookkeeping for one car the account lists, attached to
    the car's registry Vehicle as "TeslaAPI": the attachment is the listing.
    The car's data is registry fields, never kept here."""

    statusDeferral = 0  # until then a read reports stored values
    errorCount = 0  # the per-car API error backoff
    lastErrorTime = 0
    lastAPIAccessTime = 0  # the last answer that proved it awake
    firstWakeAttemptTime = 0  # the wake hold (wakeOutcome, wakeDue)
    firstChargeNeededTime = 0
    lastChargeNeededTime = 0
    delayNextWakeAttempt = 0
    online = None  # the free online check: True, False, None (no answer)
    onlineCheckedAt = 0  # ... reused for 120 s from here
    staleBefore = 0  # an online answer from before this is not reused
    # As listed: Owner API commands only for "off". The latch is set
    # by a 403 to one (noteOwnerCommandRefused)
    commandSigning = None
    ownerCommandsRefused = False
    refusalLoggedAt = None  # refusalState when its read-only line was logged
    # vehicle_data_cached answered 412 for this car: not asked again while the
    # Fleet token is the one that was refused (cachedRefusedAt: its changes)
    cachedUnsupported = False
    cachedRefusedAt = None


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
    # After a wake_up that answered asleep/offline/waking, the free online
    # check every WAKE_POLL_SECONDS, at most WAKE_POLLS times and for at most
    # their product in seconds (a slow GET retries), before it has failed
    WAKE_POLL_SECONDS = 5
    WAKE_POLLS = 9
    regionURL = {
        "NA": "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles",
        "EU": "https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/vehicles",
        "CN": "https://fleet-api.prd.cn.vn.cloud.tesla.cn/api/1/vehicles",
        # Not a Fleet region: the Owner API used by TeslaMate tokens. It reads,
        # and commands only a car that takes unsigned commands.
        # fleetRegions() leaves it out of every region list and lookup.
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
    vehicleListLock = RLock()
    config = None
    master = None
    errorCount = 0
    maxLoginRetries = 10
    params = None
    fleetRefreshURL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
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
        # wakeOutcome, the online check's cache): a test replaces it instead
        # of the process-wide time.time
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
        return [
            OwnerAPIProvider(self),
            FleetAPIProvider(self),
            FleetCachedProvider(self),
        ]

    def addVehicle(self, json, slot=None):
        # A listed car joins the registry with this module's record attached;
        # the listing slot (default: the read slot) is the sighting's source
        if "vin" not in json:
            return
        source = self.sourceOf(slot or self.slotFor("read"))
        state = CarApiState()
        state.commandSigning = json.get("command_signing")
        try:
            vehicle = self.registry.getOrCreate(
                json["vin"], source, attach=("TeslaAPI", state)
            )
        except ValueError:
            # The registry refuses a missing or malformed VIN.
            logger.debug("Skipping a listed vehicle without a usable VIN")
            return
        # Each claim follows its slot live, so a token pasted or synced after
        # the listing manages the car without a re-list; the Owner claim also
        # follows this car's signing
        vehicle.claim("OwnerAPI", lambda: self.ownerCommandsAllowed(state), "listed")
        vehicle.claim("FleetAPI", lambda: self.fleetSlot.usable("command"), "listed")
        # A cloud account listing is a sighting.
        vehicle.observe("name", json["display_name"] or vehicle.vin, source)

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
        # The slot runs one refresh at a time and shares its result, so a
        # single-use refresh token is spent once.
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
            sent = slot.changes
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
                return None, O.Outcome(O.ERROR, type(e).__name__)
            tries -= 1
            logger.log(
                logging.INFO8,
                "Car API %s %s: HTTP %s" % (method, path, req.status_code),
            )
            paid = _PAID_PATH.search(path.split("?")[0])
            if paid and slot is self.fleetSlot:
                if vehicle is not None:
                    # The cloud worker and the background thread both count
                    with vehicle._lock:
                        vehicle.paidRequests += 1
                # Never the URL, which holds the VIN
                kind = paid.group(1).replace("/", ":")
                price = _PAID[kind.split(":")[0]]
                logger.info(
                    "Tesla %s paid request: %s ($%.3f)" % (slot.tokenType, kind, price)
                )
                vin = vehicle.vin if vehicle else None
                self.master.fleetSpend.request(vin, kind, price)
            outcome = self._classify(
                slot, req.status_code, purpose, req.text, vehicle, tries > 0, sent
            )
            if outcome is not _REFRESHED:
                return req, outcome
            tries = min(tries - 1, 1)
            if tries < 1:
                return req, O.Outcome(O.REFUSED, "401")

    def _classify(
        self, slot, status, purpose, body, vehicle=None, refresh=True, sent=None
    ):
        # What an answer means, acted on: None for an HTTP 200 (unless it is an
        # Owner command's refusal), else an Outcome. purpose is "read", "list"
        # (the vehicle list), "command", "wake", "energy" (the Wall Connector
        # VIN read), "config" (telemetryConfig) or "cached" (read_cached).
        # A cached read's 412 latches that car alone and any other refusal
        # nothing: the slot and the API backoff stay as they were. Energy refusals latch that
        # read alone, config ones nothing: Storm Watch and vehicle reads keep
        # the token. Elsewhere a 401 refreshes an expired token once
        # (_REFRESHED: send again) and otherwise retires the slot; 403 refuses
        # commands or wakes and pauses reads for an hour (an Owner command's
        # 403 latches that car alone); 412 retires commands, or latches reads
        # until the token changes; 410 pauses reads; an Owner command's 410 or
        # 412 retires Owner commands (ownerRefusal); 429 backs off; 408 is an
        # asleep or unreachable car. None of these touches the API backoff but
        # 429, so the next poll can use the other slot at once.
        paid = purpose in ("command", "wake")
        owner = paid and slot is self.ownerSlot and vehicle is not None
        if status == 200:
            # Tesla can refuse an Owner command in a 200's error text
            return self.ownerRefusal(vehicle, status, body) if owner else None
        if purpose == "cached" and status != 401:
            if status == 412 and vehicle is not None:
                car = vehicle.attachment("TeslaAPI")
                car.cachedUnsupported = True
                car.cachedRefusedAt = slot.changes
            return O.Outcome(O.REFUSED if status == 412 else O.ERROR, str(status))
        if purpose in ("energy", "config"):
            if purpose == "config" or status not in (401, 403, 410, 412):
                return O.Outcome(O.ERROR, str(status))
            self._refuseWallConnectors(slot, status)
            return O.Outcome(O.REFUSED, str(status))
        label = slot.kind.capitalize()
        if status == 401 and sent is not None and slot.changes != sent:
            # Another thread refreshed, replaced or cleared the token (sent:
            # slot.changes when the request went out) while the request was
            # out: the 401 was for the old one, so the new one is sent once,
            # and a cleared slot sends nothing
            return _REFRESHED if slot.accessToken else O.Outcome(O.REFUSED, "401")
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
                    return O.Outcome(O.UNREACHABLE, "token refresh")
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
                return O.Outcome(O.REFUSED, "401")
            # Retrying a bad token might make Tesla block remote access to
            # the car: nothing more is sent with it until the user supplies
            # fresh tokens
            if not slot.clear(sent):
                # Changed since the check above: not the token refused
                return _REFRESHED if slot.accessToken else O.Outcome(O.REFUSED, "401")
            logger.error(
                "Tesla %s API token was rejected (HTTP 401); removing it." % label
            )
            self.master.queue_background_task({"cmd": "saveSettings"})
            return O.Outcome(O.REFUSED, "401")
        if owner:
            refused = self.ownerRefusal(vehicle, status, body)
            if refused is not None:
                return refused
        if paid and status == 403:
            slot.reportScopeRefused(purpose, refused=True)
            if purpose == "wake":
                # Not REFUSED, which would exclude Fleet for commands too
                return O.Outcome(O.UNREACHABLE, "wake refused")
            return O.Outcome(O.REFUSED, "403")
        if paid and status == 412:
            # A token kind Tesla will keep refusing commands (e.g. a
            # registration it lacks): no paid command or wake is sent with
            # it again until it changes. Its reads are untouched.
            logger.error(
                "Tesla %s API refused a command (HTTP 412); not sending commands "
                "with this token until it is replaced." % label
            )
            slot.refuse("command")
            return O.Outcome(O.REFUSED, "412")
        if not paid and status in (403, 410, 412):
            # A 412 ("only available on fleetapi") will keep coming, so it
            # holds until the token changes
            hold = "until it is replaced" if status == 412 else "for an hour"
            logger.error(
                "Tesla %s API refused a read (HTTP %d); not using that token "
                "for reads %s." % (label, status, hold)
            )
            slot.pause(latch=status == 412)
            return O.Outcome(O.REFUSED, str(status))
        if status == 408:
            return O.Outcome(O.UNREACHABLE, "408")
        if status == 429:
            # Explicitly told to back off
            car = vehicle.attachment("TeslaAPI") if vehicle else None
            target = car or self
            target.errorCount = max(30, target.errorCount)
            self.updateCarApiLastErrorTime(car)
        return O.Outcome(O.ERROR, str(status))

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
        # The slot a request of this purpose ("read" or "energy") uses, or
        # None when no slot is usable for it. Commands and wakes go to one
        # car, so each provider asks canCommand for its own slot.
        for slot in (self.ownerSlot, self.fleetSlot):
            if slot.usable(purpose):
                return slot
        return None

    def sourceOf(self, slot):
        # The registry source a slot's data is observed as: its provider's
        return "FleetAPI" if slot is self.fleetSlot else "OwnerAPI"

    @property
    def readOnly(self):
        # True when no listed car can be sent commands. With an Owner token
        # and no cars listed yet it is False: whether the token can command
        # is only known once they are listed. Commands still check each car.
        if self.fleetSlot.usable("command"):
            return False
        attached = self.registry.attached("TeslaAPI")
        if not attached:
            return not self.ownerTokenHeld()
        return not any(self.ownerCommandsAllowed(car) for _, car in attached)

    @property
    def ownerTokenOnly(self):
        # True when only an Owner API token is held for commands, so which
        # cars can be commanded depends on each car's command_signing
        return not self.fleetSlot.usable("command") and self.ownerTokenHeld()

    def canCommand(self, vehicle, slot):
        # Whether slot (a provider's own) may send vehicle (a registry
        # Vehicle) a command or wake: the Owner slot only for a car that
        # takes unsigned commands, any other while its token commands. The
        # provider order picks between them; neither sends the other's token
        if slot is self.ownerSlot:
            return self.ownerCommandsAllowed(vehicle.attachment("TeslaAPI"))
        return slot.usable("command")

    def ownerTokenHeld(self):
        # True when the Owner slot holds a live Owner API token. A read
        # pause does not count: it is for reads only
        return not self.ownerSlot.empty() and self.ownerSlot.tokenType == "owner"

    def ownerCommandsAllowed(self, car):
        # True when the Owner token may command the car (its CarApiState):
        # the token is not refused commands (an expired one that could not
        # refresh, a 412), Tesla lists the car with command_signing exactly
        # "off", and no Owner command to it has come back 403 since.
        # "required", no field, or any other value is read-only: fail closed
        return car is not None and self.ownerReadOnlyReason(car) is None

    def ownerReadOnlyReason(self, car):
        # Why the Owner token may not command the car, for the Vehicles page;
        # None when it may.
        if car is None or not self.ownerTokenHeld():
            return "no Owner API token"
        if "command" in self.ownerSlot.refusedPurposes:
            return "Tesla refused commands with the token"
        if car.commandSigning != "off":
            return "car requires signed commands"
        return "car refused an unsigned command" if car.ownerCommandsRefused else None

    def fleetReadOnlyReason(self):
        slot = self.fleetSlot
        if slot.empty() or slot.tokenType != "fleet":
            return "no Fleet API token"
        if "command" in slot.refusedPurposes:
            return "Tesla refused commands"
        return "token lacks command scope"

    def loadVehicleList(self):
        # List the cars (attach their records) from the vehicle list of the read slot
        # (Owner API /products, else the Fleet API vehicle list). Returns True
        # if vehicles are (now) known, False if the fetch failed or no slot is
        # usable for reads. Safe to call from any thread; concurrent callers
        # will not double-add vehicles.
        with self.vehicleListLock:
            if self.registry.attached("TeslaAPI"):
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
                self.addVehicle(car, slot)
            self.resetCarApiLastErrorTime()
        # Outside the lock: its other slot's GET never holds it
        self.resolveHeldLimits(slot, listed)
        return True

    def listPending(self) -> bool:
        # The vehicle list is not loaded, or held limits wait on it
        held = getattr(self.master, "heldLimits", None)
        return not self.registry.attached("TeslaAPI") or (
            isinstance(held, HeldLimits) and held.waiting()
        )

    def loadList(self):
        # The vehicle list (a free call) loads here, and held limits waiting
        # on it resolve: the cloud providers serve only listed cars
        if self.registry.attached("TeslaAPI"):
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
        # defers it to the next check
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

    def telemetryConfig(self, vin):
        # The car's fleet_telemetry_config answer with the Fleet token (free,
        # never wakes the car); None on any failure
        path = "vehicles/%s/fleet_telemetry_config" % vin
        req, outcome = self._request(self.fleetSlot, "get", path, "config")
        try:
            return None if outcome else json.loads(req.text)["response"]
        except (ValueError, KeyError, TypeError):
            return None

    def _vehicleList(self, slot):
        # One free GET of the slot's vehicle list: the listed cars; None when
        # the slot refused (it is cleared or paused, with no API error
        # backoff, so the next poll can use the other slot at once); False on
        # any other failure. The Owner API does not list vehicles under
        # /vehicles (it answers 412 "only available on fleetapi"); /products
        # still does, with energy sites that addVehicle skips (no VIN).
        path = "products" if slot.tokenType == "owner" else "vehicles"
        # After a refresh the list is not sent again: the next poll is
        req, outcome = self._request(slot, "get", path, "list", tries=2)
        if outcome is not None:
            return None if outcome.kind == O.REFUSED else False
        try:
            listed = json.loads(req.text)
            cars = listed["response"][: listed["count"]]
            return [car for car in cars if isinstance(car, dict)]
        except (ValueError, KeyError, TypeError):
            # Not JSON, or no 'response' or 'count' in the answer
            return False

    def getCarApiErrorRetryMins(self, car=None):
        errorCount = self.errorCount
        if car:
            errorCount = max(car.errorCount, errorCount)
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
                if outcome.kind == O.REFUSED:
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

    def getCarApiRetryRemaining(self, car=None, now=None):
        # Calculate the amount of time remaining until the API can be queried
        # again. This is the api backoff time minus the difference between now
        # and the last error time

        # The optional car (a CarApiState) adds that car's last error time
        # to the entire API's.
        lastError = self.getCarApiLastErrorTime()
        if car:
            lastError = max(car.lastErrorTime, lastError)

        if lastError == 0:
            return 0
        else:
            backoff = self.getCarApiErrorRetryMins(car) * 60
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

    def resetCarApiLastErrorTime(self, car=None):
        self.carApiLastErrorTime = 0
        if car:
            car.lastErrorTime = 0
            car.errorCount = 0
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

    def noteReadOnly(self):
        # The INFO hint when no token can command any car, once per Fleet
        # slot change: no HTTP call, no API error (an error would start the
        # backoff that also blocks reads). With an Owner token each car says
        # why instead (refuseCommandFor)
        if (
            not self.readOnly
            or self.ownerTokenHeld()
            or self.fleetSlot.changes == self.refusalLoggedAt
        ):
            return
        self.refusalLoggedAt = self.fleetSlot.changes
        logger.info(
            "Tesla API is read-only (no usable Fleet API token); not sending "
            "vehicle commands. Commands need a Fleet API token or the "
            "TeslaBLE module."
        )

    def refusalState(self, car):
        # What a car's refusal line depends on; it is logged again only when
        # this changes. Not the Owner slot's changes: a resync of the same
        # Owner token is not news
        return (
            self.fleetSlot.changes,
            car.commandSigning,
            car.ownerCommandsRefused,
            "command" in self.ownerSlot.refusedPurposes,
        )

    def refuseCommandFor(self, vehicle, command):
        # True when no slot can send vehicle commands. No HTTP call, no API
        # error, and one INFO line per state change, naming the car and why
        if self.canCommand(vehicle, self.fleetSlot) or self.canCommand(
            vehicle, self.ownerSlot
        ):
            return False
        if not self.ownerTokenHeld():
            # No token can command any car: the module-wide line
            self.noteReadOnly()
            return True
        car = vehicle.attachment("TeslaAPI")
        state = self.refusalState(car)
        if car.refusalLoggedAt == state:
            return True
        car.refusalLoggedAt = state
        if "command" in self.ownerSlot.refusedPurposes:
            why = (
                "Tesla refused commands with the Owner API token (it no longer "
                "accepts Owner API commands, or the token expired and could "
                "not be refreshed)."
            )
        elif car.ownerCommandsRefused:
            why = (
                "Tesla refused an Owner API command for it although it "
                "reported command_signing: off."
            )
        elif car.commandSigning == "required":
            why = (
                "Tesla reports %s requires signed commands (command_signing: "
                "required)." % vehicle.name
            )
        else:
            why = (
                "Tesla did not report command_signing: off for it, so it is "
                "treated as read-only."
            )
        logger.info(
            "Tesla API is read-only for %s; not sending %s or any other vehicle "
            "command to it. %s Charge commands need a Fleet API token with the "
            "Vehicle Command proxy (teslaProxy) or the TeslaBLE module."
            % (vehicle.name, command, why)
        )
        return True

    def ownerRefusal(self, vehicle, status, body):
        # An Owner API command's or wake's answer (any status, 200 included)
        # that refuses it, acted on: Tesla takes no Owner commands at all
        # (410 Gone, 412, or "only available on fleetapi" anywhere in the
        # body), or this car needs signed ones. None for any other answer
        if status in (410, 412) or "only available on fleetapi" in (
            str(body or "").lower()
        ):
            return self.noteOwnerCommandsRetired(status)
        return self.noteOwnerCommandRefused(vehicle, status, body)

    def noteOwnerCommandsRetired(self, status):
        # A token-wide signal, unlike a car's 403: no Owner command or wake
        # goes to any car until the Owner token is stored again (setTokens;
        # a TeslaMate resync tries once more). Reads keep the token. Never
        # the API error backoff. One line, which stands for every car's
        if "command" not in self.ownerSlot.refusedPurposes:
            self.ownerSlot.refuse("command")
            for _, car in self.registry.attached("TeslaAPI"):
                car.refusalLoggedAt = self.refusalState(car)
            if self.fleetSlot.usable("command"):
                # Nothing is lost: the Fleet token carries the commands
                carrier = "The Fleet API carries charge commands from here."
            else:
                carrier = (
                    "Charge commands need a Fleet API token with the Vehicle "
                    "Command proxy (teslaProxy) or the TeslaBLE module."
                )
            logger.error(
                "Tesla no longer accepts Owner API commands (HTTP %d). Not "
                "sending commands or wakes with the Owner API token until it is "
                "entered or synced again; reads continue with it. %s"
                % (status, carrier)
            )
        return _OWNER_RETIRED

    def noteOwnerCommandRefused(self, vehicle, status, body):
        # An Owner API command or wake for a car listed with signing "off"
        # came back HTTP 403, or with an error naming the Vehicle Command
        # Protocol: the car needs signed commands after all. No Owner
        # command goes to it again until TWCManager restarts, logged once.
        # Never the API error backoff, which would also stop reads, and
        # never a slot-wide refusal: the other cars keep their commands.
        # Returns the refusal, or None for any other answer. Tesla's error
        # text is matched, never logged
        try:
            error = str((json.loads(body) or {}).get("error") or "")
        except (ValueError, AttributeError, TypeError):
            error = ""
        if status != 403 and "Vehicle Command Protocol required" not in error:
            return None
        car = vehicle.attachment("TeslaAPI")
        if car is not None and not car.ownerCommandsRefused:
            car.ownerCommandsRefused = True
            car.refusalLoggedAt = self.refusalState(car)
            logger.warning(
                "Tesla refused an Owner API command for %s (HTTP %d) although "
                "it reported command_signing: off. Not sending it further "
                "Owner API commands; charge commands need a Fleet API token "
                "with the Vehicle Command proxy (teslaProxy) or the TeslaBLE "
                "module." % (vehicle.name, status)
            )
        return _SIGNING_REQUIRED

    def applySyncedToken(self, accessToken):
        # Entry point for token sync sources (TeslaMate): the access token
        # only, so a synced slot never refreshes. Accepted whether or not
        # tokenSyncEnabled() is already true. Returns storeToken's result.
        return self.storeToken(accessToken, "", "synced")

    # Command paths for the providers: each returns an Outcome and makes at
    # most one billed request (two only when an expired token is refreshed
    # in between). Nothing here retries a billed request.

    def command(
        self, vehicle, name: str, body: Optional[dict] = None, *, slot
    ) -> O.Outcome:
        # One command (name, e.g. "set_charge_limit") to vehicle with slot,
        # the provider's own, when canCommand lets it
        car = vehicle.attachment("TeslaAPI")
        if not self.canCommand(vehicle, slot):
            return O.Outcome(O.REFUSED, "no command token")
        if self.getCarApiRetryRemaining(car, now=self.now()):
            return O.Outcome(O.UNREACHABLE, "backoff")
        outcome, response = self._paidPost(vehicle, slot, "command/" + name, body)
        if outcome is None:
            reason = response.get("reason") or ""
            if not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,40}", reason):
                # Only a short reason code is carried or logged
                reason = "not accepted" if reason else ""
            if response.get("result") is True:
                outcome = O.Outcome(O.DONE)
            elif reason in self.COMMAND_SATISFIED_REASONS:
                outcome = O.Outcome(O.SATISFIED, reason)
            elif reason in self.COMMAND_UNREACHABLE_REASONS:
                outcome = O.Outcome(O.UNREACHABLE, reason)
            else:
                outcome = O.Outcome(O.ERROR, reason or "not accepted")
            if outcome.kind in (O.DONE, O.SATISFIED):
                # The car answered: it is awake (_ready reads this)
                car.lastAPIAccessTime = self.now()
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
            "vehicles/%s/%s" % (vehicle.vin, path),
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
            return O.Outcome(O.ERROR, type(e).__name__), None
        return None, response

    def wakeDue(self, car, now=None) -> bool:
        # Whether the hold schedule lets a wake_up go to the car now: not
        # during the API error backoff, not until wakeDelayMins after the
        # charge need was first seen (the car may start on the TWC's offer),
        # and not within delayNextWakeAttempt of the last wake_up.
        now = self.now() if now is None else now
        if self.getCarApiRetryRemaining(car, now=now):
            return False
        if (
            car.firstChargeNeededTime
            and not self._chargeNeedEnded(car, now)
            and now - car.firstChargeNeededTime < self.wakeDelayMins * 60
        ):
            return False
        if (
            car.delayNextWakeAttempt
            and now - car.lastAPIAccessTime <= car.delayNextWakeAttempt
        ):
            return False
        return True

    def wakeOutcome(self, vehicle, now=None, *, slot) -> O.Outcome:
        # Wakes vehicle with slot (as for command), but only after the free
        # online check (vehicles/{vin}, cached 120 s) says it is not online
        # and wakeDue allows it. One wake_up at most.
        now = self.now() if now is None else now
        car = vehicle.attachment("TeslaAPI")
        if not self.canCommand(vehicle, slot):
            return O.Outcome(O.REFUSED, "no command token")
        if self.getCarApiRetryRemaining(car, now=now):
            # Told to back off: not even the free check
            return O.Outcome(O.UNREACHABLE, "backoff")
        self._askChargeNeed(car, now)
        # The command slot's account, unless it cannot read; the Owner API
        # answers the same free check
        checkSlot = slot if slot.usable("read") else self.slotFor("read")
        awake = self.onlineState(vehicle, slot=checkSlot, now=now)
        if awake:
            self._wakeSettled(car)
            return O.Outcome(O.SATISFIED, "online")
        if awake is None:
            # No free answer (no read token, an error): never wake blind
            return O.Outcome(O.UNREACHABLE, "online check failed")
        if slot is self.fleetSlot and not slot.usable("wake"):
            # Tesla refused a wake with this token (403): no wake_up until
            # the token changes. Checked after the free online check, so an
            # online car still gets its command. An Owner 403 latches the
            # car instead (ownerCommandsAllowed)
            return O.Outcome(O.UNREACHABLE, "wake refused")
        if car.firstChargeNeededTime == 0:
            car.firstChargeNeededTime = now
            if self.wakeDelayMins:
                logger.info(
                    vehicle.name
                    + ": deferring wake for "
                    + str(self.wakeDelayMins)
                    + " minutes"
                )
        if not self.wakeDue(car, now):
            return O.Outcome(O.UNREACHABLE, "wake held")

        # The wake hold is measured from here
        car.lastAPIAccessTime = now
        outcome, response = self._paidPost(vehicle, slot, "wake_up", purpose="wake")
        if outcome in (
            O.Outcome(O.UNREACHABLE, "wake refused"),
            _SIGNING_REQUIRED,
            _OWNER_RETIRED,
        ):
            return outcome
        state = "error"
        if outcome is None:
            state = response.get("state", "")
            if not isinstance(state, str):
                state = "error"
        if state in ("asleep", "waking", "offline"):
            # wake_up answers the state at that moment; the car connects
            # seconds later. Blocking ~45 s is fine: this is the cloud worker
            deadline = time.monotonic() + self.WAKE_POLLS * self.WAKE_POLL_SECONDS
            for _ in range(self.WAKE_POLLS):
                if time.monotonic() >= deadline:
                    break
                time.sleep(self.WAKE_POLL_SECONDS)
                if self.onlineState(vehicle, slot=checkSlot, maxAge=0):
                    state = "online"
                    break
        if state == "online":
            self.resetCarApiLastErrorTime()
            self._wakeSettled(car)
            # The wake's or a poll's answer replaces the cached "asleep": the
            # read that follows it goes out
            car.online, car.onlineCheckedAt = True, max(now, car.onlineCheckedAt)
            # A command sent at once after a wake can fail with
            # could_not_wake_buses; let the car settle
            time.sleep(5)
            return O.Outcome(O.DONE)

        # Every failed wake waits WAKE_RETRY_SECONDS before the next: rapid
        # retries cost $0.02 each and do not help an asleep or offline car
        if car.firstWakeAttemptTime == 0:
            car.firstWakeAttemptTime = now
        car.delayNextWakeAttempt = self.WAKE_RETRY_SECONDS
        if state in ("asleep", "waking", "offline"):
            self.resetCarApiLastErrorTime()
            outcome = O.Outcome(O.UNREACHABLE, state)
        elif outcome is None:
            # HTTP 200 with an unexpected state
            self.updateCarApiLastErrorTime(car)
            outcome = O.Outcome(O.ERROR, "state")
        logger.info(
            "Car API wake %s failed (%s %s). Will try again in %d seconds."
            % (vehicle.name, outcome.kind, outcome.detail, car.delayNextWakeAttempt)
        )
        if car.firstWakeAttemptTime > 0 and now - car.firstWakeAttemptTime > 60 * 60:
            # It should never take over an hour to wake a car
            logger.info(
                "ERROR: We have failed to wake %s (%s) for %.1f hours.\n"
                "Please file an issue at https://github.com/ngardiner/TWCManager/."
                % (
                    vehicle.name,
                    outcome.detail,
                    (now - car.firstWakeAttemptTime) / 60 / 60,
                )
            )
        return outcome

    # A charge need (the wakeDelayMins grace, firstChargeNeededTime) has
    # ended when nothing asked for a wake for this long. Ending it can only
    # delay a wake, never bring one forward. A need also ends when the free
    # online check finds the car awake (_wakeSettled); this is the backstop.
    CHARGE_NEED_IDLE_SECONDS = 600

    def _chargeNeedEnded(self, car, now):
        return now - car.lastChargeNeededTime > self.CHARGE_NEED_IDLE_SECONDS

    def _askChargeNeed(self, car, now):
        # A wake is asked for: a need that ended starts again with its grace
        if car.firstChargeNeededTime and self._chargeNeedEnded(car, now):
            car.firstChargeNeededTime = 0
        car.lastChargeNeededTime = now

    @staticmethod
    def _wakeSettled(car):
        # The car is online: the next need starts a new hold schedule
        car.firstWakeAttemptTime = 0
        car.firstChargeNeededTime = 0
        car.delayNextWakeAttempt = 0

    def updateCarApiLastErrorTime(self, car=None):
        timestamp = time.time()
        logger.log(
            logging.INFO8,
            "updateCarApiLastErrorTime() called due to Tesla API Error. Updating timestamp from "
            + str(self.carApiLastErrorTime)
            + " to "
            + str(timestamp),
        )
        if car:
            car.lastErrorTime = timestamp
            car.errorCount += 1
        else:
            self.carApiLastErrorTime = timestamp
            self.errorCount += 1

        return True

    @property
    def numCarsAtHome(self):
        return len(homeCars(self.master, "TeslaAPI"))

    @property
    def minBatteryLevelAtHome(self):
        cars = homeCars(self.master, "TeslaAPI")
        return min([v.value("batteryLevel", 10000) for v, _ in cars] or [10000])

    # One listed car's reads: vehicle is its registry Vehicle, car its
    # CarApiState. Field reads and writes go through the registry.

    def _ready(self, vehicle, car) -> bool:
        if self.getCarApiRetryRemaining(car):
            # It's been under carApiErrorRetryMins minutes since the car API
            # generated an error on this vehicle. Return that car is not ready.
            logger.log(
                logging.INFO8,
                vehicle.name
                + " not ready because of recent lastErrorTime "
                + str(car.lastErrorTime),
            )
            return False

        if (
            car.firstWakeAttemptTime == 0
            and time.time() - car.lastAPIAccessTime < 2 * 60
        ):
            # If it's been less than 2 minutes since we successfully woke this car, it
            # should still be awake.  No need to check.  It returns to sleep state about
            # two minutes after the last command was issued.
            return True

        # This can check whether the car is online; if so, it will likely stay online for
        # two minutes.
        if self.is_awake(vehicle):
            car.firstWakeAttemptTime = 0
            return True

        logger.log(
            logging.INFO8,
            vehicle.name + " not ready because it wasn't woken in the last 2 minutes.",
        )
        return False

    def onlineState(self, vehicle, slot=None, now=None, maxAge=120) -> Optional[bool]:
        # True or False when an answer (a sync source or the free
        # vehicles/{vin}) says so; None when nothing answered. Free sources
        # only, never vehicle_data. The newest observation wins: telemetry
        # (any state) is trusted only while its feed is live, fresh and newer
        # than our own last state GET; otherwise use that GET's cached result
        # or GET again. A feed that is down only repeats its last report.
        # Fresh is the registry's age, so a live feed keeps an old reading
        # fresh; it still loses to a state GET made after it. now (default:
        # the module's clock) measures the maxAge-second cache, so a caller
        # on a test clock drives it
        car = vehicle.attachment("TeslaAPI")
        now = self.now() if now is None else now
        obs, age = vehicle.telemetryState()
        if (
            age is not None
            and age <= vehicle.SYNC_TIMEOUT / 4
            and obs.at >= max(car.onlineCheckedAt, car.staleBefore)
            and isinstance(self.registry.sourceLiveAt(obs.source, vehicle), float)
        ):
            return obs.value in ("online", "charging", "updating", "driving")

        # Not more often than maxAge (120 s by default)
        age = now - car.onlineCheckedAt
        if age < maxAge and car.onlineCheckedAt >= car.staleBefore:
            return car.online
        result, response = self.get_car_api(
            vehicle, vehicle.vin, checkReady=False, slot=slot
        )
        car.online = response.get("state", "") == "online" if result else None
        car.onlineCheckedAt = now
        if result:
            source = self.sourceOf(slot or self.slotFor("read"))
            vehicle.observe("state", response.get("state"), source)
        return car.online

    def is_awake(self, vehicle, slot=None, now=None) -> bool:
        # False for asleep and for no answer alike
        return bool(self.onlineState(vehicle, slot, now))

    def get_car_api(self, vehicle, path, checkReady=True, slot=None):
        # path is relative to the read slot's base URL, e.g. "<VIN>" or
        # "<VIN>/vehicle_data?endpoints=...". slot (a read source's) overrides
        # slotFor("read"). checkReady: the read needs the car awake (_ready),
        # and an answer proves it was
        car = vehicle.attachment("TeslaAPI")
        if slot is None:
            slot = self.slotFor("read")
        elif not slot.usable("read"):
            # Cleared or paused since the source chose it
            return False, None
        if slot is None:
            # No usable token
            return False, None

        if checkReady and not self._ready(vehicle, car):
            return False, None

        apiResponseDict = {}
        # Up to 3 requests while the car's buses wake or a transient error
        # clears; a resend after a refresh (at most one) is one of them
        sent, refreshed = 0, False
        while sent < 3:
            token = slot.accessToken
            req, outcome = self._request(
                slot,
                "get",
                "vehicles/" + path,
                "read",
                vehicle=vehicle,
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
                if not any(error.startswith(e) for e in self.carApiTransientErrors):
                    logger.info(
                        "ERROR: Can't access vehicle status for %s.  Will try "
                        "again later." % vehicle.name
                    )
                    self.updateCarApiLastErrorTime(car)
                    return False, None
                # Transient errors (vehicle unavailable, operation_timedout,
                # etc.) retry after a short delay, without incrementing
                # errorCount into the full exponential backoff.
                logger.log(
                    logging.INFO8,
                    "Transient error for %s: %s.  Will retry shortly."
                    % (vehicle.name, error),
                )
                time.sleep(5)
                continue
            if checkReady:
                car.lastAPIAccessTime = time.time()
            return True, response
        self.updateCarApiLastErrorTime(car)
        return False, None

    def _observeAppearance(self, vehicle, response, source):
        config = response.get("vehicle_config") or {}
        config = dict(config, option_codes=response.get("option_codes"))
        for key, field in APPEARANCE.items():
            if config.get(key):
                vehicle.observe(field, config[key], source)

    def readAppearance(self, vehicle) -> bool:
        # vehicle_config alone, for a car no source has said the looks of.
        # Never wakes it: get_car_api asks the free online check first
        slot = self.slotFor("read")
        if slot is None:
            return False
        path = vehicle.vin + "/vehicle_data?endpoints=vehicle_config"
        result, response = self.get_car_api(vehicle, path, slot=slot)
        if result:
            self._observeAppearance(vehicle, response, self.sourceOf(slot))
        return bool(result)

    def readPrice(self) -> float:
        return FLEET_DATA.price if self.slotFor("read") is self.fleetSlot else 0.0

    def _observeCharge(self, vehicle, charge, source, at):
        # Only keys present in the response are observed: a default or a
        # carried-over value is not a fresh reading.
        for key, field in (
            ("fast_charger_present", "fastChargerPresent"),
            ("fast_charger_type", "fastChargerType"),
            ("conn_charge_cable", "connChargeCable"),
            ("charger_power", "chargerPower"),
            ("charge_current_request_max", "chargeCurrentRequestMax"),
            ("charge_limit_soc", "chargeLimit"),
            ("battery_level", "batteryLevel"),
            ("time_to_full_charge", "timeToFullCharge"),
            ("charger_pilot_current", "availableCurrent"),
            ("charge_current_request", "chargeCurrentRequest"),
            ("charger_actual_current", "actualCurrent"),
            ("charger_phases", "phases"),
            ("charger_voltage", "voltage"),
            ("charging_state", "chargingState"),
            ("scheduled_charging_pending", "scheduledChargingPending"),
        ):
            if key in charge:
                vehicle.observe(field, charge[key], source, at=at)

    def cachedRefused(self, vehicle):
        # Whether the car's 412 still stands: it holds only for the Fleet
        # token that was refused, so a replaced token is asked again.
        car = vehicle.attachment("TeslaAPI")
        return car.cachedUnsupported and car.cachedRefusedAt == self.fleetSlot.changes

    def read_cached(self, vehicle):
        # One free Fleet GET of the car's last snapshot (it never wakes the
        # car): the charge fields are observed at the snapshot's own stamp,
        # which is returned; None when nothing was observed. The Owner API
        # answers 412 to this endpoint, so only the Fleet slot is used.
        slot = self.fleetSlot
        if self.cachedRefused(vehicle) or not slot.usable("read"):
            return None
        path = "vehicles/%s/vehicle_data_cached" % vehicle.vin
        req, outcome = self._request(slot, "get", path, "cached", vehicle=vehicle)
        if outcome is not None:
            return None
        try:
            data = json.loads(req.text)["response"]
            charge = data["charge_state"]
            stamp = charge["timestamp"] / 1000
        except (ValueError, KeyError, TypeError):
            return None
        source = "FleetAPICached"
        name = (data.get("vehicle_state") or {}).get("vehicle_name")
        if name:
            vehicle.observe("name", name, source, at=stamp)
        self._observeCharge(vehicle, charge, source, stamp)
        if "charge_port_door_open" in charge:
            vehicle.observe(
                "chargePortOpen", charge["charge_port_door_open"], source, at=stamp
            )
        # The snapshot has no pending flag of its own
        if "scheduled_charging_mode" in charge:
            vehicle.observe(
                "scheduledChargingPending",
                self._cachedPending(charge),
                source,
                at=stamp,
            )
        return stamp

    @staticmethod
    def _cachedPending(charge):
        return (
            charge.get("scheduled_charging_mode") != "Off"
            and charge.get("scheduled_charging_start_time") is not None
            and charge.get("charging_state") == "Stopped"
        )

    def update_vehicle_data(self, vehicle, slot=None):
        car = vehicle.attachment("TeslaAPI")
        path = (
            vehicle.vin
            + "/vehicle_data?endpoints="
            + "%3B".join(
                [
                    "location_data",
                    "charge_state",
                    "drive_state",
                    "vehicle_state",
                    "vehicle_config",
                ]
            )
        )

        now = time.time()

        # Not redundant with fetch's check: a second thread that passed it
        # finds the deferral the first just set, and pays no vehicle_data
        if now < car.statusDeferral:
            return True

        slot = slot or self.slotFor("read")
        source = self.sourceOf(slot)
        result, response = self.get_car_api(vehicle, path, slot=slot)

        if result:
            # What is missing, should the step after it raise
            missing = "drive_state missing from API response"
            try:
                drive = response["drive_state"]
                missing = "charge_state missing from API response"
                charge = response["charge_state"]
                missing = "latitude/longitude missing from drive_state"
                # The fix time first: the position's observers date it by it
                if drive.get("gps_as_of"):
                    vehicle.observe("gpsAsOf", drive["gps_as_of"], source)
                vehicle.observe("lat", drive["latitude"], source)
                vehicle.observe("lon", drive["longitude"], source)
            except (KeyError, TypeError, AttributeError):
                logger.error("update_vehicle_data: %s for %s" % (missing, vehicle.vin))
                return False

            # Only keys present in the response are observed: a default or a
            # carried-over value is not a fresh TeslaAPI reading.
            if "shift_state" in drive:
                vehicle.observe("shiftState", drive["shift_state"], source)

            try:
                vehicleName = response["vehicle_state"]["vehicle_name"]
            except (KeyError, TypeError):
                vehicleName = None
            if vehicleName:
                vehicle.observe("name", vehicleName, source)

            self._observeAppearance(vehicle, response, source)

            # One reading: the charger details carry the charging state's
            # stamp, and come first so it is judged with them
            self._observeCharge(vehicle, charge, source, self.registry.now())

            car.statusDeferral = now + READ_MAX_AGE

        return result
