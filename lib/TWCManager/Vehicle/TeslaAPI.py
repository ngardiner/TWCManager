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
from TWCManager.Vehicle.Registry.Fields import TELEMETRY_SOURCES
from TWCManager.Vehicle.Registry.Sources import TeslaAPIReadSource
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
from TWCManager.Vehicle.TeslaTokenSlot import TeslaTokenSlot, decodeToken

logger = LoggerFactory.get_logger("TeslaAPI", "Vehicle")


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
    # A charge rate the car accepted is not resent for this long (seconds).
    CHARGE_RATE_RESEND_AFTER = 600
    # The energy-site list (/products) is fetched at most this often (seconds)
    ENERGY_SITES_TTL = 3600
    # Seconds a free energy-site request may take (background thread)
    ENERGY_TIMEOUT = 10
    regionURL = {
        "NA": "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles",
        "EU": "https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/vehicles",
        "CN": "https://fleet-api.prd.cn.vn.cloud.tesla.cn/api/1/vehicles",
        # Not a Fleet region: the read-only Owner API used by TeslaMate tokens.
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
    # fleetSlot.changes when a refused command was last logged.
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
    wakeDelayMins = 3
    # The never-expires expiry earlier releases saved with a synced token.
    # Only compared against, when restoring saved settings.
    LEGACY_SYNCED_EXPIRY = 99999 * 99999 * 99999
    carApiLastStartOrStopChargeTime = 0
    carApiLastChargeLimitApplyTime = 0
    lastChargeLimitApplied = 0
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
    minChargeLevel = -1
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
        self.registry = VehicleRegistry.forMaster(master)
        # One read source per token slot, Owner first: the Fleet source reads
        # (paid) only when the Owner slot cannot.
        self.ownerReadSource = TeslaAPIReadSource(self, "owner")
        self.fleetReadSource = TeslaAPIReadSource(self, "fleet")
        self.registry.registerReadSource(self.ownerReadSource)
        self.registry.registerReadSource(self.fleetReadSource)
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
            self.minChargeLevel = self.config["config"].get("minChargeLevel", -1)
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
        # expiry. Called both from car_api_available() (ahead of vehicle
        # commands) and from read-only callers such as Storm Watch, which
        # otherwise have no other path that keeps the token alive. A synced
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

    def slotRequestFailed(self, slot, response):
        # Reacts to a failed vehicle read made with this slot's token.
        # Returns True only when it refreshed the slot, so the caller should
        # retry once. A dead token is cleared and a refused one paused for
        # reads; neither records an API error, so the next poll can use the
        # other slot at once. Storm Watch does not come here: a refused
        # energy read must not pause vehicle reads.
        if response is None:
            return False
        code = response.status_code
        label = slot.kind.capitalize()
        if code == 401:
            if (
                "expired" in response.text
                and slot.source != "synced"
                and slot.refreshToken
            ):
                return self.refreshSlot(slot) == "success"
            logger.error(
                "Tesla %s API token was rejected (HTTP 401); removing it." % label
            )
            slot.clear()
            self.master.queue_background_task({"cmd": "saveSettings"})
        elif code in (403, 410, 412):
            logger.error(
                "Tesla %s API refused a read (HTTP %d); not using that token "
                "for reads for an hour." % (label, code)
            )
            slot.pause()
        return False

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
        # uses, or None when no slot is usable for it. The only chooser.
        for slot in (self.ownerSlot, self.fleetSlot):
            if slot.usable(purpose):
                return slot
        return None

    @property
    def readOnly(self):
        # True when no slot can send vehicle commands.
        return self.slotFor("command") is None

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

            req = None
            apiResponseDict = {}
            url = self.vehicleListURL(slot)
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + slot.accessToken,
            }
            try:
                req = requests.get(
                    url, headers=headers, verify=slot.verifyCert, timeout=30
                )
                logger.log(logging.INFO8, "Car API cmd vehicles " + str(req))
                if req.status_code in (401, 403, 410, 412):
                    # Clears or pauses this slot only; no retry here, and no
                    # API error backoff, so the next poll can use the other
                    # slot at once.
                    self.slotRequestFailed(slot, req)
                    return False
                apiResponseDict = json.loads(req.text)
            except requests.exceptions.RequestException:
                logger.info("Failed to make API call " + url)
                if req:
                    logger.log(logging.INFO6, "Response: " + req.text)
            except json.decoder.JSONDecodeError:
                logger.info("Could not parse JSON result from " + url)
                if req:
                    logger.log(logging.INFO6, "Response: " + req.text)

            try:
                logger.debug("Car API vehicle list" + str(apiResponseDict) + "\n")

                for i in range(0, apiResponseDict["count"]):
                    self.addVehicle(apiResponseDict["response"][i])
                self.resetCarApiLastErrorTime()
            except (KeyError, TypeError):
                # This catches cases like trying to access
                # apiResponseDict['response'] when 'response' doesn't exist in
                # apiResponseDict.
                logger.log(
                    logging.INFO2,
                    "ERROR: Can't get list of vehicles via Tesla car API.  Will try again in "
                    + str(self.getCarApiErrorRetryMins())
                    + " minutes.",
                )
                self.updateCarApiLastErrorTime()
                return False
            return True

    def car_api_available(self, charge=None, applyLimit=None, vin=None):
        now = time.time()
        needSleep = False
        apiResponseDict = {}

        if self.getCarApiRetryRemaining():
            # It's been under carApiErrorRetryMins minutes since the car API
            # generated an error. To keep strain off Tesla's API servers, wait
            # carApiErrorRetryMins mins till we try again. This delay could be
            # reduced if you feel the need. It's mostly here to deal with unexpected
            # errors that are hopefully transient.
            # https://teslamotorsclub.com/tmc/threads/model-s-rest-api.13410/page-114#post-2732052
            # says he tested hammering the servers with requests as fast as possible
            # and was automatically blacklisted after 2 minutes. Waiting 30 mins was
            # enough to clear the blacklist. So at this point it seems Tesla has
            # accepted that third party apps use the API and deals with bad behavior
            # automatically.
            logger.log(
                logging.INFO6,
                "Car API disabled for "
                + str(self.getCarApiRetryRemaining())
                + " more seconds due to recent error.",
            )
            return False
        else:
            logger.log(
                logging.INFO8,
                "Entering car_api_available - next step is to query Tesla API",
            )

        # Authenticate to Tesla API
        self.refreshTokenIfNeeded()

        slot = self.slotFor("read")
        if slot is None:
            # No token yet, a token of unknown type (logged as an ERROR when
            # it was stored), or a paused slot.
            slots = (self.ownerSlot, self.fleetSlot)
            if any(s.accessToken and s.tokenType != s.kind for s in slots):
                reason = "token type unknown"
            elif any(now < s.pausedUntil for s in slots):
                reason = "paused"
            else:
                reason = "no Tesla API token yet"
            logger.log(
                logging.INFO8,
                "car_api_available returning False: no usable Tesla API token (%s)"
                % reason,
            )
            return False

        if self.getVehicleCount() < 1 and not self.loadVehicleList():
            return False

        if self.getVehicleCount() > 0 and (charge or applyLimit) and not self.readOnly:
            # Wake cars if needed
            for vehicle in self.getCarApiVehicles():
                if vin and vehicle.VIN != vin:
                    # Targeted command: leave other cars asleep.
                    continue

                if charge is True and vehicle.stopAskingToStartCharging:
                    # Vehicle is in a state (complete or charging) already
                    # which doesn't make sense for us to keep requesting it
                    # to start charging, so we will stop.
                    logger.log(
                        logging.DEBUG2,
                        "Don't repeatedly request API to charge "
                        + vehicle.name
                        + ", because vehicle.stopAskingToStartCharging "
                        + " == True - it has already been requested.",
                    )
                    continue

                if applyLimit is True and vehicle.stopTryingToApplyLimit:
                    logger.log(
                        logging.DEBUG2,
                        "Don't wake "
                        + vehicle.name
                        + " to set the charge limit - it has already been set",
                    )
                    continue

                if self.getCarApiRetryRemaining(vehicle):
                    # It's been under carApiErrorRetryMins minutes since the car
                    # API generated an error on this vehicle. Don't send it more
                    # commands yet.
                    logger.log(
                        logging.DEBUG2,
                        "Don't send commands to "
                        + vehicle.name
                        + " because it returned an error in the last "
                        + str(self.getCarApiErrorRetryMins())
                        + " minutes.",
                    )
                    continue

                # Don't wake a vehicle we already know is not at home;
                # waking it would drain its battery unnecessarily (closes #466).
                if not vehicle.atHome and vehicle.lat != 10000:
                    logger.log(
                        logging.DEBUG2,
                        "Don't wake " + vehicle.name + " because it is not at home.",
                    )
                    continue

                # Pre-wake delay: on first detection check awake state once,
                # then hold wakeDelayMins before sending wake_up.  During the
                # hold window no Fleet API calls are made, giving the car a
                # chance to self-start via the TWC offer.
                if vehicle.firstChargeNeededTime == 0:
                    if vehicle.ready():
                        continue
                    vehicle.firstChargeNeededTime = now
                    logger.info(
                        vehicle.name
                        + ": deferring wake for "
                        + str(self.wakeDelayMins)
                        + " minutes"
                    )
                    return False

                wakeDelaySecs = self.wakeDelayMins * 60
                if now - vehicle.firstChargeNeededTime < wakeDelaySecs:
                    # Still in pre-wake hold window - no API calls.
                    return False

                # Pre-wake delay elapsed.  Honour post-wake retry delay
                # before issuing another wake command.
                if now - vehicle.lastAPIAccessTime <= vehicle.delayNextWakeAttempt:
                    logger.debug(
                        "car_api_available returning False - still in "
                        + str(vehicle.delayNextWakeAttempt)
                        + "s post-wake delay."
                    )
                    return False

                # Check whether the car woke itself since the last attempt.
                if vehicle.ready():
                    vehicle.firstChargeNeededTime = 0
                    continue

                # Car is still not awake.  Send a wake command.
                apiResponseDict = self.wakeVehicle(vehicle)

                state = "error"
                logger.debug("Car API wake car response" + str(apiResponseDict))
                try:
                    state = apiResponseDict["response"]["state"]
                    self.resetCarApiLastErrorTime()

                except (KeyError, TypeError):
                    # This catches unexpected cases like trying to access
                    # apiResponseDict['response'] when 'response' doesn't exist
                    # in apiResponseDict.
                    state = "error"

                if state == "online":
                    # With max power saving settings, car will almost always
                    # report 'asleep' or 'offline' the first time it's sent
                    # wake_up.  Rarely, it returns 'online' on the first wake_up
                    # even when the car has not been contacted in a long while.
                    # I suspect that happens when we happen to query the car
                    # when it periodically awakens for some reason.
                    vehicle.firstWakeAttemptTime = 0
                    vehicle.firstChargeNeededTime = 0
                    vehicle.delayNextWakeAttempt = 0
                    # Don't alter vehicle.lastAPIAccessTime because
                    # vehicle.ready() uses it to return True if the last wake
                    # was under 2 mins ago.
                    needSleep = True
                else:
                    if vehicle.firstWakeAttemptTime == 0:
                        vehicle.firstWakeAttemptTime = now

                    # Fleet API wake credits are finite and expensive.
                    # Regardless of state (asleep/offline/error), wait 30
                    # minutes before retrying.  Rapid retries don't help:
                    # asleep cars can take 2+ minutes to come online after
                    # the first wake_up, and offline cars may not be
                    # reachable at all until they self-wake.
                    if state in ("asleep", "waking"):
                        self.resetCarApiLastErrorTime()
                    elif state == "offline":
                        self.resetCarApiLastErrorTime()
                        # Brief pause to avoid hammering the API on a
                        # transient connectivity hiccup.
                        time.sleep(5)
                    else:
                        # Handle 'error' state.
                        self.updateCarApiLastErrorTime()

                    vehicle.delayNextWakeAttempt = 30 * 60

                    if state == "error":
                        logger.info(
                            "Car API wake "
                            + vehicle.name
                            + " failed with unknown response.  "
                            + "Will try again in "
                            + str(vehicle.delayNextWakeAttempt)
                            + " seconds."
                        )
                    else:
                        logger.info(
                            "Car API wake "
                            + vehicle.name
                            + " failed.  State remains: '"
                            + state
                            + "'.  Will try again in "
                            + str(vehicle.delayNextWakeAttempt)
                            + " seconds."
                        )

                if (
                    vehicle.firstWakeAttemptTime > 0
                    and now - vehicle.firstWakeAttemptTime > 60 * 60
                ):
                    # It should never take over an hour to wake a car.  If it
                    # does, ask user to report an error.
                    logger.info(
                        "ERROR: We have failed to wake "
                        + vehicle.name
                        + " from '"
                        + state
                        + "' state for %.1f hours.\n"
                        "Please file an issue at https://github.com/ngardiner/TWCManager/. "
                        "Also include this: %s"
                        % (
                            ((now - vehicle.firstWakeAttemptTime) / 60 / 60),
                            str(apiResponseDict),
                        )
                    )

        if now - self.getCarApiLastErrorTime() < (self.getCarApiErrorRetryMins() * 60):
            logger.log(
                logging.INFO8,
                "car_api_available returning False because of recent carApiLasterrorTime "
                + str(now - self.getCarApiLastErrorTime()),
            )
            return False

        # We return True to indicate there was no error that prevents running
        # car API commands and that we successfully got a list of vehicles.
        # True does not indicate that any vehicle is actually awake and ready
        # for commands.
        logger.log(logging.INFO8, "car_api_available returning True")

        if needSleep:
            # If you send charge_start/stop less than 1 second after calling
            # update_location(), the charge command usually returns:
            #   {'response': {'result': False, 'reason': 'could_not_wake_buses'}}
            # I'm not sure if the same problem exists when sending commands too
            # quickly after we send wake_up.  I haven't seen a problem sending a
            # command immediately, but it seems safest to sleep 5 seconds after
            # waking before sending a command.
            time.sleep(5)

        return True

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

    def car_api_charge(self, task):
        # Do not call this function directly.  Call by using background thread:
        # queue_background_task({'cmd':'charge', 'charge':<True/False>})
        if self.refuseCommandIfReadOnly("charge_start/charge_stop"):
            return "error"

        charge = None
        vin = None
        if task:
            charge = task.get("charge", None)
            # When set, act on that vehicle only (and wake only that vehicle).
            vin = task.get("vin", None)

        now = time.time()
        apiResponseDict = {}
        if not charge:
            # Whenever we are going to tell vehicles to stop charging, reset
            # per-vehicle wake state so the next start cycle is fresh.
            for vehicle in self.getCarApiVehicles():
                if vin and vehicle.VIN != vin:
                    continue
                vehicle.stopAskingToStartCharging = False
                vehicle.firstChargeNeededTime = 0
                vehicle.firstWakeAttemptTime = 0
                vehicle.delayNextWakeAttempt = 0

        if now - self.getLastStartOrStopChargeTime() < 60:
            # Don't start or stop more often than once a minute. A skip, not
            # a failure: "error" would hand the command to the next provider
            logger.log(
                logging.DEBUG2,
                "car_api_charge return because not long enough since last carApiLastStartOrStopChargeTime",
            )
            return "skipped"

        if self.car_api_available(charge=charge, vin=vin) is False:
            logger.log(
                logging.INFO8,
                "car_api_charge return because car_api_available() == False",
            )
            return self._unavailableResult()

        startOrStop = "start" if charge else "stop"
        result = "success"
        logger.log(logging.INFO8, "startOrStop is set to " + str(startOrStop))

        for vehicle in self.getCarApiVehicles():
            if vin and vehicle.VIN != vin:
                continue

            if charge and vehicle.stopAskingToStartCharging:
                logger.log(
                    logging.INFO8,
                    "Don't charge "
                    + vehicle.name
                    + " because vehicle.stopAskingToStartCharging == True",
                )
                continue

            if not vehicle.ready():
                continue

            if not vehicle.update_charge() or not vehicle.update_location():
                result = "error"
                continue

            if vehicle.batteryLevel < self.minChargeLevel and not charge:
                # If the vehicle's charge state is lower than the configured minimum,
                #   don't stop it from charging, even if we'd otherwise not charge.
                continue

            # Only update carApiLastStartOrStopChargeTime if car_api_available() managed
            # to wake cars.  Setting this prevents any command below from being sent
            # more than once per minute.
            self.updateLastStartOrStopChargeTime()

            # only start/stop charging cars parked at home.

            if vehicle.update_location(3600) is False:
                result = "error"
                continue

            if not vehicle.atHome:
                # Vehicle is not at home, so don't change its charge state.
                message = (
                    vehicle.name
                    + " is not at home.  Do not "
                    + startOrStop
                    + " charge."
                )
                # Stop asking to start charging when not at home.
                if startOrStop == "start":
                    vehicle.stopAskingToStartCharging = True
                    message += "  Stop asking to start charging."
                logger.info(message)
                continue

            if vehicle.chargingState in ["Disconnected", "Complete"]:
                # Sending a command to a disconnected or complete vehicle is
                # useless.
                logger.info(
                    vehicle.name
                    + " is not able to charge.  Do not "
                    + startOrStop
                    + " charge."
                )
                vehicle.stopAskingToStartCharging = True
                continue

            if (
                charge
                and vehicle.batteryLevel > 0
                and vehicle.chargeLimit > 0
                and vehicle.batteryLevel >= vehicle.chargeLimit
            ):
                # Car is already at or above its charge limit; starting charge
                # would be immediately rejected by the car (closes #591).
                logger.info(
                    vehicle.name
                    + " battery level "
                    + str(vehicle.batteryLevel)
                    + "% is at or above charge limit "
                    + str(vehicle.chargeLimit)
                    + "%.  Do not start charge."
                )
                vehicle.stopAskingToStartCharging = True
                continue

            if vehicle.chargingState == "Charging" and charge:
                # Don't start charging if car is already charging.
                logger.info(
                    vehicle.name + " is already charging.  Do not start charge."
                )
                vehicle.stopAskingToStartCharging = True
                continue

            if (
                charge
                and vehicle.scheduledChargingPending
                and self.config["config"].get("respectVehicleSchedule", True)
            ):
                # The car is deliberately waiting on its own Scheduled
                # Charging / Scheduled Departure timer; a charge_start command
                # would override the in-car schedule and begin charging
                # immediately at full rate. The TWC keeps offering power,
                # which the car accepts when its schedule fires. Not setting
                # stopAskingToStartCharging so this re-evaluates each cycle
                # in case the user cancels the schedule.
                logger.info(
                    vehicle.name
                    + " is waiting on its in-car charging schedule.  Do not"
                    + " start charge (respectVehicleSchedule)."
                )
                continue

            # If you send charge_start/stop less than 1 second after calling
            # update_location(), the charge command usually returns:
            #   {'response': {'result': False, 'reason': 'could_not_wake_buses'}}
            # Waiting 2 seconds seems to consistently avoid the error, but let's
            # wait 5 seconds in case of hardware differences between cars.
            time.sleep(5)

            if charge:
                self.applyChargeLimit(self.lastChargeLimitApplied, checkArrival=True)

            slot = self.slotFor("command")
            if slot is None:
                result = "error"
                continue
            url = slot.baseURL + "/"
            url = url + str(vehicle.VIN) + "/command/charge_" + startOrStop
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + slot.accessToken,
            }

            # Retry up to 3 times on certain errors.
            for _ in range(0, 3):
                try:
                    req = requests.post(url, headers=headers, verify=slot.verifyCert)
                    logger.log(
                        logging.INFO8,
                        "Car API cmd charge_" + startOrStop + " " + str(req),
                    )
                    apiResponseDict = json.loads(req.text)
                except requests.exceptions.RequestException:
                    pass
                except json.decoder.JSONDecodeError:
                    pass

                try:
                    logger.log(
                        logging.INFO4,
                        vehicle.name
                        + ": "
                        + startOrStop
                        + " charge response"
                        + str(apiResponseDict),
                    )
                    # Responses I've seen in apiResponseDict:
                    # Car is done charging:
                    #   {'response': {'result': False, 'reason': 'complete'}}
                    # Car wants to charge but may not actually be charging. Oddly, this
                    # is the state reported when car is not plugged in to a charger!
                    # It's also reported when plugged in but charger is not offering
                    # power or even when the car is in an error state and refuses to
                    # charge.
                    #   {'response': {'result': False, 'reason': 'charging'}}
                    # Car not reachable:
                    #   {'response': None, 'error_description': '', 'error': 'vehicle unavailable: {:error=>"vehicle unavailable:"}'}
                    # This weird error seems to happen randomly and re-trying a few
                    # seconds later often succeeds:
                    #   {'response': {'result': False, 'reason': 'could_not_wake_buses'}}
                    # I've seen this a few times on wake_up, charge_start, and drive_state:
                    #   {'error': 'upstream internal error', 'response': None, 'error_description': ''}
                    # I've seen this once on wake_up:
                    #   {'error': 'operation_timedout for txid `4853e3ad74de12733f8cc957c9f60040`}', 'response': None, 'error_description': ''}
                    # Start or stop charging success:
                    #   {'response': {'result': True, 'reason': ''}}
                    if apiResponseDict["response"] is None:
                        # This generally indicates an error like 'vehicle
                        # unavailable', but it's not something I think the caller can do
                        # anything about, so return generic 'error'.
                        error = (apiResponseDict or {}).get("error", "")
                        if "Tesla Vehicle Command Protocol required" in error:
                            # The legacy REST command endpoint has been decommissioned.
                            # Log a clear message and stop retrying (closes #580).
                            logger.error(
                                "Tesla Vehicle Command Protocol (VCP) is required for "
                                + vehicle.name
                                + ". The legacy REST command endpoint is no longer "
                                "supported. Please configure the Fleet API with a "
                                "registered app, or enable the TeslaBLE module."
                            )
                            vehicle.stopAskingToStartCharging = True
                            result = "error"
                            return result
                        result = "error"
                        # Don't send another command to this vehicle for
                        # carApiErrorRetryMins mins.
                        self.updateCarApiLastErrorTime(vehicle)
                    else:
                        if apiResponseDict["response"]["result"] == True:
                            self.resetCarApiLastErrorTime(vehicle)
                        elif charge:
                            reason = self.findReason(apiResponseDict)
                            if reason in [
                                "complete",
                                "charging",
                                "is_charging",
                                "disconnected",
                                "requested",
                            ]:
                                # We asked the car to charge, but it responded
                                # that it can't, either because it's reached
                                # target charge state (reason == 'complete'), or
                                # it's already trying to charge (reason ==
                                # 'charging' or 'requested') or it's not
                                # connected to a charger (reason ==
                                # 'disconnected'). In these cases, it won't help
                                # to keep asking it to charge, so set
                                # vehicle.stopAskingToStartCharging = True.
                                #
                                # Remember, this only means at least one car in
                                # the list wants us to stop asking and we don't
                                # know which car in the list is connected to our
                                # TWC.
                                logger.info(
                                    vehicle.name
                                    + " is done charging or already trying to charge or not connected to a charger."
                                    + "  Stop asking to start charging."
                                )
                                vehicle.stopAskingToStartCharging = True
                                self.resetCarApiLastErrorTime(vehicle)
                            elif reason == "could_not_wake_buses":
                                # This error often happens if you call
                                # charge_start too quickly after another command
                                # like drive_state. Even if you delay 5 seconds
                                # between the commands, this error still comes
                                # up occasionally. Retrying often succeeds, so
                                # wait 5 secs and retry.
                                # If all retries fail, we'll try again in a
                                # minute because we set
                                # carApiLastStartOrStopChargeTime = now earlier.
                                time.sleep(5)
                                continue
                            else:
                                # Start charge failed with an error I
                                # haven't seen before, so wait
                                # carApiErrorRetryMins mins before trying again.
                                logger.info(
                                    'ERROR "'
                                    + reason
                                    + '" when trying to '
                                    + startOrStop
                                    + " car charging via Tesla car API.  Will try again later."
                                    + "\nIf this error persists, please file an issue at https://github.com/ngardiner/TWCManager/ with a copy of this error.",
                                )
                                result = "error"
                                self.updateCarApiLastErrorTime(vehicle)
                        else:
                            # Stop charge failed with an error I
                            # haven't seen before, so wait
                            # carApiErrorRetryMins mins before trying again.
                            reason = self.findReason(apiResponseDict)
                            logger.info(
                                'ERROR "'
                                + reason
                                + '" when trying to '
                                + startOrStop
                                + " car charging via Tesla car API.  Will try again later."
                                + "\nIf this error persists, please file an issue at https://github.com/ngardiner/TWCManager/ with a copy of this error.",
                            )
                            result = "error"
                            self.updateCarApiLastErrorTime(vehicle)

                except (KeyError, TypeError):
                    # This catches cases like trying to access
                    # apiResponseDict['response'] when 'response' doesn't exist in
                    # apiResponseDict.
                    logger.info(
                        "ERROR: Failed to "
                        + startOrStop
                        + " car charging via Tesla car API.  Will try again later."
                    )
                    self.updateCarApiLastErrorTime(vehicle)
                break

        if self.getLastStartOrStopChargeTime() == now:
            logger.info("Car API " + startOrStop + " charge result: " + result)

        return result

    def findReason(self, apiResponseDict):
        if "reason" in apiResponseDict["response"]:
            return apiResponseDict["response"]["reason"]
        elif "string" in apiResponseDict["response"]:
            return apiResponseDict["response"]["string"].split(": ")[-1]
        return ""

    def applyChargeLimit(
        self,
        limit,
        checkArrival=False,
        checkDeparture=False,
        vin=None,
        remoteOnly=False,
    ):
        # vin: act on that car only. remoteOnly asks for a cloud path, which
        # this is. A targeted call is neither held by nor counted in the
        # once-a-minute limit Policy's broadcasts keep.
        target = str(vin).upper() if vin else None
        if self.refuseCommandIfReadOnly("set_charge_limit"):
            return "error"
        if limit != -1 and (limit < 50 or limit > 100):
            logger.log(logging.INFO8, "applyChargeLimit skipped")
            return "error"

        if not self.car_api_available():
            logger.log(
                logging.INFO8,
                "applyChargeLimit return because car_api_available() == False",
            )
            return "error"

        now = time.time()
        if (
            target is None
            and not checkArrival
            and not checkDeparture
            and now - self.carApiLastChargeLimitApplyTime < 60
        ):
            # Don't change limits more often than once a minute. A skip, not
            # a failure: "error" would hand the limit to the next provider
            logger.log(
                logging.DEBUG2,
                "applyChargeLimit return because under 60 sec since last carApiLastChargeLimitApplyTime",
            )
            return "skipped"

        # We need to try to apply limits if:
        #   - We think the car is at home and the limit has changed
        #   - We think the car is at home and we've been asked to check for departures
        #   - We think the car is at home and we notice it gone
        #   - We think the car is away from home and we've been asked to check for arrivals
        #
        # We do NOT opportunistically check for arrivals, because that would be a
        # continuous API poll.
        vehicles = [
            v
            for v in self.carApiVehicles
            if target is None or str(v.VIN).upper() == target
        ]
        if target is not None and not vehicles:
            logger.log(logging.INFO8, "applyChargeLimit: no such vehicle")
            return "error"

        needToWake = False
        for vehicle in vehicles:
            wasAtHome, outside, lastApplied = self.master.getNormalChargeLimit(
                vehicle.ID
            )
            # Don't wake cars to tell them about reduced limits;
            # only wake if they might be able to charge further now
            if wasAtHome and (limit > (lastApplied if lastApplied != -1 else outside)):
                needToWake = True
                vehicle.stopAskingToStartCharging = False
            if (
                wasAtHome
                and (
                    limit != lastApplied
                    or checkDeparture
                    or (vehicle.update_location() and not vehicle.atHome)
                )
            ) or (not wasAtHome and checkArrival):
                vehicle.stopTryingToApplyLimit = False
                vehicle.stopAskingToStartCharging = False

        if needToWake and self.car_api_available(applyLimit=True, vin=target) is False:
            logger.log(
                logging.INFO8,
                "applyChargeLimit return because car_api_available() == False",
            )
            return self._unavailableResult()

        if target is None and self.lastChargeLimitApplied != limit:
            if limit != -1:
                logger.log(
                    logging.INFO2,
                    "Attempting to apply limit of "
                    + str(limit)
                    + "% to all vehicles at home",
                )
            else:
                logger.log(
                    logging.INFO2,
                    "Attempting to restore charge limits for all vehicles at home",
                )
            self.lastChargeLimitApplied = limit

        if target is None:
            self.carApiLastChargeLimitApplyTime = now

        needSleep = False
        for vehicle in vehicles:
            if vehicle.stopTryingToApplyLimit or not vehicle.ready():
                continue

            wasAtHome, outside, lastApplied = self.master.getNormalChargeLimit(
                vehicle.ID
            )
            forgetVehicle = False
            if not vehicle.update_charge() or not vehicle.update_location():
                # We failed to read the "normal" limit or locate the car; don't
                # risk changing the charge limit yet.
                continue

            if not wasAtHome and not vehicle.atHome:
                # If the vehicle was away and is still away, nothing to do.
                continue

            if not wasAtHome and vehicle.atHome:
                logger.log(logging.INFO2, vehicle.name + " has arrived")
                vehicle.stopAskingToStartCharging = False
                outside = vehicle.chargeLimit
            elif wasAtHome and not vehicle.atHome:
                logger.log(logging.INFO2, vehicle.name + " has departed")
                # Don't ask a departed vehicle to start charging; it's not here
                vehicle.stopAskingToStartCharging = True
                forgetVehicle = True

            if limit == -1 or not vehicle.atHome:
                # We're removing any applied limit, provided it hasn't been manually changed
                #
                # If lastApplied == -1, the manual-change path is always selected.
                if (
                    wasAtHome
                    and vehicle.chargeLimit == lastApplied
                    and vehicle.chargeLimit != outside
                ):
                    if vehicle.apply_charge_limit(outside):
                        logger.log(
                            logging.INFO2,
                            "Restoring "
                            + vehicle.name
                            + " to charge limit "
                            + str(outside)
                            + "%",
                        )
                        vehicle.stopTryingToApplyLimit = True
                else:
                    # If the charge limit has been manually changed, user action overrides the
                    # saved charge limit.  Leave it alone.
                    vehicle.stopTryingToApplyLimit = True
                    outside = vehicle.chargeLimit

                if vehicle.stopTryingToApplyLimit:
                    if forgetVehicle:
                        self.master.removeNormalChargeLimit(vehicle.ID)
                    else:
                        self.master.saveNormalChargeLimit(vehicle.ID, outside, -1)
            else:
                if vehicle.chargeLimit != limit:
                    if vehicle.apply_charge_limit(limit):
                        logger.log(
                            logging.INFO2,
                            "Set "
                            + vehicle.name
                            + " to charge limit of "
                            + str(limit)
                            + "%",
                        )
                        vehicle.stopTryingToApplyLimit = True
                else:
                    vehicle.stopTryingToApplyLimit = True

                if vehicle.stopTryingToApplyLimit:
                    self.master.saveNormalChargeLimit(vehicle.ID, outside, limit)

            if vehicle.atHome and vehicle.stopTryingToApplyLimit:
                needSleep = True

        if needSleep:
            # If you start charging too quickly after setting the charge limit,
            # the vehicle sometimes refuses the start command because it's
            # "fully charged" under the old limit, but then continues to say
            # charging was stopped once the new limit is in place.
            time.sleep(5)

        if checkArrival:
            self.updateChargeAtHome()

        # VehiclePriority reads None as failure and would try the next
        # provider (Home Assistant: a second, billed command)
        return "success"

    def _unavailableResult(self):
        # car_api_available() is False on a failure (no usable token, no
        # vehicle list, the error backoff) and also while it holds off waking
        # a car on purpose (wakeDelayMins, the post-wake wait). A hold is a
        # skip: "error" would hand the command to the next provider (Home
        # Assistant, billed), which would wake the car.
        if (
            self.slotFor("command") is not None
            and self.slotFor("read") is not None
            and self.getVehicleCount() > 0
            and not self.getCarApiRetryRemaining()
        ):
            return "skipped"
        return "error"

    def getCarApiErrorRetryMins(self, vehicle=None):
        errorCount = self.errorCount
        if vehicle:
            errorCount = max(vehicle.errorCount, errorCount)
        errorCount = max(errorCount - 1, 0)
        return min(errorCount, 10)

    def getCarApiLastErrorTime(self):
        return self.carApiLastErrorTime

    def vehicleListURL(self, slot):
        # The Owner API no longer lists vehicles under /vehicles (it answers
        # 412 "only available on fleetapi"); /products still works and also
        # returns energy sites, which addVehicle skips because they have no
        # VIN. Per-vehicle endpoints (/vehicles/<VIN>/...) work as they are.
        if slot.tokenType == "owner":
            return slot.baseURL[: -len("vehicles")] + "products"
        return slot.baseURL

    # Energy sites and the Wall Connector VINs. Only the free /products and
    # /energy_sites/<id>/live_status are ever requested here: never a vehicle
    # endpoint, vehicle_data, a wake or a command. Background thread only.
    # Unverified on hardware: that live_status lists wall_connectors, each
    # with a din and, while a car is connected, its vin (tesla-fleet-api npm
    # types), and whether the Owner API serves live_status at all (a refusal
    # falls to the Fleet slot).

    def _energyURL(self, slot, path):
        # The API root under the slot's vehicles URL, as vehicleListURL
        return slot.baseURL[: -len("vehicles")] + path

    def _energyGet(self, slot, path):
        # (status code, parsed body or None); a transport error raises
        req = requests.get(
            self._energyURL(slot, path),
            headers={
                "accept": "application/json",
                "Authorization": "Bearer " + slot.accessToken,
            },
            verify=slot.verifyCert,
            timeout=self.ENERGY_TIMEOUT,
        )
        try:
            body = req.json()
        except ValueError:
            body = None
        return req.status_code, body

    # Answers that latch the Wall Connector read off for a slot until its
    # token changes, as the vehicle reads treat 403/410/412
    ENERGY_REFUSALS = (401, 403, 410, 412)

    def _energyRefused(self, slot, status, liveStatus=False):
        # A 404 from live_status on the Owner API is taken as the Owner API
        # not serving it (unverified), so the Fleet slot is asked instead
        return status in self.ENERGY_REFUSALS or (
            liveStatus and status == 404 and slot.kind == "owner"
        )

    def _refuseWallConnectors(self, slot, status):
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
        try:
            status, body = self._energyGet(slot, "products")
        except requests.exceptions.RequestException as e:
            logger.debug("Energy sites: %s", type(e).__name__)
            return None
        if self._energyRefused(slot, status):
            self._refuseWallConnectors(slot, status)
            return None
        if status != 200:
            return None
        try:
            sites = [
                entry
                for entry in body["response"]
                if isinstance(entry, dict) and "energy_site_id" in entry
            ]
        except (KeyError, TypeError):
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
            try:
                status, body = self._energyGet(
                    slot, "energy_sites/%s/live_status" % siteID
                )
            except requests.exceptions.RequestException as e:
                logger.debug("Wall Connector VINs: %s", type(e).__name__)
                continue
            if self._energyRefused(slot, status, liveStatus=True):
                self._refuseWallConnectors(slot, status)
                return None
            if status != 200:
                continue
            response = body.get("response") if isinstance(body, dict) else None
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

    def getCarApiRetryRemaining(self, vehicle=None):
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
            lasterrortime = time.time() - lastError
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

    def getLastStartOrStopChargeTime(self):
        return int(self.carApiLastStartOrStopChargeTime)

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
        # True when no usable Fleet API token can send vehicle commands.
        # Logs at INFO once per Fleet slot change, makes no HTTP call and
        # records no API error (an error would start the backoff that also
        # blocks reads).
        if not self.readOnly:
            return False
        if self.fleetSlot.changes != self.refusalLoggedAt:
            logger.info(
                "Tesla API is read-only (no usable Fleet API token); not sending "
                + command
                + " or any other vehicle command. Commands need a Fleet API "
                "token or the TeslaBLE module."
            )
            self.refusalLoggedAt = self.fleetSlot.changes
        return True

    def applySyncedToken(self, accessToken):
        # Entry point for token sync sources (TeslaMate): the access token
        # only, so a synced slot never refreshes. Accepted whether or not
        # tokenSyncEnabled() is already true. Returns storeToken's result.
        return self.storeToken(accessToken, "", "synced")

    def setChargeRate(self, charge_rate, vehicle=None, set_again=False):
        # As a fallback to allow initial implementation of the charge rate functionality for single car installs,
        # If no vehicle is specified, we take the first returned to us.

        if self.refuseCommandIfReadOnly("set_charging_amps"):
            return False

        if not vehicle:
            vehicles = self.getCarApiVehicles()
            if not vehicles:
                logger.error("No Tesla vehicles available to set charge rate")
                return False
            vehicle = vehicles[0]

        slot = self.slotFor("command")
        if slot is None:
            return False

        # The Fleet API takes whole amps. The web debug path passes the
        # rate through from JSON, so it may be a numeric string.
        try:
            charge_rate = int(float(charge_rate))
        except (TypeError, ValueError, OverflowError):
            logger.error("setChargeRate: invalid charge rate %r", charge_rate)
            return False
        # Do not set charge_rate to 0 as this will effectively stop the car from charging all together
        if charge_rate < 1:
            charge_rate = 1

        # set_charging_amps is a paid command: don't resend the rate this
        # car last accepted, unless CHARGE_RATE_RESEND_AFTER has passed (the
        # rate may have been changed elsewhere). set_again (the low-rate
        # repeat) always sends.
        if (
            not set_again
            and getattr(vehicle, "lastChargeRateSent", None) == charge_rate
            and time.monotonic() - getattr(vehicle, "lastChargeRateSentTime", 0)
            < self.CHARGE_RATE_RESEND_AFTER
        ):
            return True

        vehicle.lastAPIAccessTime = time.time()

        url = slot.baseURL + "/"
        url = url + str(vehicle.VIN) + "/command/set_charging_amps"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + slot.accessToken,
        }

        body = {"charging_amps": charge_rate}

        try:
            req = requests.post(
                url, headers=headers, json=body, verify=slot.verifyCert, timeout=10
            )
            logger.log(
                logging.INFO8,
                f"Car API cmd set_charging_amps {charge_rate}A {str(req)}",
            )
            apiResponseDict = json.loads(req.text)
        except requests.exceptions.RequestException:
            return False
        except json.decoder.JSONDecodeError:
            return False

        accepted = self._chargeRateAccepted(apiResponseDict)

        # Set charge rates < 5 twice, see https://github.com/tdorssers/TeslaPy/pull/42
        if charge_rate < 5:
            if set_again:
                # The caller (the first send) records the rate.
                return apiResponseDict
            time.sleep(5)
            again = self.setChargeRate(charge_rate, vehicle, set_again=True)
            if accepted and self._chargeRateAccepted(again):
                self._recordChargeRate(vehicle, charge_rate)
            return again
        if accepted:
            self._recordChargeRate(vehicle, charge_rate)
        return apiResponseDict

    @staticmethod
    def _chargeRateAccepted(apiResponseDict):
        try:
            return apiResponseDict["response"]["result"] == True
        except (KeyError, TypeError):
            return False

    @staticmethod
    def _recordChargeRate(vehicle, charge_rate):
        vehicle.lastChargeRateSent = charge_rate
        vehicle.lastChargeRateSentTime = time.monotonic()
        # Own-write stamp, so a later read can tell our write from the feed.
        # Non-creating lookup: a write never revives a forgotten vehicle.
        live = getattr(vehicle, "_liveVehicle", None)
        registryVehicle = live() if callable(live) else None
        if registryVehicle is not None:
            registryVehicle.noteWrite("availableCurrent", charge_rate, "TeslaAPI")

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

    def updateLastStartOrStopChargeTime(self):
        self.carApiLastStartOrStopChargeTime = time.time()
        return True

    def updateChargeAtHome(self):
        # True when this module holds cars to refresh, so VehiclePriority's
        # walk ends here; False lets another module refresh its own cars
        for car in self.carApiVehicles:
            if car.atHome:
                car.update_charge()
        self.lastChargeCheck = time.time()
        return bool(self.carApiVehicles)

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

    def wakeVehicle(self, vehicle):
        if self.refuseCommandIfReadOnly("wake_up"):
            return False
        slot = self.slotFor("command")
        if slot is None:
            return False
        apiResponseDict = None
        vehicle.lastAPIAccessTime = time.time()

        url = slot.baseURL + "/"
        url = url + str(vehicle.VIN) + "/wake_up"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + slot.accessToken,
        }
        req = None
        try:
            req = requests.post(url, headers=headers, verify=slot.verifyCert)
            logger.log(logging.INFO8, "Car API cmd wake_up" + str(req))
            req.raise_for_status()
            apiResponseDict = json.loads(req.text)
        except requests.exceptions.RequestException:
            if req is None:
                # The request never went out (e.g. no network).
                pass
            elif req.status_code == 401 and "expired" in req.text:
                # If the token is expired, refresh it and try again
                if self.refreshSlot(slot) == "success":
                    return self.wakeVehicle(vehicle)
            elif req.status_code == 429:
                # We're explicitly being told to back off
                self.errorCount = max(30, self.errorCount)
                self.updateCarApiLastErrorTime(vehicle)
            return False
        except json.decoder.JSONDecodeError:
            return False

        return apiResponseDict

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
    lastLimitAttemptTime = 0

    errorCount = 0
    lastErrorTime = 0
    lastVehicleStatusTime = 0
    lastVehicleAwakeTime = 0
    lastVehicleAwakeState = False
    stopAskingToStartCharging = False
    stopTryingToApplyLimit = False
    statusDeferral = 0
    # update_charge reuses a charge reading up to this old (seconds).
    READ_MAX_AGE = 300

    # Per-vehicle fields are views onto the registry Vehicle: reads give the
    # latest observation (or the legacy default), writes observe.
    name = RegistryField("name", "")
    batteryLevel = RegistryField("batteryLevel", 10000)
    chargeLimit = RegistryField("chargeLimit", -1)
    chargingState = RegistryField("chargingState", "Unknown")
    availableCurrent = RegistryField("availableCurrent", 0)
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
    # The last rate set_charging_amps succeeded with, and when (monotonic
    # clock), so it isn't resent within TeslaAPI.CHARGE_RATE_RESEND_AFTER.
    lastChargeRateSent = None
    lastChargeRateSentTime = 0
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

    def _bind(self, vehicle, listed=False):
        # Joins the registry vehicle with both claims. The Owner API never
        # commands; the Fleet claim follows the Fleet slot live, so a token
        # pasted or synced after the listing manages the car without a
        # re-list. A listing attaches this object; a rebind (after a forget)
        # only takes an empty attachment, so a stale object never displaces a
        # newer one.
        if vehicle is not self._vehicle:
            if listed or vehicle.attachment("TeslaAPI") is None:
                vehicle.attach("TeslaAPI", self)
            carapi = self.carapi
            vehicle.claim("OwnerAPI", False, "listed")
            vehicle.claim(
                "FleetAPI", lambda: carapi.fleetSlot.usable("command"), "listed"
            )
            self._vehicle = vehicle
            # A held limit migrated under this car's numeric ID moves under
            # its VIN now that the listing names both.
            held = getattr(getattr(carapi, "master", None), "heldLimits", None)
            if held is not None:
                held.adoptLegacyId(self.ID, self.VIN)
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

    def is_awake(self, slot=None):
        obs, age = self._telemetryState()
        if obs is not None and obs.source in TELEMETRY_SOURCES:
            # External sync (e.g. TeslaMate) has provided at least one update.
            if obs.value in ("asleep", "offline"):
                # Car is known to be asleep - trust the sync source and avoid
                # Fleet API calls that cost money just to confirm it's still asleep.
                return False
            if age is not None and age <= self.syncTimeout / 4:
                # Fresh data and car is not asleep - trust it.
                return obs.value in ("online", "charging", "updating", "driving")
            # Stale data but car was last known active - fall through to API check
            # in case the sync source dropped while the car was still awake.
        now = time.time()
        # Don't check more often than every 2 minutes
        if now - self.lastVehicleAwakeTime < 120:
            return self.lastVehicleAwakeState
        result, response = self.get_car_api(
            str(self.VIN), checkReady=False, provesOnline=False, slot=slot
        )
        awakeState = result and response.get("state", "") == "online"
        self.lastVehicleAwakeState = awakeState
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
            # No usable token (car_api_available logs why).
            return False, None

        if checkReady and not self.ready():
            return False, None

        apiResponseDict = {}
        refreshed = False

        # Retry up to 3 times on certain errors.
        for _ in range(0, 3):
            # Built per attempt, so a retry after a refresh sends the new token.
            url = slot.baseURL + "/" + path
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + slot.accessToken,
            }
            req = None
            try:
                req = requests.get(url, headers=headers, verify=slot.verifyCert)
                req.raise_for_status()
                logger.log(logging.INFO8, "Car API cmd " + url + " " + str(req))
                apiResponseDict = json.loads(req.text)
                # This error can happen here as well:
                #   {'response': {'reason': 'could_not_wake_buses', 'result': False}}
                # This one is somewhat common:
                #   {'response': None, 'error': 'vehicle unavailable: {:error=>"vehicle unavailable:"}', 'error_description': ''}
            except requests.exceptions.RequestException:
                if req is None:
                    # The request never went out (e.g. no network).
                    pass
                elif req.status_code in (401, 403, 410, 412):
                    # Clears or pauses the slot, or refreshes it for one retry.
                    if not refreshed and self.carapi.slotRequestFailed(slot, req):
                        refreshed = True
                        continue
                elif req.status_code == 429:
                    # We're explicitly being told to back off
                    self.errorCount = max(30, self.errorCount)
                    self.carapi.updateCarApiLastErrorTime(self)
                return False, None
            except json.decoder.JSONDecodeError:
                pass

            try:
                logger.debug("Car API vehicle status" + str(apiResponseDict))

                response = apiResponseDict["response"]

                # A successful call to drive_state will not contain a
                # response['reason'], so we check if the 'reason' key exists.
                if (
                    "reason" in response
                    and response["reason"] == "could_not_wake_buses"
                ):
                    # Retry after 5 seconds.  See notes in car_api_charge where
                    # 'could_not_wake_buses' is handled.
                    time.sleep(5)
                    continue
            except (KeyError, TypeError):
                # This catches cases like trying to access
                # apiResponseDict['response'] when 'response' doesn't exist in
                # apiResponseDict.
                error = (apiResponseDict or {}).get("error", "")
                is_transient = any(
                    error.startswith(e) for e in self.carapi.carApiTransientErrors
                )
                if is_transient:
                    # Transient errors (vehicle unavailable, operation_timedout,
                    # etc.) should not increment errorCount and trigger the full
                    # exponential backoff; just retry after a short delay (closes #377).
                    logger.log(
                        logging.INFO8,
                        "Transient error for "
                        + self.name
                        + ": "
                        + error
                        + ".  Will retry shortly.",
                    )
                    time.sleep(5)
                    continue
                logger.info(
                    "ERROR: Can't access vehicle status for "
                    + self.name
                    + ".  Will try again later."
                )
                self.carapi.updateCarApiLastErrorTime(self)
                return False, None

            if provesOnline:
                self.lastAPIAccessTime = time.time()

            return (True, response)
        else:
            self.carapi.updateCarApiLastErrorTime(self)
            return (False, None)

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

        try:
            result, response = self.get_car_api(path, slot=slot)
        except TypeError:
            logger.error("Got None response from get_car_api()")
            return False

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

    def apply_charge_limit(self, limit):
        if self.carapi.refuseCommandIfReadOnly("set_charge_limit"):
            return False

        if self.stopTryingToApplyLimit:
            return True

        now = time.time()

        if (
            now - self.lastLimitAttemptTime <= 300
            or self.carapi.getCarApiRetryRemaining(self)
        ):
            return False

        if self.ready() is False:
            return False

        self.lastLimitAttemptTime = now

        slot = self.carapi.slotFor("command")
        if slot is None:
            return False
        url = slot.baseURL + "/"
        url = url + str(self.VIN) + "/command/set_charge_limit"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + slot.accessToken,
        }
        body = {"percent": limit}

        for _ in range(0, 3):
            try:
                req = requests.post(
                    url, headers=headers, json=body, verify=slot.verifyCert
                )
                logger.log(logging.INFO8, "Car API cmd set_charge_limit " + str(req))

                apiResponseDict = json.loads(req.text)
            except requests.exceptions.RequestException:
                pass
            except json.decoder.JSONDecodeError:
                pass

            result = False
            reason = ""
            try:
                result = apiResponseDict["response"]["result"]
                reason = self.carapi.findReason(apiResponseDict)
            except (KeyError, TypeError):
                # This catches unexpected cases like trying to access
                # apiResponseDict['response'] when 'response' doesn't exist
                # in apiResponseDict.
                result = False

            if result is True or reason == "already_set":
                registryVehicle = self._liveVehicle()
                if registryVehicle is not None:
                    registryVehicle.noteWrite("chargeLimit", limit, "TeslaAPI")
                self.stopTryingToApplyLimit = True
                self.lastAPIAccessTime = now
                self.carapi.resetCarApiLastErrorTime(self)
                return True
            elif reason == "could_not_wake_buses":
                time.sleep(5)
                continue
            else:
                self.carapi.updateCarApiLastErrorTime(self)

        return False
