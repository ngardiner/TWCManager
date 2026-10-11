import base64
import hashlib
import json
import logging
import re
import requests
from requests.adapters import HTTPAdapter
import secrets
import ssl
from threading import RLock, Thread
import time
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib3.util.ssl_ import create_urllib3_context
import jwt
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("TeslaAPI", "Vehicle")


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
    baseURL = ""
    regionURL = {
        "NA": "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles",
        "EU": "https://fleet-api.prd.eu.vn.cloud.tesla.com/api/1/vehicles",
        "CN": "https://fleet-api.prd.cn.vn.cloud.tesla.cn/api/1/vehicles",
        # Not a Fleet region: the read-only Owner API used by TeslaMate tokens.
        # fleetRegions() leaves it out of every region list and lookup.
        "OwnerAPI": "https://owner-api.teslamotors.com/api/1/vehicles",
    }
    # teslaProxy base URL and certificate from config.json, kept apart from
    # baseURL so it can be restored when a Fleet token replaces an Owner one.
    proxyURL = ""
    proxyCert = True
    # True while the current token is an Owner API token, which cannot send
    # vehicle commands.
    readOnly = False
    readOnlyRefusalLogged = False
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
    verifyCert = True
    carApiLastErrorTime = 0
    wakeDelayMins = 3
    carApiBearerToken = ""
    carApiRefreshToken = ""
    carApiTokenExpireTime = time.time()
    # Expiry given to tokens supplied by a token sync source (TeslaMate). The
    # source refreshes them; TWCManager must never try to.
    SYNCED_TOKEN_EXPIRY = 99999 * 99999 * 99999
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

        try:
            self.config = master.config
            cfg = self.config.get("vehicle", {}).get("teslaAPI", {}) or {}
            self._enabled = cfg.get("enabled", True)

            proxyURL = self.config["config"].get("teslaProxy", "")
            if proxyURL:
                self.proxyURL = proxyURL + "/api/1/vehicles"
                self.proxyCert = self.config["config"].get("teslaProxyCert", True)
                # Until a token is classified, assume it suits the proxy.
                self.baseURL = self.proxyURL
                self.verifyCert = self.proxyCert
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

    def enabled(self) -> bool:
        return self._enabled

    def addVehicle(self, json):
        if "vin" in json:
            self.carApiVehicles.append(CarApiVehicle(json, self, self.config))

    def apiDebugInterface(self, command, vehicleID, parameters):
        # Provides an interface from the Web UI to allow commands to be run interactively

        # Map vehicle ID back to vehicle object
        vehicle = self.getVehicleByID(int(vehicleID))

        # Get parameters
        params = {}
        try:
            params = json.loads(parameters)
        except json.decoder.JSONDecodeError:
            pass

        # Execute specified command
        if command == "setChargeRate":
            charge_rate = params.get("charge_rate", 0)
            self.setChargeRate(charge_rate, vehicle)
            return True
        elif command == "wakeVehicle":
            self.wakeVehicle(vehicle)
            return True

        # If we make it here, we did not execute a command
        return False

    def apiRefresh(self):
        return self.refreshToken() == "success"

    def refreshToken(self, saveOnFailure=True):
        # Refreshes the tokens. Returns "success", "rejected" (Tesla answered
        # but gave no tokens), "unreachable" (no response), "not_configured"
        # (a Fleet refresh with no teslaApiClientID) or "token_sync".
        # saveOnFailure=False leaves saving a failure's state to the caller.
        if self.master.tokenSyncEnabled():
            # The sync source (TeslaMate) owns and refreshes these tokens.
            # Refreshing here would rotate the token out from under it.
            logger.log(logging.INFO8, "Token sync is on; not refreshing token")
            return "token_sync"
        # Refresh tokens expire in 45
        # days when first issued, so we'll get a new token every 15 days.
        if self.refreshTokenIsOwner():
            # An Owner API token refreshes as in release/1.3.3: client_id
            # "ownerapi" at auth.tesla.com, through the TLS 1.3 session.
            # No teslaApiClientID is needed.
            clientID = "ownerapi"
            myRefreshURL = self.authTokenURL
            post = self.session.post
        else:
            if not self.refreshClientID:
                logger.error(
                    "Cannot refresh Tesla token: no teslaApiClientID configured. "
                    "Register a Fleet API app and set teslaApiClientID in config.json."
                )
                return "not_configured"
            clientID = self.refreshClientID
            # Refresh at the same host the token was issued from: auth.tesla.com
            # for the PKCE/public-client flow, the regional fleet-auth for
            # confidential.
            myRefreshURL = self.authTokenURL if self.usePKCE() else self.fleetRefreshURL
            post = requests.post
        headers = {"accept": "application/json", "Content-Type": "application/json"}
        data = {
            "client_id": clientID,
            "grant_type": "refresh_token",
            "refresh_token": self.getCarApiRefreshToken(),
            "scope": "offline_access",
        }
        req = None
        now = time.time()
        try:
            req = post(myRefreshURL, headers=headers, json=data)
            logger.log(logging.INFO2, "Car API request" + str(req))
            req.raise_for_status()
            apiResponseDict = json.loads(req.text)
        except requests.exceptions.RequestException:
            # req is None when the request never went out (e.g. no network).
            if req is not None and req.status_code == 401:
                logger.log(
                    logging.INFO2,
                    "ERROR: Can't access Tesla car via API.  Please supply fresh tokens.",
                )
                self.clearCarApiBearerToken()
                self.setCarApiRefreshToken("")
            self.updateCarApiLastErrorTime()
            # Instead of just setting carApiLastErrorTime, erase tokens to
            # prevent further authorization attempts until user enters password
            # on web interface. I feel this is safer than trying to log in every
            # ten minutes with a bad token because Tesla might decide to block
            # remote access to your car after too many authorization errors.
            if saveOnFailure:
                self.master.queue_background_task({"cmd": "saveSettings"})
            return "unreachable" if req is None else "rejected"
        except json.decoder.JSONDecodeError:
            logger.log(
                logging.INFO2, "JSON Decode Error parsing API Token Refresh Response"
            )
        except ValueError:
            logger.log(logging.INFO2, "ValueError parsing API Token Refresh Response")
        except Exception as e:
            logger.warning(f"Unexpected error during token refresh: {e}")

        try:
            logger.log(logging.INFO4, "Car API auth response" + str(apiResponseDict))
            self.setCarApiBearerToken(apiResponseDict["access_token"])
            self.setCarApiRefreshToken(apiResponseDict["refresh_token"])
            self.setCarApiTokenExpireTime(now + apiResponseDict["expires_in"])
            self.master.queue_background_task({"cmd": "saveSettings"})
            return "success"

        except Exception as e:
            logger.warning(f"Error processing token refresh response: {e}")
            pass

        return "rejected"

    def refreshTokenIsOwner(self):
        # True when the refresh token is an Owner API one, so apiRefresh uses
        # the ownerapi refresh. A classified (Owner or Fleet) bearer token
        # decides (readOnly is set only for an Owner API token). With no
        # bearer token, e.g. when only a refresh token was pasted, or one of
        # unknown type, the refresh token's own type decides.
        bearer = self.getCarApiBearerToken()
        if bearer and self.classifyToken(self.decodeToken(bearer))[0] != "unknown":
            return self.readOnly
        return self.refreshTokenType(self.getCarApiRefreshToken()) == "owner"

    def refreshTokenType(self, token):
        # "owner" for an Owner API refresh token, a JWT issued by
        # auth.tesla.com/oauth2/v3; "fleet" for anything else, including an
        # opaque token (Fleet refresh tokens are opaque).
        decoded = self.decodeToken(token)
        if decoded is not None and decoded.get("iss") == self.ownerTokenIssuer:
            return "owner"
        return "fleet"

    def refreshTokenIfNeeded(self):
        # Proactively refresh the bearer token if it's missing or close to
        # expiry. Called both from car_api_available() (ahead of vehicle
        # commands) and from read-only callers such as Storm Watch, which
        # otherwise have no other path that keeps the token alive.
        now = time.time()
        if not self.master.tokenSyncEnabled() and (
            self.getCarApiBearerToken() == ""
            or self.getCarApiTokenExpireTime() - now < 60 * 60
        ):
            if self.getCarApiRefreshToken() != "":
                logger.log(logging.INFO8, "Attempting token refresh")
                self.apiRefresh()

    def loadVehicleList(self):
        # Populate carApiVehicles from the Fleet API vehicle list. Returns True
        # if vehicles are (now) known, False if the fetch failed. Safe to call
        # from any thread; concurrent callers will not double-add vehicles.
        with self.vehicleListLock:
            if self.getVehicleCount() > 0:
                return True
            if self.getCarApiBearerToken() == "":
                return False

            req = None
            apiResponseDict = {}
            url = self.vehicleListURL()
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + self.getCarApiBearerToken(),
            }
            try:
                req = requests.get(
                    url, headers=headers, verify=self.verifyCert, timeout=30
                )
                logger.log(logging.INFO8, "Car API cmd vehicles " + str(req))
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

    def car_api_available(self, charge=None, applyLimit=None):
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

        if not self.getCarApiBaseURL():
            # No token yet (and no teslaProxy), or the token type is unknown,
            # which setCarApiBearerToken logged as an ERROR. The main loop's
            # refreshTokenIfNeeded() still covers a token-less start.
            if self.getCarApiBearerToken():
                reason = "token type unknown"
            else:
                reason = "no Tesla API token yet"
            logger.log(
                logging.INFO8,
                "car_api_available returning False: no API base URL (%s)" % reason,
            )
            return False

        # Authenticate to Tesla API
        self.refreshTokenIfNeeded()

        if self.getCarApiBearerToken() != "":
            if self.getVehicleCount() < 1 and not self.loadVehicleList():
                return False

            if (
                self.getVehicleCount() > 0
                and (charge or applyLimit)
                and not self.readOnly
            ):
                # Wake cars if needed
                for vehicle in self.getCarApiVehicles():
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
                            "Don't wake "
                            + vehicle.name
                            + " because it is not at home.",
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

        if (
            now - self.getCarApiLastErrorTime() < (self.getCarApiErrorRetryMins() * 60)
            or self.getCarApiBearerToken() == ""
        ):
            logger.log(
                logging.INFO8,
                "car_api_available returning False because of recent carApiLasterrorTime "
                + str(now - self.getCarApiLastErrorTime())
                + " or empty carApiBearerToken '"
                + self.getCarApiBearerToken()
                + "'",
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

    def is_location_home(self, lat, lon):
        if self.master.getHomeLatLon()[0] == 10000:
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
        if task:
            charge = task.get("charge", None)

        now = time.time()
        apiResponseDict = {}
        if not charge:
            # Whenever we are going to tell vehicles to stop charging, reset
            # per-vehicle wake state so the next start cycle is fresh.
            for vehicle in self.getCarApiVehicles():
                vehicle.stopAskingToStartCharging = False
                vehicle.firstChargeNeededTime = 0
                vehicle.firstWakeAttemptTime = 0
                vehicle.delayNextWakeAttempt = 0

        if now - self.getLastStartOrStopChargeTime() < 60:
            # Don't start or stop more often than once a minute
            logger.log(
                logging.DEBUG2,
                "car_api_charge return because not long enough since last carApiLastStartOrStopChargeTime",
            )
            return "error"

        if self.car_api_available(charge=charge) is False:
            logger.log(
                logging.INFO8,
                "car_api_charge return because car_api_available() == False",
            )
            return "error"

        startOrStop = "start" if charge else "stop"
        result = "success"
        logger.log(logging.INFO8, "startOrStop is set to " + str(startOrStop))

        for vehicle in self.getCarApiVehicles():
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

            # If you send charge_start/stop less than 1 second after calling
            # update_location(), the charge command usually returns:
            #   {'response': {'result': False, 'reason': 'could_not_wake_buses'}}
            # Waiting 2 seconds seems to consistently avoid the error, but let's
            # wait 5 seconds in case of hardware differences between cars.
            time.sleep(5)

            if charge:
                self.applyChargeLimit(self.lastChargeLimitApplied, checkArrival=True)

            url = self.baseURL + "/"
            url = url + str(vehicle.VIN) + "/command/charge_" + startOrStop
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + self.getCarApiBearerToken(),
            }

            # Retry up to 3 times on certain errors.
            for _ in range(0, 3):
                try:
                    req = requests.post(url, headers=headers, verify=self.verifyCert)
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

    def applyChargeLimit(self, limit, checkArrival=False, checkDeparture=False):
        if self.refuseCommandIfReadOnly("set_charge_limit"):
            return "error"
        if limit != -1 and (limit < 50 or limit > 100):
            logger.log(logging.INFO8, "applyChargeLimit skipped")
            return "error"

        now = time.time()
        if (
            not checkArrival
            and not checkDeparture
            and now - self.carApiLastChargeLimitApplyTime < 60
        ):
            # Don't change limits more often than once a minute
            logger.log(
                logging.DEBUG2,
                "applyChargeLimit return because under 60 sec since last carApiLastChargeLimitApplyTime",
            )
            return "error"

        # We need to try to apply limits if:
        #   - We think the car is at home and the limit has changed
        #   - We think the car is at home and we've been asked to check for departures
        #   - We think the car is at home and we notice it gone
        #   - We think the car is away from home and we've been asked to check for arrivals
        #
        # We do NOT opportunistically check for arrivals, because that would be a
        # continuous API poll.
        needToWake = False
        for vehicle in self.carApiVehicles:
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

        if needToWake and self.car_api_available(applyLimit=True) is False:
            logger.log(
                logging.INFO8,
                "applyChargeLimit return because car_api_available() == False",
            )
            return "error"

        if self.lastChargeLimitApplied != limit:
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

        self.carApiLastChargeLimitApplyTime = now

        needSleep = False
        for vehicle in self.carApiVehicles:
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

    def getCarApiBearerToken(self):
        return self.carApiBearerToken

    def getCarApiErrorRetryMins(self, vehicle=None):
        errorCount = self.errorCount
        if vehicle:
            errorCount = max(vehicle.errorCount, errorCount)
        errorCount = max(errorCount - 1, 0)
        return min(errorCount, 10)

    def getCarApiLastErrorTime(self):
        return self.carApiLastErrorTime

    def getCarApiRefreshToken(self):
        return self.carApiRefreshToken

    def vehicleListURL(self):
        # The Owner API no longer lists vehicles under /vehicles (it answers
        # 412 "only available on fleetapi"); /products still works and also
        # returns energy sites, which addVehicle skips because they have no
        # VIN. Per-vehicle endpoints (/vehicles/<VIN>/...) work as they are.
        url = self.getCarApiBaseURL()
        if self.readOnly and url.endswith("/vehicles"):
            return url[: -len("vehicles")] + "products"
        return url

    def getCarApiBaseURL(self):
        return self.baseURL

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

    def getCarApiTokenExpireTime(self):
        return self.carApiTokenExpireTime

    def getLastStartOrStopChargeTime(self):
        return int(self.carApiLastStartOrStopChargeTime)

    def getVehicleByID(self, vehicleID):
        # Returns the vehicle object identified by the given ID
        for vehicle in self.getCarApiVehicles():
            if vehicle.ID == vehicleID:
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
        if self.master.tokenSyncEnabled():
            # The sync source (TeslaMate) owns the tokens. Refuse before the
            # exchange, so nothing is stored and the code is not used up.
            logger.warning(
                "Tesla login refused: TeslaMate token sync is on, so "
                "TWCManager uses TeslaMate's tokens. Turn off syncTokens to "
                "log in here."
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
        # baseURL is left to setCarApiBearerToken, which classifies the token
        # and picks the proxy or the region URL for a Fleet token.

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
            self.setCarApiBearerToken(params["access_token"])
            self.setCarApiRefreshToken(params["refresh_token"])
            self.setCarApiTokenExpireTime(
                time.time() + params.get("expires_in", 8 * 60 * 60)
            )
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
        # Stores a token pasted on its own: an access token as the bearer
        # token, anything else as the refresh token (which refreshes by its
        # type). Returns a status string for the web UI, as saveApiToken does.
        if self.master.tokenSyncEnabled():
            logger.warning(
                "Tesla API token refused: TeslaMate token sync is on, so "
                "TWCManager uses TeslaMate's tokens. Turn off syncTokens to "
                "enter tokens by hand."
            )
            return "token_sync"
        if not token:
            return "error"
        decoded = self.decodeToken(token)
        tokenType = self.classifyToken(decoded)[0]
        isOwnerRefresh = decoded is not None and (
            decoded.get("iss") == self.ownerTokenIssuer and tokenType != "owner"
        )
        if tokenType != "unknown" and not isOwnerRefresh:
            self.pasteAccessToken(token)
            self.master.queue_background_task({"cmd": "saveSettings"})
            return "success"
        return self.pasteRefreshToken(token)

    def pasteAccessToken(self, token):
        # An access token entered on its own (Settings page or paste-back).
        # A stored refresh token of the other type can't refresh it: the
        # refresh would go to the wrong client and, on a 401, clear both
        # tokens. Drop it, so the access token is used until it expires.
        if not self.setCarApiBearerToken(token):
            return False
        tokenType = self.classifyToken(self.decodeToken(token))[0]
        stored = self.getCarApiRefreshToken()
        if (
            stored
            and tokenType in ("owner", "fleet")
            and self.refreshTokenType(stored) != tokenType
        ):
            logger.warning(
                "Dropping the stored Tesla API refresh token: it is a %s "
                "token and the pasted access token is a %s token. Paste the "
                "matching refresh token too, or the access token will not be "
                "refreshed when it expires."
                % (self.refreshTokenType(stored), tokenType)
            )
            self.carApiRefreshToken = ""
        return True

    def pasteRefreshToken(self, token):
        # A refresh token entered on its own (Settings page or paste-back).
        # The stored bearer token may be of the other type, and apiRefresh
        # picks the refresh from the bearer token, so forget it first: the
        # refresh then goes by the refresh token's own type.
        # Returns a status for the web UI, as saveApiToken does:
        #   "success";
        #   "token_rejected" or "not_configured": Tesla rejected the token,
        #     or it needs a teslaApiClientID; the previous tokens are kept;
        #   "unreachable": no response from Tesla; the pasted token is kept
        #     and the usual refresh retries it;
        #   "token_sync": refused, nothing changed.
        if self.master.tokenSyncEnabled():
            # Refuses and logs why.
            self.setCarApiRefreshToken(token)
            return "token_sync"
        if not token:
            return "error"
        previous = (
            self.carApiBearerToken,
            self.carApiRefreshToken,
            self.carApiTokenExpireTime,
            self.baseURL,
            self.verifyCert,
            self.readOnly,
            self.readOnlyRefusalLogged,
            self.carApiLastErrorTime,
            self.errorCount,
        )
        self.clearCarApiBearerToken()
        self.carApiRefreshToken = token
        # Nothing is saved on failure until the outcome is known, so a
        # queued save never writes the cleared state.
        result = self.refreshToken(saveOnFailure=False)
        if result == "success":
            return "success"
        if result == "unreachable":
            logger.warning(
                "Could not reach Tesla to check the pasted refresh token; "
                "keeping it and retrying later."
            )
            self.master.queue_background_task({"cmd": "saveSettings"})
            return "unreachable"
        (
            self.carApiBearerToken,
            self.carApiRefreshToken,
            self.carApiTokenExpireTime,
            self.baseURL,
            self.verifyCert,
            self.readOnly,
            self.readOnlyRefusalLogged,
            self.carApiLastErrorTime,
            self.errorCount,
        ) = previous
        self.master.queue_background_task({"cmd": "saveSettings"})
        logger.warning(
            "Pasted Tesla API refresh token not accepted; previous tokens kept."
        )
        return "token_rejected" if result == "rejected" else result

    def decodeToken(self, token):
        # A token's JWT claims, read without verifying the signature (only
        # the token type and expiry are needed), or None if it is not a JWT.
        try:
            decoded = jwt.decode(
                token,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_exp": False,
                },
            )
        except (jwt.exceptions.InvalidTokenError, TypeError, ValueError):
            return None
        return decoded if isinstance(decoded, dict) else None

    def setCarApiBearerToken(self, token=None, fromSync=False):
        if token:
            if self.master.tokenSyncEnabled() and not fromSync:
                # While token sync is on, only the sync source may set the
                # token; refuse ones from the web UI, settings or a refresh.
                return False
            else:
                self.carApiBearerToken = token
                decoded = self.decodeToken(token)
                self.applyTokenClassification(decoded)

                if decoded and "exp" in decoded:
                    self.setCarApiTokenExpireTime(int(decoded["exp"]))
                else:
                    self.setCarApiTokenExpireTime(time.time() + 8 * 60 * 60)
                return True
        else:
            return False

    def clearCarApiBearerToken(self):
        # Forgets the bearer token, its type and its expiry, e.g. after the
        # refresh token is rejected. (setCarApiBearerToken("") stores
        # nothing.) baseURL is reset as for an unknown token; a configured
        # teslaProxy stays in proxyURL for the next Fleet token. Callers
        # check tokenSyncEnabled() first: a synced token must not be cleared.
        self.carApiBearerToken = ""
        if self.readOnly:
            # Let the next refused command log again.
            self.readOnlyRefusalLogged = False
        self.baseURL = ""
        self.verifyCert = True
        self.readOnly = False
        self.setCarApiTokenExpireTime(time.time())

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

    def applyTokenClassification(self, decoded):
        # Point baseURL, verifyCert and readOnly at the API the current
        # token belongs to. Runs for every accepted token.
        tokenType, region = self.classifyToken(decoded)
        if tokenType == "owner":
            # The Vehicle Command proxy only takes Fleet tokens.
            baseURL, verifyCert, readOnly = self.regionURL["OwnerAPI"], True, True
        elif tokenType == "fleet" and self.proxyURL:
            baseURL, verifyCert, readOnly = self.proxyURL, self.proxyCert, False
        elif tokenType == "fleet":
            baseURL, verifyCert, readOnly = self.regionURL[region], True, False
        else:
            baseURL, verifyCert, readOnly = "", True, False
            logger.error(
                "Unable to determine the Tesla API token type (Owner API or "
                "Fleet API). No Tesla API calls will be made until a "
                "recognised token is supplied."
            )
        if readOnly != self.readOnly:
            # Let the next refused command log again.
            self.readOnlyRefusalLogged = False
        self.baseURL = baseURL
        self.verifyCert = verifyCert
        self.readOnly = readOnly
        logger.info(
            "Tesla API token type: %s, base URL host: %s, read-only: %s"
            % (tokenType, urlsplit(baseURL).netloc or "none", readOnly)
        )

    def setCarApiRefreshToken(self, token):
        if self.master.tokenSyncEnabled():
            # While token sync is on, only the sync source may set the token
            # (applySyncedTokens stores it directly); refuse ones from the web
            # UI, settings or a refresh, and store nothing.
            if token:
                logger.warning(
                    "Tesla API refresh token refused: TeslaMate token sync is "
                    "on, so TWCManager uses TeslaMate's tokens. Turn off "
                    "syncTokens to enter tokens by hand."
                )
            return False
        self.carApiRefreshToken = token
        if (
            token
            and not self.master.tokenSyncEnabled()
            and (
                self.getCarApiBearerToken() == ""
                or self.getCarApiTokenExpireTime() - time.time() < 60 * 60
            )
        ):
            return self.apiRefresh()
        return True

    def setCarApiTokenExpireTime(self, value):
        self.carApiTokenExpireTime = value
        return True

    def restoreCarApiTokenExpireTime(self, value):
        # Settings-load path (TWCMaster.loadSettings). A saved
        # SYNCED_TOKEN_EXPIRY only applies while token sync is on; once it is
        # off, use the stored token's own exp, so TWCManager's normal refresh
        # (or a re-login) follows instead of a token that never refreshes.
        if value == self.SYNCED_TOKEN_EXPIRY and not self.master.tokenSyncEnabled():
            value = time.time()
            try:
                decoded = jwt.decode(
                    self.getCarApiBearerToken(),
                    options={
                        "verify_signature": False,
                        "verify_aud": False,
                        "verify_exp": False,
                    },
                )
                value = int(decoded["exp"])
            except (jwt.exceptions.InvalidTokenError, KeyError, TypeError, ValueError):
                pass
        return self.setCarApiTokenExpireTime(value)

    def refuseCommandIfReadOnly(self, command):
        # True when the current token cannot send vehicle commands. Logs at
        # INFO once per read-only period, makes no HTTP call and records no
        # API error (an error would start the backoff that also blocks reads).
        if not self.readOnly:
            return False
        if not self.readOnlyRefusalLogged:
            logger.info(
                "Tesla API token is read-only (Owner API); not sending "
                + command
                + " or any other vehicle command. Commands need a Fleet API "
                "token or the TeslaBLE module."
            )
            self.readOnlyRefusalLogged = True
        return True

    def applySyncedTokens(self, accessToken, refreshToken):
        # Entry point for token sync sources (TeslaMate). Accepted whether or
        # not tokenSyncEnabled() is already true, and never refreshes.
        if not self.setCarApiBearerToken(accessToken, fromSync=True):
            return False
        # After the bearer token, so its decoded exp cannot replace the
        # sentinel, and before the refresh token, so nothing can refresh with
        # the new refresh token under the decoded exp.
        self.setCarApiTokenExpireTime(self.SYNCED_TOKEN_EXPIRY)
        # Stored directly: setCarApiRefreshToken() may start a refresh.
        self.carApiRefreshToken = refreshToken
        return True

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

        if not self.getCarApiBaseURL():
            # No token yet, or its type is unknown (setCarApiBearerToken
            # logged why).
            return False

        # Do not set charge_rate to 0 as this will effectively stop the car from charging all together
        if charge_rate < 1:
            charge_rate = 1

        vehicle.lastAPIAccessTime = time.time()

        url = self.baseURL + "/"
        url = url + str(vehicle.VIN) + "/command/set_charging_amps"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + self.getCarApiBearerToken(),
        }

        body = {"charging_amps": charge_rate}

        try:
            req = requests.post(url, headers=headers, json=body, verify=self.verifyCert)
            logger.log(
                logging.INFO8,
                f"Car API cmd set_charging_amps {charge_rate}A {str(req)}",
            )
            apiResponseDict = json.loads(req.text)
        except requests.exceptions.RequestException:
            return False
        except json.decoder.JSONDecodeError:
            return False

        # Set charge rates < 5 twice, see https://github.com/tdorssers/TeslaPy/pull/42
        if charge_rate < 5 and not set_again:
            time.sleep(5)
            return self.setChargeRate(charge_rate, vehicle, set_again=True)
        else:
            return apiResponseDict

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
        for car in self.carApiVehicles:
            if car.atHome:
                car.update_charge()
        self.lastChargeCheck = time.time()

    def vehicleIsDefinitelyHome(self, vin):
        for car in self.carApiVehicles:
            if car.VIN == vin:
                if not car.atHome:
                    updated = car.update_location(0)
                    if updated and not car.atHome:
                        logger.log(
                            logging.INFO2,
                            car.name
                            + " was connected to a TWC, so is definitely home.",
                        )
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
        if not self.getCarApiBaseURL():
            # No token yet, or its type is unknown (setCarApiBearerToken
            # logged why).
            return False
        apiResponseDict = None
        vehicle.lastAPIAccessTime = time.time()

        url = self.baseURL + "/"
        url = url + str(vehicle.VIN) + "/wake_up"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + self.getCarApiBearerToken(),
        }
        req = None
        try:
            req = requests.post(url, headers=headers, verify=self.verifyCert)
            logger.log(logging.INFO8, "Car API cmd wake_up" + str(req))
            req.raise_for_status()
            apiResponseDict = json.loads(req.text)
        except requests.exceptions.RequestException:
            if req is None:
                # The request never went out (e.g. no network).
                pass
            elif req.status_code == 401 and "expired" in req.text:
                # If the token is expired, refresh it and try again
                if self.apiRefresh():
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
    name = ""
    syncSource = "TeslaAPI"
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

    batteryLevel = 10000
    chargeLimit = -1
    lat = 10000
    lon = 10000
    atHome = False
    timeToFullCharge = 0.0
    availableCurrent = 0
    _actualCurrent = 0
    lastCurrentChangeTime = 0
    phases = 0
    voltage = 0
    chargingState = "Unknown"

    # Sync values are updated by an external module such as TeslaMate
    syncTimestamp = 0
    syncTimeout = 60 * 60
    syncLat = 10000
    syncLon = 10000
    syncState = "asleep"

    def __init__(self, json, carapi, config):
        self.carapi = carapi
        self.__config = config
        self.ID = json["id"]
        self.VIN = json["vin"]
        self.name = json["display_name"] or self.VIN or str(self.ID) or "unknown"

        # Launch sync monitoring thread
        thread = Thread(target=self.checkSyncNotStale)
        thread.daemon = True
        thread.start()

    @property
    def verifyCert(self):
        # Follows the current token: the proxy certificate only applies
        # while requests go to teslaProxy.
        return self.carapi.verifyCert

    def checkSyncNotStale(self):
        # Once an external system begins providing sync functionality to defer
        # Tesla API queries and provide already fetched information, there is a
        # potential condition which may occur in which the external system goes
        # away and leaves us with stale data.

        # To guard against this, this threaded function will loop every x minutes
        # and check the last sync timestamp. If it has not updated in that interval,
        # we switch back to using the API

        while True:
            if (
                self.syncSource != "TeslaAPI"
                and self.is_awake()
                and (self.syncTimestamp < (time.time() - self.syncTimeout))
            ):
                logger.error(
                    "Data from "
                    + self.syncSource
                    + " for "
                    + self.name
                    + " is stale. Switching back to TeslaAPI"
                )
                self.syncSource = "TeslaAPI"
            time.sleep(self.syncTimeout)

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

    def is_awake(self):
        if self.syncSource != "TeslaAPI" and self.syncTimestamp > 0:
            # External sync (e.g. TeslaMate) has provided at least one update.
            if self.syncState in ("asleep", "offline"):
                # Car is known to be asleep - trust the sync source and avoid
                # Fleet API calls that cost money just to confirm it's still asleep.
                return False
            if self.syncTimestamp >= (time.time() - self.syncTimeout / 4):
                # Fresh data and car is not asleep - trust it.
                return self.syncState in ("online", "charging", "updating", "driving")
            # Stale data but car was last known active - fall through to API check
            # in case the sync source dropped while the car was still awake.

        now = time.time()
        # Don't check more often than every 2 minutes
        if now - self.lastVehicleAwakeTime < 120:
            return self.lastVehicleAwakeState
        url = self.carapi.getCarApiBaseURL() + "/" + str(self.VIN)
        result, response = self.get_car_api(
            url, checkReady=False, provesOnline=False
        )
        awakeState = result and response.get("state", "") == "online"
        self.lastVehicleAwakeState = awakeState
        self.lastVehicleAwakeTime = now
        return awakeState

    def get_car_api(self, url, checkReady=True, provesOnline=True):
        if not self.carapi.getCarApiBaseURL():
            # No token yet, or its type is unknown (setCarApiBearerToken
            # logged why).
            return False, None

        if checkReady and not self.ready():
            return False, None

        apiResponseDict = {}

        # Retry up to 3 times on certain errors.
        for _ in range(0, 3):
            # Built per attempt, so a retry after a refresh sends the new token.
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + self.carapi.getCarApiBearerToken(),
            }
            req = None
            try:
                req = requests.get(url, headers=headers, verify=self.verifyCert)
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
                elif req.status_code == 401 and "expired" in req.text:
                    # If the token is expired, refresh it and try again
                    if self.carapi.apiRefresh():
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

    def update_location(self, minInterval=0):
        if self.syncSource == "TeslaAPI":
            if minInterval > 0:
                # Caller only needs fresh data if older than minInterval seconds.
                # Set statusDeferral to skip the API call if data is recent enough.
                now = time.time()
                if now - self.lastAPIAccessTime < minInterval:
                    return True
            return self.update_vehicle_data()

        else:
            self.lat = self.syncLat
            self.lon = self.syncLon
            self.atHome = self.carapi.is_location_home(self.lat, self.lon)

            return True

    def update_vehicle_data(self):
        url = (
            "/".join([self.carapi.getCarApiBaseURL(), str(self.VIN), "vehicle_data"])
            + "?endpoints="
            + "%3B".join(
                ["location_data", "charge_state", "drive_state", "vehicle_state"]
            )
        )

        now = time.time()

        if now < self.statusDeferral:
            return True

        try:
            result, response = self.get_car_api(url)
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

            self.atHome = self.carapi.is_location_home(self.lat, self.lon)

            self.chargeLimit = charge.get("charge_limit_soc", self.chargeLimit)
            self.batteryLevel = charge.get("battery_level", self.batteryLevel)
            self.timeToFullCharge = charge.get(
                "time_to_full_charge", self.timeToFullCharge
            )

            try:
                self.name = response["vehicle_state"]["vehicle_name"] or self.name
            except (KeyError, TypeError):
                pass

            self.availableCurrent = charge.get(
                "charger_pilot_current", self.availableCurrent
            )
            self.actualCurrent = charge.get(
                "charger_actual_current", self.actualCurrent
            )
            self.phases = charge.get("charger_phases", self.phases)
            self.voltage = charge.get("charger_voltage", self.voltage)
            self.chargingState = charge.get("charging_state", self.chargingState)

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
        if self.syncSource == "TeslaAPI":
            return self.update_vehicle_data()

        else:
            return True

    @property
    def actualCurrent(self):
        return self._actualCurrent

    @actualCurrent.setter
    def actualCurrent(self, value):
        if value != self._actualCurrent:
            self._actualCurrent = value
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

        url = self.carapi.getCarApiBaseURL() + "/"
        url = url + str(self.VIN) + "/command/set_charge_limit"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + self.carapi.getCarApiBearerToken(),
        }
        body = {"percent": limit}

        for _ in range(0, 3):
            try:
                req = requests.post(
                    url, headers=headers, json=body, verify=self.verifyCert
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
