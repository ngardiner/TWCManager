import base64
import hashlib
import os
import re
import requests
import time
from urllib.parse import parse_qs


class TeslaAPI:

    import json

    authURL = "https://auth.tesla.com/oauth2/v3/authorize"
    callbackURL = "https://auth.tesla.com/void/callback"
    carApiLastErrorTime = 0
    carApiBearerToken = ""
    carApiRefreshToken = ""
    carApiTokenExpireTime = time.time()
    carApiLastStartOrStopChargeTime = 0
    carApiLastChargeLimitApplyTime = 0
    clientID = "81527cff06843c8634fdc09e8ac0abefb46ac849f38fe1e431c2ef2106796384"
    clientSecret = "c7257eb71a564034f9419ee651c7d0e5f7aa6bfbd18bafb5c5c033b093bb2fa3"
    lastChargeLimitApplied = 0
    lastChargeCheck = 0
    chargeUpdateInterval = 1800
    carApiVehicles = []
    config = None
    debugLevel = 0
    master = None
    maxLoginRetries = 10
    minChargeLevel = -1
    refreshURL = "https://owner-api.teslamotors.com/oauth/token"
    verifier = ""

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
    ]

    # Define minutes between retrying non-transient errors.
    carApiErrorRetryMins = 10

    def __init__(self, master):
        self.master = master
        try:
            self.config = master.config
            self.debugLevel = self.config["config"].get("debugLevel", 1)
            self.minChargeLevel = self.config["config"].get("minChargeLevel", -1)
            self.chargeUpdateInterval = self.config["config"].get(
                "cloudUpdateInterval", 1800
            )
        except KeyError:
            pass

    def addVehicle(self, json):
        self.carApiVehicles.append(CarApiVehicle(json, self, self.config))
        return True

    def apiLogin(self, email, password):

        # GET parameters are used both for phase 1 and phase 2
        params = None

        for attempt in range(self.maxLoginRetries):

            self.verifier = base64.urlsafe_b64encode(os.urandom(86)).rstrip(b"=")
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier).digest()
            ).rstrip(b"=")
            state = (
                base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("utf-8")
            )

            params = (
                ("client_id", "ownerapi"),
                ("code_challenge", challenge),
                ("code_challenge_method", "S256"),
                ("redirect_uri", self.callbackURL),
                ("response_type", "code"),
                ("scope", "openid email offline_access"),
                ("state", state),
            )

            session = requests.Session()
            resp = session.get(self.authURL, params=params)

            if resp.ok and "<title>" in resp.text:
                self.master.debugLog(
                    6,
                    "TeslaAPI",
                    "Tesla Auth form fetch success, attempt: " + str(attempt),
                )
                break
            else:
                self.master.debugLog(
                    6,
                    "TeslaAPI",
                    "Tesla auth form fetch failed, attempt: " + str(attempt),
                )

            time.sleep(3)
        else:
            self.master.debugLog(
                2,
                "TeslaAPI",
                "Wasn't able to find authentication form after "
                + str(attempt)
                + " attempts",
            )
            return "Phase1Error"

        csrf = re.search(r'name="_csrf".+value="([^"]+)"', resp.text).group(1)
        transaction_id = re.search(
            r'name="transaction_id".+value="([^"]+)"', resp.text
        ).group(1)

        if not csrf or not transaction_id:
            # These two parameters are required for Phase 1 (Authentication) auth
            # If they are missing, we raise an appropriate error to the user's attention
            return "Phase1Error"

        data = {
            "_csrf": csrf,
            "_phase": "authenticate",
            "_process": "1",
            "transaction_id": transaction_id,
            "cancel": "",
            "identity": email,
            "credential": password,
        }

        for attempt in range(self.maxLoginRetries):
            resp = session.post(
                self.authURL, params=params, data=data, allow_redirects=False
            )
            if resp.ok and (resp.status_code == 302 or "<title>" in resp.text):
                self.master.debugLog(
                    2,
                    "TeslaAPI",
                    "Posted auth form successfully after " + str(attempt) + " attempts",
                )
                break
            time.sleep(3)
        else:
            self.master.debugLog(
                2,
                "TeslaAPI",
                "Wasn't able to post authentication form after "
                + str(attempt)
                + " attempts",
            )
            return "Phase2Error"

        if resp.status_code == 200 and "/mfa/verify" in resp.text:
            # This account is using MFA, redirect to MFA code entry page
            return "MFA"

        try:
            code = parse_qs(resp.headers["location"])[self.callbackURL + "?code"]
        except KeyError:
            return "Phase2ErrorTip"

        data = {
            "grant_type": "authorization_code",
            "client_id": "ownerapi",
            "code_verifier": self.verifier.decode("utf-8"),
            "code": code,
            "redirect_uri": self.callbackURL,
        }

        resp = session.post("https://auth.tesla.com/oauth2/v3/token", json=data)
        access_token = resp.json()["access_token"]

        headers = {"authorization": "bearer " + access_token}

        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "client_id": self.clientID,
        }
        resp = session.post(
            "https://owner-api.teslamotors.com/oauth/token", headers=headers, json=data
        )
        try:
            self.setCarApiBearerToken(resp.json()["access_token"])
            self.setCarApiRefreshToken(resp.json()["refresh_token"])
            self.setCarApiTokenExpireTime(time.time() + resp.json()["expires_in"])
            self.master.queue_background_task({"cmd": "saveSettings"})

        except KeyError:
            self.master.debugLog(
                2,
                "TeslaAPI",
                "ERROR: Can't access Tesla car via API.  Please log in again via web interface.",
            )
            self.updateCarApiLastErrorTime()
            # Instead of just setting carApiLastErrorTime, erase tokens to
            # prevent further authorization attempts until user enters password
            # on web interface. I feel this is safer than trying to log in every
            # ten minutes with a bad token because Tesla might decide to block
            # remote access to your car after too many authorization errors.
            self.setCarApiBearerToken("")
            self.setCarApiRefreshToken("")
            self.master.queue_background_task({"cmd": "saveSettings"})

    def apiRefresh(self):
        # Refresh tokens expire in 45
        # days when first issued, so we'll get a new token every 15 days.
        headers = {
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        data = {
            "client_id": self.clientID,
            "client_secret": self.clientSecret,
            "grant_type": "refresh_token",
            "refresh_token": self.getCarApiRefreshToken(),
        }
        req = None
        try:
            req = requests.post(self.refreshURL, headers=headers, json=data)
            self.master.debugLog(2, "TeslaAPI", "Car API request" + str(req))
            apiResponseDict = self.json.loads(req.text)
        except:
            pass

        try:
            self.master.debugLog(
                4, "TeslaAPI", "Car API auth response" + str(apiResponseDict)
            )
            self.setCarApiBearerToken(apiResponseDict["access_token"])
            self.setCarApiRefreshToken(apiResponseDict["refresh_token"])
            self.setCarApiTokenExpireTime(now + apiResponseDict["expires_in"])
            self.master.queue_background_task({"cmd": "saveSettings"})

        except KeyError:
            self.master.debugLog(
                2,
                "TeslaAPI",
                "ERROR: Can't access Tesla car via API.  Please log in again via web interface.",
            )
            self.updateCarApiLastErrorTime()
            # Instead of just setting carApiLastErrorTime, erase tokens to
            # prevent further authorization attempts until user enters password
            # on web interface. I feel this is safer than trying to log in every
            # ten minutes with a bad token because Tesla might decide to block
            # remote access to your car after too many authorization errors.
            self.setCarApiBearerToken("")
            self.setCarApiRefreshToken("")
            self.master.queue_background_task({"cmd": "saveSettings"})

    def car_api_available(
        self, email=None, password=None, charge=None, applyLimit=None
    ):
        now = time.time()
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
            self.master.debugLog(
                6,
                "TeslaAPI",
                "Car API disabled for "
                + str(self.getCarApiRetryRemaining())
                + " more seconds due to recent error.",
            )
            return False
        else:
            self.master.debugLog(
                8,
                "TeslaAPI",
                "Entering car_api_available - next step is to query Tesla API",
            )

        # Authentiate to Tesla API
        if (
            self.getCarApiBearerToken() == ""
            or self.getCarApiTokenExpireTime() - now < 30 * 24 * 60 * 60
        ):
            if self.getCarApiRefreshToken() != "":
                headers = {
                    "accept": "application/json",
                    "Content-Type": "application/json",
                }
                data = {
                    "client_id": self.clientID,
                    "client_secret": self.clientSecret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.getCarApiRefreshToken(),
                }
                self.master.debugLog(8, "TeslaAPI", "Attempting token refresh")
                self.apiRefresh()

            elif email != None and password != None:
                self.master.debugLog(8, "TeslaAPI", "Attempting password auth")
                ret = self.apiLogin(email, password)

                # If any string is returned, we redirect to it. This helps with MFA login flow
                if (
                    str(ret) != "True"
                    and str(ret) != "False"
                    and str(ret) != ""
                    and str(ret) != "None"
                ):
                    return ret

        if self.getCarApiBearerToken() != "":
            if self.getVehicleCount() < 1:
                url = "https://owner-api.teslamotors.com/api/1/vehicles"
                headers = {
                    "accept": "application/json",
                    "Authorization": "Bearer " + self.getCarApiBearerToken(),
                }
                try:
                    req = requests.get(url, headers=headers)
                    self.master.debugLog(
                        8, "TeslaAPI", "Car API cmd vehicles " + str(req)
                    )
                    apiResponseDict = self.json.loads(req.text)
                except:
                    self.master.debugLog(
                        1, "TeslaAPI", "Failed to make API call " + url
                    )
                    self.master.debugLog(6, "TeslaAPI", "Response: " + req.text)
                    pass

                try:
                    self.master.debugLog(
                        10,
                        "TeslaAPI",
                        "Car API vehicle list" + str(apiResponseDict) + "\n",
                    )

                    for i in range(0, apiResponseDict["count"]):
                        self.addVehicle(apiResponseDict["response"][i])
                except (KeyError, TypeError):
                    # This catches cases like trying to access
                    # apiResponseDict['response'] when 'response' doesn't exist in
                    # apiResponseDict.
                    self.master.debugLog(
                        2,
                        "TeslaAPI",
                        "ERROR: Can't get list of vehicles via Tesla car API.  Will try again in "
                        + str(self.getCarApiErrorRetryMins())
                        + " minutes.",
                    )
                    self.updateCarApiLastErrorTime()
                    return False

            needSleep = False
            if self.getVehicleCount() > 0 and (charge or applyLimit):
                # Wake cars if needed
                for vehicle in self.getCarApiVehicles():
                    if charge == True and vehicle.stopAskingToStartCharging:
                        # Vehicle is in a state (complete or charging) already
                        # which doesn't make sense for us to keep requesting it
                        # to start charging, so we will stop.
                        self.master.debugLog(
                            11,
                            "TeslaAPI",
                            "Don't repeatedly request API to charge "
                            + vehicle.name
                            + ", because vehicle.stopAskingToStartCharging "
                            + " == True - it has already been requested.",
                        )
                        continue

                    if applyLimit == True and vehicle.stopTryingToApplyLimit:
                        self.master.debugLog(
                            11,
                            "TeslaAPI",
                            "Don't wake "
                            + vehicle.name
                            + " to set the charge limit - it has already been set",
                        )
                        continue

                    if self.getCarApiRetryRemaining():
                        # It's been under carApiErrorRetryMins minutes since the car
                        # API generated an error on this vehicle. Don't send it more
                        # commands yet.
                        self.master.debugLog(
                            11,
                            "TeslaAPI",
                            "Don't send commands to "
                            + vehicle.name
                            + " because it returned an error in the last "
                            + str(self.getCarApiErrorRetryMins())
                            + " minutes.",
                        )
                        continue

                    if vehicle.ready():
                        continue

                    if now - vehicle.lastAPIAccessTime <= vehicle.delayNextWakeAttempt:
                        self.master.debugLog(
                            10,
                            "TeslaAPI",
                            "car_api_available returning False because we are still delaying "
                            + str(vehicle.delayNextWakeAttempt)
                            + " seconds after the last failed wake attempt.",
                        )
                        return False

                    # It's been delayNextWakeAttempt seconds since we last failed to
                    # wake the car, or it's never been woken. Wake it.
                    vehicle.lastAPIAccessTime = now
                    url = "https://owner-api.teslamotors.com/api/1/vehicles/"
                    url = url + str(vehicle.ID) + "/wake_up"

                    headers = {
                        "accept": "application/json",
                        "Authorization": "Bearer " + self.getCarApiBearerToken(),
                    }
                    try:
                        req = requests.post(url, headers=headers)
                        self.master.debugLog(
                            8, "TeslaAPI", "Car API cmd wake_up" + str(req)
                        )
                        apiResponseDict = self.json.loads(req.text)
                    except:
                        pass

                    state = "error"
                    self.master.debugLog(
                        10,
                        "TeslaAPI",
                        "Car API wake car response" + str(apiResponseDict),
                    )
                    try:
                        state = apiResponseDict["response"]["state"]

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
                        vehicle.delayNextWakeAttempt = 0
                        # Don't alter vehicle.lastAPIAccessTime because
                        # vehicle.ready() uses it to return True if the last wake
                        # was under 2 mins ago.
                        needSleep = True
                    else:
                        if vehicle.firstWakeAttemptTime == 0:
                            vehicle.firstWakeAttemptTime = now

                        if state == "asleep" or state == "waking":
                            if now - vehicle.firstWakeAttemptTime <= 10 * 60:
                                # http://visibletesla.com has a 'force wakeup' mode
                                # that sends wake_up messages once every 5 seconds
                                # 15 times. This generally manages to wake my car if
                                # it's returning 'asleep' state, but I don't think
                                # there is any reason for 5 seconds and 15 attempts.
                                # The car did wake in two tests with that timing,
                                # but on the third test, it had not entered online
                                # mode by the 15th wake_up and took another 10+
                                # seconds to come online. In general, I hear relays
                                # in the car clicking a few seconds after the first
                                # wake_up but the car does not enter 'waking' or
                                # 'online' state for a random period of time. I've
                                # seen it take over one minute, 20 sec.
                                #
                                # I interpret this to mean a car in 'asleep' mode is
                                # still receiving car API messages and will start
                                # to wake after the first wake_up, but it may take
                                # awhile to finish waking up. Therefore, we try
                                # waking every 30 seconds for the first 10 mins.
                                vehicle.delayNextWakeAttempt = 30
                            elif now - vehicle.firstWakeAttemptTime <= 70 * 60:
                                # Cars in 'asleep' state should wake within a
                                # couple minutes in my experience, so we should
                                # never reach this point. If we do, try every 5
                                # minutes for the next hour.
                                vehicle.delayNextWakeAttempt = 5 * 60
                            else:
                                # Car hasn't woken for an hour and 10 mins. Try
                                # again in 15 minutes. We'll show an error about
                                # reaching this point later.
                                vehicle.delayNextWakeAttempt = 15 * 60
                        elif state == "offline":
                            # In any case it can make sense to wait 5 seconds here.
                            # I had the issue, that the next command was sent too
                            # fast and only a reboot of the Raspberry resultet in
                            # possible reconnect to the API (even the Tesla App
                            # couldn't connect anymore).
                            time.sleep(5)
                            if now - vehicle.firstWakeAttemptTime <= 31 * 60:
                                # A car in offline state is presumably not connected
                                # wirelessly so our wake_up command will not reach
                                # it. Instead, the car wakes itself every 20-30
                                # minutes and waits some period of time for a
                                # message, then goes back to sleep. I'm not sure
                                # what the period of time is, so I tried sending
                                # wake_up every 55 seconds for 16 minutes but the
                                # car failed to wake.
                                # Next I tried once every 25 seconds for 31 mins.
                                # This worked after 19.5 and 19.75 minutes in 2
                                # tests but I can't be sure the car stays awake for
                                # 30secs or if I just happened to send a command
                                # during a shorter period of wakefulness.
                                vehicle.delayNextWakeAttempt = 25

                                # I've run tests sending wake_up every 10-30 mins to
                                # a car in offline state and it will go hours
                                # without waking unless you're lucky enough to hit
                                # it in the brief time it's waiting for wireless
                                # commands. I assume cars only enter offline state
                                # when set to max power saving mode, and even then,
                                # they don't always enter the state even after 8
                                # hours of no API contact or other interaction. I've
                                # seen it remain in 'asleep' state when contacted
                                # after 16.5 hours, but I also think I've seen it in
                                # offline state after less than 16 hours, so I'm not
                                # sure what the rules are or if maybe Tesla contacts
                                # the car periodically which resets the offline
                                # countdown.
                                #
                                # I've also seen it enter 'offline' state a few
                                # minutes after finishing charging, then go 'online'
                                # on the third retry every 55 seconds.  I suspect
                                # that might be a case of the car briefly losing
                                # wireless connection rather than actually going
                                # into a deep sleep.
                                # 'offline' may happen almost immediately if you
                                # don't have the charger plugged in.
                        else:
                            # Handle 'error' state.
                            if now - vehicle.firstWakeAttemptTime <= 60 * 60:
                                # We've tried to wake the car for less than an
                                # hour.
                                foundKnownError = False
                                if "error" in apiResponseDict:
                                    error = apiResponseDict["error"]
                                    for knownError in self.getCarApiTransientErrors():
                                        if knownError == error[0 : len(knownError)]:
                                            foundKnownError = True
                                            break

                                if foundKnownError:
                                    # I see these errors often enough that I think
                                    # it's worth re-trying in 1 minute rather than
                                    # waiting 5 minutes for retry in the standard
                                    # error handler.
                                    vehicle.delayNextWakeAttempt = 60
                                else:
                                    # by the API servers being down, car being out of
                                    # range, or by something I can't anticipate. Try
                                    # waking the car every 5 mins.
                                    vehicle.delayNextWakeAttempt = 5 * 60
                            else:
                                # Car hasn't woken for over an hour. Try again
                                # in 15 minutes. We'll show an error about this
                                # later.
                                vehicle.delayNextWakeAttempt = 15 * 60

                        if state == "error":
                            self.master.debugLog(
                                1,
                                "TeslaAPI",
                                "Car API wake car failed with unknown response.  "
                                + "Will try again in "
                                + str(vehicle.delayNextWakeAttempt)
                                + " seconds.",
                            )
                        else:
                            self.master.debugLog(
                                1,
                                "TeslaAPI",
                                "Car API wake car failed.  State remains: '"
                                + state
                                + "'.  Will try again in "
                                + str(vehicle.delayNextWakeAttempt)
                                + " seconds.",
                            )

                    if (
                        vehicle.firstWakeAttemptTime > 0
                        and now - vehicle.firstWakeAttemptTime > 60 * 60
                    ):
                        # It should never take over an hour to wake a car.  If it
                        # does, ask user to report an error.
                        self.master.debugLog(
                            1,
                            "TeslaAPI",
                            "ERROR: We have failed to wake a car from '"
                            + state
                            + "' state for %.1f hours.\n"
                            "Please private message user CDragon at "
                            "http://teslamotorsclub.com with a copy of this error. "
                            "Also include this: %s"
                            % (
                                ((now - vehicle.firstWakeAttemptTime) / 60 / 60),
                                str(apiResponseDict),
                            ),
                        )

        if (
            now - self.getCarApiLastErrorTime() < (self.getCarApiErrorRetryMins() * 60)
            or self.getCarApiBearerToken() == ""
        ):
            self.master.debugLog(
                8,
                "TeslaAPI",
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
        self.master.debugLog(8, "TeslaAPI", "car_api_available returning True")

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

    def is_location_home(self, lat, lon):

        if self.master.getHomeLatLon()[0] == 10000:
            self.master.debugLog(
                1,
                "TeslaAPI",
                "Home location for vehicles has never been set.  "
                + "We'll assume home is where we found the first vehicle currently parked.  "
                + "Home set to lat="
                + str(lat)
                + ", lon="
                + str(lon),
            )
            self.master.setHomeLat(lat)
            self.master.setHomeLon(lon)
            self.master.queue_background_task({"cmd": "saveSettings"})
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
        if (
            abs(self.master.getHomeLatLon()[0] - lat) > 0.0289
            or abs(self.master.getHomeLatLon()[1] - lon) > 0.0289
        ):
            return False

        return True

    def car_api_charge(self, charge):
        # Do not call this function directly.  Call by using background thread:
        # queue_background_task({'cmd':'charge', 'charge':<True/False>})

        now = time.time()
        apiResponseDict = {}
        if not charge:
            # Whenever we are going to tell vehicles to stop charging, set
            # vehicle.stopAskingToStartCharging = False on all vehicles.
            for vehicle in self.getCarApiVehicles():
                vehicle.stopAskingToStartCharging = False

        if now - self.getLastStartOrStopChargeTime() < 60:
            # Don't start or stop more often than once a minute
            self.master.debugLog(
                11,
                "TeslaAPI",
                "car_api_charge return because under 60 sec since last carApiLastStartOrStopChargeTime",
            )
            return "error"

        if self.car_api_available(charge=charge) == False:
            self.master.debugLog(
                8,
                "TeslaAPI",
                "car_api_charge return because car_api_available() == False",
            )
            return "error"

        startOrStop = "start" if charge else "stop"
        result = "success"
        self.master.debugLog(8, "TeslaAPI", "startOrStop is set to " + str(startOrStop))

        for vehicle in self.getCarApiVehicles():
            if charge and vehicle.stopAskingToStartCharging:
                self.master.debugLog(
                    8,
                    "TeslaAPI",
                    "Don't charge "
                    + vehicle.name
                    + " because vehicle.stopAskingToStartCharging == True",
                )
                continue

            if not vehicle.ready():
                continue

            if (
                vehicle.update_charge()
                and vehicle.batteryLevel < self.minChargeLevel
                and not charge
            ):
                # If the vehicle's charge state is lower than the configured minimum,
                #   don't stop it from charging, even if we'd otherwise not charge.
                continue

            # Only update carApiLastStartOrStopChargeTime if car_api_available() managed
            # to wake cars.  Setting this prevents any command below from being sent
            # more than once per minute.
            self.updateLastStartOrStopChargeTime()

            if (
                self.config["config"]["onlyChargeMultiCarsAtHome"]
                and self.getVehicleCount() > 1
            ):
                # When multiple cars are enrolled in the car API, only start/stop
                # charging cars parked at home.

                if vehicle.update_location() == False:
                    result = "error"
                    continue

                if not vehicle.atHome:
                    # Vehicle is not at home, so don't change its charge state.
                    self.master.debugLog(
                        1,
                        "TeslaAPI",
                        vehicle.name
                        + " is not at home.  Do not "
                        + startOrStop
                        + " charge.",
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

            url = "https://owner-api.teslamotors.com/api/1/vehicles/"
            url = url + str(vehicle.ID) + "/command/charge_" + startOrStop
            headers = {
                "accept": "application/json",
                "Authorization": "Bearer " + self.getCarApiBearerToken(),
            }

            # Retry up to 3 times on certain errors.
            for _ in range(0, 3):
                try:
                    req = requests.post(url, headers=headers)
                    self.master.debugLog(
                        8,
                        "TeslaAPI",
                        "Car API cmd charge_" + startOrStop + " " + str(req),
                    )
                    apiResponseDict = self.json.loads(req.text)
                except:
                    pass

                try:
                    self.master.debugLog(
                        4,
                        "TeslaAPI",
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
                    if apiResponseDict["response"] == None:
                        if "error" in apiResponseDict:
                            foundKnownError = False
                            error = apiResponseDict["error"]
                            for knownError in self.getCarApiTransientErrors():
                                if knownError == error[0 : len(knownError)]:
                                    # I see these errors often enough that I think
                                    # it's worth re-trying in 1 minute rather than
                                    # waiting carApiErrorRetryMins minutes for retry
                                    # in the standard error handler.
                                    self.master.debugLog(
                                        1,
                                        "TeslaAPI",
                                        "Car API returned '"
                                        + error
                                        + "' when trying to start charging.  Try again in 1 minute.",
                                    )
                                    time.sleep(60)
                                    foundKnownError = True
                                    break
                            if foundKnownError:
                                continue

                        # This generally indicates a significant error like 'vehicle
                        # unavailable', but it's not something I think the caller can do
                        # anything about, so return generic 'error'.
                        result = "error"
                        # Don't send another command to this vehicle for
                        # carApiErrorRetryMins mins.
                        vehicle.lastErrorTime = now
                    elif apiResponseDict["response"]["result"] == False:
                        if charge:
                            reason = apiResponseDict["response"]["reason"]
                            if reason == "complete" or reason == "charging":
                                # We asked the car to charge, but it responded that
                                # it can't, either because it's reached target
                                # charge state (reason == 'complete'), or it's
                                # already trying to charge (reason == 'charging').
                                # In these cases, it won't help to keep asking it to
                                # charge, so set vehicle.stopAskingToStartCharging =
                                # True.
                                #
                                # Remember, this only means at least one car in the
                                # list wants us to stop asking and we don't know
                                # which car in the list is connected to our TWC.
                                self.master.debugLog(
                                    1,
                                    "TeslaAPI",
                                    vehicle.name
                                    + " is done charging or already trying to charge.  Stop asking to start charging.",
                                )
                                vehicle.stopAskingToStartCharging = True
                            else:
                                # Car was unable to charge for some other reason, such
                                # as 'could_not_wake_buses'.
                                if reason == "could_not_wake_buses":
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
                                    # Start or stop charge failed with an error I
                                    # haven't seen before, so wait
                                    # carApiErrorRetryMins mins before trying again.
                                    self.master.debugLog(
                                        1,
                                        "TeslaAPI",
                                        'ERROR "'
                                        + reason
                                        + '" when trying to '
                                        + startOrStop
                                        + " car charging via Tesla car API.  Will try again later."
                                        + "\nIf this error persists, please private message user CDragon at http://teslamotorsclub.com with a copy of this error.",
                                    )
                                    result = "error"
                                    vehicle.lastErrorTime = now

                except (KeyError, TypeError):
                    # This catches cases like trying to access
                    # apiResponseDict['response'] when 'response' doesn't exist in
                    # apiResponseDict.
                    self.master.debugLog(
                        1,
                        "TeslaAPI",
                        "ERROR: Failed to "
                        + startOrStop
                        + " car charging via Tesla car API.  Will try again later.",
                    )
                    vehicle.lastErrorTime = now
                break

        if (
            self.config["config"]["debugLevel"] >= 1
            and self.getLastStartOrStopChargeTime() == now
        ):
            self.master.debugLog(
                1, "TeslaAPI", "Car API " + startOrStop + " charge result: " + result
            )

        return result

    def applyChargeLimit(self, limit, checkArrival=False, checkDeparture=False):

        if limit != -1 and (limit < 50 or limit > 100):
            self.master.debugLog(8, "TeslaAPI", "applyChargeLimit skipped")
            return "error"

        if self.car_api_available() == False:
            self.master.debugLog(
                8,
                "TeslaAPI",
                "applyChargeLimit return because car_api_available() == False",
            )
            return "error"

        now = time.time()
        if (
            not checkArrival
            and not checkDeparture
            and now - self.carApiLastChargeLimitApplyTime < 60
        ):
            # Don't change limits more often than once a minute
            self.master.debugLog(
                11,
                "TeslaAPI",
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
            (wasAtHome, outside, lastApplied) = self.master.getNormalChargeLimit(
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
                    or (vehicle.update_location(cacheTime=3600) and not vehicle.atHome)
                )
            ) or (not wasAtHome and checkArrival):
                vehicle.stopTryingToApplyLimit = False

        if needToWake and self.car_api_available(applyLimit=True) == False:
            self.master.debugLog(
                8,
                "TeslaAPI",
                "applyChargeLimit return because car_api_available() == False",
            )
            return "error"

        if self.lastChargeLimitApplied != limit:
            if limit != -1:
                self.master.debugLog(
                    2,
                    "TeslaAPI",
                    "Attempting to apply limit of "
                    + str(limit)
                    + "% to all vehicles at home",
                )
            else:
                self.master.debugLog(
                    2,
                    "TeslaAPI",
                    "Attempting to restore charge limits for all vehicles at home",
                )
            self.lastChargeLimitApplied = limit

        self.carApiLastChargeLimitApplyTime = now

        needSleep = False
        for vehicle in self.carApiVehicles:
            if vehicle.stopTryingToApplyLimit or not vehicle.ready():
                continue

            located = vehicle.update_location()
            (wasAtHome, outside, lastApplied) = self.master.getNormalChargeLimit(
                vehicle.ID
            )
            forgetVehicle = False
            if not vehicle.update_charge():
                # We failed to read the "normal" limit; don't risk changing it.
                continue

            if not wasAtHome and located and vehicle.atHome:
                self.master.debugLog(2, "TeslaAPI", vehicle.name + " has arrived")
                outside = vehicle.chargeLimit
            elif wasAtHome and located and not vehicle.atHome:
                self.master.debugLog(2, "TeslaAPI", vehicle.name + " has departed")
                forgetVehicle = True

            if limit == -1 or (located and not vehicle.atHome):
                # We're removing any applied limit, provided it hasn't been manually changed
                #
                # If lastApplied == -1, the manual-change path is always selected.
                if wasAtHome and vehicle.chargeLimit == lastApplied:
                    if vehicle.apply_charge_limit(outside):
                        self.master.debugLog(
                            2,
                            "TeslaAPI",
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
                        self.master.debugLog(
                            2,
                            "TeslaAPI",
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

    def getCarApiErrorRetryMins(self):
        return self.carApiErrorRetryMins

    def getCarApiLastErrorTime(self):
        return self.carApiLastErrorTime

    def getCarApiRefreshToken(self):
        return self.carApiRefreshToken

    def getCarApiRetryRemaining(self, vehicleLast=0):
        # Calculate the amount of time remaining until the API can be queried
        # again. This is the api backoff time minus the difference between now
        # and the last error time

        # The optional vehicleLast parameter allows passing the last error time
        # for an individual vehicle, rather than the entire API.
        lastError = self.getCarApiLastErrorTime()
        if vehicleLast > 0:
            lastError = vehicleLast

        if lastError == 0:
            return 0
        else:
            backoff = self.getCarApiErrorRetryMins() * 60
            lasterrortime = time.time() - lastError
            if lasterrortime >= backoff:
                return 0
            else:
                self.master.debugLog(
                    11,
                    "TeslaAPI",
                    "Backoff is "
                    + str(backoff)
                    + ", lasterror delta is "
                    + str(lasterrortime)
                    + ", last error was "
                    + str(lastError),
                )
                return int(backoff - lasterrortime)

    def getCarApiTransientErrors(self):
        return self.carApiTransientErrors

    def getCarApiTokenExpireTime(self):
        return self.carApiTokenExpireTime

    def getLastStartOrStopChargeTime(self):
        return int(self.carApiLastStartOrStopChargeTime)

    def getVehicleCount(self):
        # Returns the number of currently tracked vehicles
        return int(len(self.carApiVehicles))

    def getCarApiVehicles(self):
        return self.carApiVehicles

    def setCarApiBearerToken(self, token=None):
        if token:
            self.carApiBearerToken = token
            return True
        else:
            return False

    def setCarApiErrorRetryMins(self, mins):
        self.carApiErrorRetryMins = mins
        return True

    def setCarApiLastErrorTime(self, tstamp):
        self.carApiLastErrorTime = tstamp
        return True

    def setCarApiRefreshToken(self, token):
        self.carApiRefreshToken = token
        return True

    def setCarApiTokenExpireTime(self, value):
        self.carApiTokenExpireTime = value
        return True

    def updateCarApiLastErrorTime(self):
        timestamp = time.time()
        self.master.debugLog(
            8,
            "TeslaAPI",
            "updateCarApiLastErrorTime() called due to Tesla API Error. Updating timestamp from "
            + str(self.carApiLastErrorTime)
            + " to "
            + str(timestamp),
        )
        self.carApiLastErrorTime = timestamp
        return True

    def updateLastStartOrStopChargeTime(self):
        self.carApiLastStartOrStopChargeTime = time.time()
        return True

    def updateChargeAtHome(self):
        for car in self.carApiVehicles:
            if car.atHome:
                car.update_charge()
        self.lastChargeCheck = time.time()

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


class CarApiVehicle:

    import time
    import requests
    import json

    carapi = None
    config = None
    debuglevel = 0
    ID = None
    name = ""
    VIN = ""

    firstWakeAttemptTime = 0
    lastAPIAccessTime = 0
    delayNextWakeAttempt = 0
    lastLimitAttemptTime = 0

    lastErrorTime = 0
    lastDriveStatusTime = 0
    lastChargeStatusTime = 0
    stopAskingToStartCharging = False
    stopTryingToApplyLimit = False

    batteryLevel = 10000
    chargeLimit = -1
    lat = 10000
    lon = 10000
    atHome = False
    timeToFullCharge = 0.0

    def __init__(self, json, carapi, config):
        self.carapi = carapi
        self.config = config
        self.debugLevel = config["config"]["debugLevel"]
        self.ID = json["id"]
        self.VIN = json["vin"]
        self.name = json["display_name"]

    def ready(self):
        if self.carapi.getCarApiRetryRemaining(self.lastErrorTime):
            # It's been under carApiErrorRetryMins minutes since the car API
            # generated an error on this vehicle. Return that car is not ready.
            self.carapi.master.debugLog(
                8,
                "TeslaAPI",
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

        self.carapi.master.debugLog(
            8,
            "TeslaVehic",
            self.name + " not ready because it wasn't woken in the last 2 minutes.",
        )
        return False

    # Permits opportunistic API requests
    def is_awake(self):
        url = "https://owner-api.teslamotors.com/api/1/vehicles/" + str(self.ID)
        (result, response) = self.get_car_api(url, checkReady=False, provesOnline=False)
        return result and response.get("state", "") == "online"

    def get_car_api(self, url, checkReady=True, provesOnline=True):
        if checkReady and not self.ready():
            return (False, None)

        apiResponseDict = {}

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + self.carapi.getCarApiBearerToken(),
        }

        # Retry up to 3 times on certain errors.
        for _ in range(0, 3):
            try:
                req = requests.get(url, headers=headers)
                self.carapi.master.debugLog(
                    8, "TeslaVehic", "Car API cmd " + url + " " + str(req)
                )
                apiResponseDict = self.json.loads(req.text)
                # This error can happen here as well:
                #   {'response': {'reason': 'could_not_wake_buses', 'result': False}}
                # This one is somewhat common:
                #   {'response': None, 'error': 'vehicle unavailable: {:error=>"vehicle unavailable:"}', 'error_description': ''}
            except:
                pass

            try:
                self.carapi.master.debugLog(
                    10, "TeslaVehic", "Car API vehicle status" + str(apiResponseDict)
                )

                if "error" in apiResponseDict:
                    foundKnownError = False
                    error = apiResponseDict["error"]

                    for knownError in self.carapi.getCarApiTransientErrors():
                        if knownError == error[0 : len(knownError)]:
                            # I see these errors often enough that I think
                            # it's worth re-trying in 1 minute rather than
                            # waiting carApiErrorRetryMins minutes for retry
                            # in the standard error handler.
                            self.carapi.master.debugLog(
                                1,
                                "TeslaVehic",
                                "Car API returned '"
                                + error
                                + "' when trying to get status.  Try again in 1 minute.",
                            )
                            time.sleep(60)
                            foundKnownError = True
                            break
                    if foundKnownError:
                        continue

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
                self.carapi.master.debugLog(
                    1,
                    "TeslaVehic",
                    "ERROR: Can't access vehicle status for "
                    + self.name
                    + ".  Will try again later.",
                )
                self.lastErrorTime = time.time()
                return (False, None)

            if provesOnline:
                self.lastAPIAccessTime = time.time()

            return (True, response)

    def update_location(self, cacheTime=60):

        url = "https://owner-api.teslamotors.com/api/1/vehicles/"
        url = url + str(self.ID) + "/data_request/drive_state"

        now = time.time()

        if now - self.lastDriveStatusTime < cacheTime:
            return True

        (result, response) = self.get_car_api(url)

        if result:
            self.lastDriveStatusTime = now
            self.lat = response["latitude"]
            self.lon = response["longitude"]
            self.atHome = self.carapi.is_location_home(self.lat, self.lon)

        return result

    def update_charge(self):
        url = "https://owner-api.teslamotors.com/api/1/vehicles/"
        url = url + str(self.ID) + "/data_request/charge_state"

        now = time.time()

        if now - self.lastChargeStatusTime < 60:
            return True

        (result, response) = self.get_car_api(url)

        if result:
            self.lastChargeStatusTime = time.time()
            self.chargeLimit = response["charge_limit_soc"]
            self.batteryLevel = response["battery_level"]
            self.timeToFullCharge = response["time_to_full_charge"]

        return result

    def apply_charge_limit(self, limit):
        if self.stopTryingToApplyLimit:
            return True

        now = time.time()

        if (
            now - self.lastLimitAttemptTime <= 300
            or self.carapi.getCarApiRetryRemaining(self.lastErrorTime)
        ):
            return False

        if self.ready() == False:
            return False

        self.lastLimitAttemptTime = now

        url = "https://owner-api.teslamotors.com/api/1/vehicles/"
        url = url + str(self.ID) + "/command/set_charge_limit"

        headers = {
            "accept": "application/json",
            "Authorization": "Bearer " + self.carapi.getCarApiBearerToken(),
        }
        body = {"percent": limit}

        for _ in range(0, 3):
            try:
                req = requests.post(url, headers=headers, json=body)
                self.carapi.master.debugLog(
                    8, "TeslaVehic", "Car API cmd set_charge_limit " + str(req)
                )

                apiResponseDict = self.json.loads(req.text)
            except:
                pass

            result = False
            reason = ""
            try:
                result = apiResponseDict["response"]["result"]
                reason = apiResponseDict["response"]["reason"]
            except (KeyError, TypeError):
                # This catches unexpected cases like trying to access
                # apiResponseDict['response'] when 'response' doesn't exist
                # in apiResponseDict.
                result = False

            if result == True or reason == "already_set":
                self.stopTryingToApplyLimit = True
                self.lastAPIAccessTime = now
                return True
            elif reason == "could_not_wake_buses":
                time.sleep(5)
                continue
            elif apiResponseDict["response"] == None:
                if "error" in apiResponseDict:
                    foundKnownError = False
                    error = apiResponseDict["error"]
                    for knownError in self.carapi.getCarApiTransientErrors():
                        if knownError == error[0 : len(knownError)]:
                            # I see these errors often enough that I think
                            # it's worth re-trying in 1 minute rather than
                            # waiting carApiErrorRetryMins minutes for retry
                            # in the standard error handler.
                            self.carapi.master.debugLog(
                                1,
                                "TeslaVehic",
                                "Car API returned '"
                                + error
                                + "' when trying to set charge limit.  Try again in 1 minute.",
                            )
                            time.sleep(60)
                            foundKnownError = True
                            break
                    if foundKnownError:
                        continue
            else:
                self.lastErrorTime = now

        return False
