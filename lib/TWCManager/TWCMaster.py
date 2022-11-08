#! /usr/bin/python3

from datetime import datetime, timedelta
import json
import logging
import os.path
import queue
from sys import modules
import threading
import time
import math
import requests
import bisect

logger = logging.getLogger("\u26FD Master")


class TWCMaster:

    allowed_flex = 0
    backgroundTasksQueue = queue.Queue()
    backgroundTasksCmds = {}
    backgroundTasksLock = threading.Lock()
    backgroundTasksDelayed = []
    config = None
    consumptionValues = {}
    debugOutputToFile = False
    generationValues = {}
    lastSaveFailed = 0
    lastTWCResponseMsg = None
    lastUpdateCheck = 0
    maxAmpsToDivideAmongSlaves = 0
    modules = {}
    nextHistorySnap = 0
    protocolVersion = 2
    releasedModules = []
    settings = {
        "chargeNowAmps": 0,
        "chargeStopMode": "1",
        "chargeNowTimeEnd": 0,
        "homeLat": 10000,
        "homeLon": 10000,
        "hourResumeTrackGreenEnergy": -1,
        "kWhDelivered": 119,
        "nonScheduledAmpsMax": 0,
        "respondToSlaves": 1,
        "scheduledAmpsDaysBitmap": 0x7F,
        "scheduledAmpsEndHour": -1,
        "scheduledAmpsMax": 0,
        "scheduledAmpsStartHour": -1,
        "sendServerTime": 0,
    }
    stopTimeout = datetime.max
    spikeAmpsToCancel6ALimit = 16
    subtractChargerLoad = False
    teslaLoginAskLater = False
    updateVersion = False
    version = "1.3.0"

    def __init__(self, TWCID, config):
        self.config = config
        self.debugOutputToFile = config["config"].get("debugOutputToFile", False)
        self.TWCID = TWCID
        self.subtractChargerLoad = config["config"]["subtractChargerLoad"]
        self.advanceHistorySnap()

        # Register ourself as a module, allows lookups via the Module architecture
        self.registerModule({"name": "master", "ref": self, "type": "Master"})

    def addkWhDelivered(self, kWh):
        self.settings["kWhDelivered"] = self.settings.get("kWhDelivered", 0) + kWh

    def advanceHistorySnap(self):
        try:
            futureSnap = datetime.now().astimezone() + timedelta(minutes=5)
            self.nextHistorySnap = futureSnap.replace(
                minute=math.floor(futureSnap.minute / 5) * 5, second=0, microsecond=0
            )
        except ValueError as e:
            logger.debug("Exception in advanceHistorySnap: " + str(e))

    def cancelStopCarsCharging(self):
        self.delete_background_task({"cmd": "charge", "charge": False})

    def checkForUpdates(self):
        # This function is used by the Web UI and later on will be used by the console to detect TWCManager Updates
        # It runs a maximum of once per hour, and queries the current PyPi package version vs the current version
        # If they match, it returns false. If there's a different version available, we alert the user
        if time.time() > self.lastUpdateCheck + (60 * 60):
            self.lastUpdateCheck = time.time()

            # Fetch the JSON data from PyPi for our package
            url = "https://pypi.org/pypi/twcmanager/json"

            try:
                req = requests.get(url)
                logger.log(logging.INFO8, "Requesting PyPi package info " + str(req))
                pkgInfo = json.loads(req.text)
            except requests.exceptions.RequestException:
                logger.info("Failed to fetch package details " + url)
                logger.log(logging.INFO6, "Response: " + req.text)
                pass
            except json.decoder.JSONDecodeError:
                logger.info("Could not parse JSON result from " + url)
                logger.log(logging.INFO6, "Response: " + req.text)
                pass

            if pkgInfo.get("info", {}).get("version", None):
                if pkgInfo["info"]["version"] != self.version:
                    # Versions don't match. Let's make sure the new one really is newer
                    current_arr = [int(v) for v in self.version.split(".")]
                    avail_arr = [int(v) for v in pkgInfo["info"]["version"].split(".")]
                    for i in range(max(len(current_arr), len(avail_arr))):
                        v1 = current_arr[i] if i < len(current_arr) else 0
                        v2 = avail_arr[i] if i < len(avail_arr) else 0

                        # If any element of current version in order from first to last is lower than available version,
                        # advertise newer version
                        if v1 < v2:
                            self.updateVersion = pkgInfo["info"]["version"]
                            break

                        # If current version is greater than available version, do not advertise newer version
                        if v1 > v2:
                            break

        return self.updateVersion

    def checkModuleCapability(self, type, capability):
        # For modules which advertise capabilities, scan all loaded modules of a certain type and
        # report on if any of those modules advertise the reported capability
        match = False

        for module in self.getModulesByType(type):
            if module["ref"].getCapabilities(capability):
                match = True

        return match

    def checkScheduledCharging(self):

        # Check if we're within the hours we must use scheduledAmpsMax instead
        # of nonScheduledAmpsMax
        blnUseScheduledAmps = 0
        ltNow = time.localtime()
        hourNow = ltNow.tm_hour + (ltNow.tm_min / 60)
        timeSettings = self.getScheduledAmpsTimeFlex()
        startHour = timeSettings[0]
        endHour = timeSettings[1]
        daysBitmap = timeSettings[2]

        if (
            self.getScheduledAmpsMax() > 0
            and startHour > -1
            and endHour > -1
            and daysBitmap > 0
        ):
            if startHour > endHour:
                # We have a time like 8am to 7am which we must interpret as the
                # 23-hour period after 8am or before 7am. Since this case always
                # crosses midnight, we only ensure that scheduledAmpsDaysBitmap
                # is set for the day the period starts on. For example, if
                # scheduledAmpsDaysBitmap says only schedule on Monday, 8am to
                # 7am, we apply scheduledAmpsMax from Monday at 8am to Monday at
                # 11:59pm, and on Tuesday at 12am to Tuesday at 6:59am.
                yesterday = ltNow.tm_wday - 1
                if yesterday < 0:
                    yesterday += 7
                if (hourNow >= startHour and (daysBitmap & (1 << ltNow.tm_wday))) or (
                    hourNow < endHour and (daysBitmap & (1 << yesterday))
                ):
                    blnUseScheduledAmps = 1
            else:
                # We have a time like 7am to 8am which we must interpret as the
                # 1-hour period between 7am and 8am.
                hourNow = ltNow.tm_hour + (ltNow.tm_min / 60)
                if (
                    hourNow >= startHour
                    and hourNow < endHour
                    and (daysBitmap & (1 << ltNow.tm_wday))
                ):
                    blnUseScheduledAmps = 1
        return blnUseScheduledAmps

    def checkVINEntitlement(self, vin):
        # When provided with the VIN reported for a vehicle,
        # we check the policy for charging and determine if it is allowed or not

        if not vin:
            # No VIN supplied. We can't make any decision other than allow
            return 1

        if str(self.settings.get("chargeAuthorizationMode", "1")) == "1":
            # In this mode, we allow all vehicles to charge unless they
            # are explicitly banned from charging
            if (
                vin
                in self.settings["VehicleGroups"]["Deny Charging"]["Members"]
            ):
                return 0
            else:
                return 1

        elif str(self.settings.get("chargeAuthorizationMode", "1")) == "2":
            # In this mode, vehicles may only charge if they are listed
            # in the Allowed VINs list
            if (
                vin
                in self.settings["VehicleGroups"]["Allow Charging"]["Members"]
            ):
                return 1
            else:
                return 0

    def convertAmpsToWatts(self, amps):
        (voltage, phases) = self.getVoltageMeasurement()
        return phases * voltage * amps

    def convertWattsToAmps(self, watts):
        (voltage, phases) = self.getVoltageMeasurement()
        return watts / (phases * voltage)

    def delete_background_task(self, task):
        if (
            task["cmd"] in self.backgroundTasksCmds
            and self.backgroundTasksCmds[task["cmd"]] == task
        ):
            del self.backgroundTasksCmds[task["cmd"]]["cmd"]
            del self.backgroundTasksCmds[task["cmd"]]

    def doneBackgroundTask(self, task):

        # Delete task['cmd'] from backgroundTasksCmds such that
        # queue_background_task() can queue another task['cmd'] in the future.
        if "cmd" in task:
            del self.backgroundTasksCmds[task["cmd"]]

        # task_done() must be called to let the queue know the task is finished.
        # backgroundTasksQueue.join() can then be used to block until all tasks
        # in the queue are done.
        self.backgroundTasksQueue.task_done()

    def getAllowedFlex(self):
        return self.allowedFlex

    def getBackgroundTask(self):
        result = None

        while result is None:
            # Insert any delayed tasks
            while (
                self.backgroundTasksDelayed
                and self.backgroundTasksDelayed[0][0] <= datetime.now()
            ):
                self.queue_background_task(self.backgroundTasksDelayed.pop(0)[1])

            # Get the next task
            try:
                result = self.backgroundTasksQueue.get(timeout=30)
            except queue.Empty:
                continue

        return result

    def getBackgroundTasksLock(self):
        self.backgroundTasksLock.acquire()

    def getChargeNowAmps(self):
        # Returns the currently configured Charge Now Amps setting
        chargenow = int(self.settings.get("chargeNowAmps", 0))
        if chargenow > 0:
            return chargenow
        else:
            return 0

    def getConsumptionOffset(self):
        # Start by reading the offset value from config, if it exists
        # This is a legacy value but it doesn't hurt to keep it
        offset = self.convertAmpsToWatts(
            self.config["config"].get("greenEnergyAmpsOffset", 0)
        )

        # Iterate through the offsets listed in settings
        for offsetName in self.settings.get("consumptionOffset", {}).keys():
            if self.settings["consumptionOffset"][offsetName]["unit"] == "W":
                offset += self.settings["consumptionOffset"][offsetName]["value"]
            else:
                offset += self.convertAmpsToWatts(
                    self.settings["consumptionOffset"][offsetName]["value"]
                )
        return offset

    def getEVSEbyID(self, id):
        for evse in self.getAllEVSEs():
            if evse.id == id:
                return evse
        return None

    def getHourResumeTrackGreenEnergy(self):
        return self.settings.get("hourResumeTrackGreenEnergy", -1)

    def getkWhDelivered(self):
        return self.settings["kWhDelivered"]

    def getMaxAmpsToDivideAmongSlaves(self):
        if self.maxAmpsToDivideAmongSlaves > 0:
            return self.maxAmpsToDivideAmongSlaves
        else:
            return 0

    def getModuleByName(self, name):
        module = self.modules.get(name, None)
        if module:
            return module["ref"]
        else:
            return None

    def getModulesByType(self, type):
        matched = []
        for module in self.modules:
            modinfo = self.modules[module]
            if modinfo["type"] == type:
                matched.append({"name": module, "ref": modinfo["ref"]})
        return matched

    def getScheduledAmpsDaysBitmap(self):
        return self.settings.get("scheduledAmpsDaysBitmap", 0x7F)

    def getScheduledAmpsBatterySize(self):
        return self.settings.get("scheduledAmpsBatterySize", 100)

    def getNonScheduledAmpsMax(self):
        nschedamps = int(self.settings.get("nonScheduledAmpsMax", 0))
        if nschedamps > 0:
            return nschedamps
        else:
            return 0

    def getSendServerTime(self):
        sendservertime = int(self.settings.get("sendServerTime", 0))
        if sendservertime > 0:
            return 1
        else:
            return 0

    def getScheduledAmpsMax(self):
        schedamps = int(self.settings.get("scheduledAmpsMax", 0))
        if schedamps > 0:
            return schedamps
        else:
            return 0

    def getScheduledAmpsStartHour(self):
        return int(self.settings.get("scheduledAmpsStartHour", -1))

    def getScheduledAmpsTimeFlex(self):
        startHour = self.getScheduledAmpsStartHour()
        days = self.getScheduledAmpsDaysBitmap()
        if (
            startHour >= 0
            and self.getScheduledAmpsFlexStart()
            and self.countSlaveTWC() == 1
        ):
            # Try to charge at the end of the scheduled time
            slave = next(iter(self.slaveTWCs.values()))
            vehicle = slave.getLastVehicle()
            if vehicle != None:
                amps = self.getScheduledAmpsMax()
                watts = self.convertAmpsToWatts(amps) * self.getRealPowerFactor(amps)
                hoursForFullCharge = self.getScheduledAmpsBatterySize() / (watts / 1000)
                realChargeFactor = (vehicle.chargeLimit - vehicle.batteryLevel) / 100
                # calculating startHour with a max Battery size - so it starts charging and then it has the time
                startHour = round(
                    self.getScheduledAmpsEndHour()
                    - (hoursForFullCharge * realChargeFactor),
                    2,
                )
                # Always starting a quarter of a hour earlier
                startHour -= 0.25
                # adding half an hour if battery should be charged over 98%
                if vehicle.chargeLimit >= 98:
                    startHour -= 0.5
                if startHour < 0:
                    startHour = startHour + 24
                # if startHour is smaller than the intial startHour, then it should begin beginn charging a day later
                # (if starting usually at 9pm and it calculates to start at 4am - it's already the next day)
                if startHour < self.getScheduledAmpsDaysBitmap():
                    days = self.rotl(days, 7)
        return (startHour, self.getScheduledAmpsEndHour(), days)

    def getScheduledAmpsEndHour(self):
        return self.settings.get("scheduledAmpsEndHour", -1)

    def getScheduledAmpsFlexStart(self):
        return int(self.settings.get("scheduledAmpsFlexStart", False))

    def getStatus(self):
        chargerLoad = float(self.getChargerLoad())
        data = {
            "carsCharging": self.num_cars_charging_now(),
            "chargerLoadWatts": "%.2f" % chargerLoad,
            "chargerLoadAmps": ("%.2f" % self.convertWattsToAmps(chargerLoad),),
            "currentPolicy": str(self.getModuleByName("Policy").active_policy),
            "maxAmpsToDivideAmongSlaves": "%.2f"
            % float(self.getMaxAmpsToDivideAmongSlaves()),
        }
        if self.settings.get("sendServerTime", "0") == 1:
            data["currentServerTime"] = datetime.now().strftime(
                "%Y-%m-%d, %H:%M&nbsp;|&nbsp;"
            )
        consumption = float(self.getConsumption())
        if consumption:
            data["consumptionAmps"] = ("%.2f" % self.convertWattsToAmps(consumption),)
            data["consumptionWatts"] = "%.2f" % consumption
        else:
            data["consumptionAmps"] = "%.2f" % 0
            data["consumptionWatts"] = "%.2f" % 0
        generation = float(self.getGeneration())
        if generation:
            data["generationAmps"] = ("%.2f" % self.convertWattsToAmps(generation),)
            data["generationWatts"] = "%.2f" % generation
        else:
            data["generationAmps"] = "%.2f" % 0
            data["generationWatts"] = "%.2f" % 0
        if self.getModuleByName("Policy").policyIsGreen():
            data["isGreenPolicy"] = "Yes"
        else:
            data["isGreenPolicy"] = "No"

        data["scheduledChargingStartHour"] = self.getScheduledAmpsStartHour()
        data["scheduledChargingFlexStart"] = self.getScheduledAmpsTimeFlex()[0]
        data["scheduledChargingEndHour"] = self.getScheduledAmpsEndHour()
        scheduledChargingDays = self.getScheduledAmpsDaysBitmap()
        scheduledFlexTime = self.getScheduledAmpsTimeFlex()

        data["ScheduledCharging"] = {
            "enabled": data["scheduledChargingStartHour"] >= 0
            and data["scheduledChargingEndHour"] >= 0
            and scheduledChargingDays > 0
            and self.getScheduledAmpsMax() > 0,
            "amps": self.getScheduledAmpsMax(),
            "startingMinute": int(data["scheduledChargingStartHour"] * 60)
            if data["scheduledChargingStartHour"] >= 0
            else -1,
            "endingMinute": int(data["scheduledChargingEndHour"] * 60)
            if data["scheduledChargingEndHour"] >= 0
            else -1,
            "monday": (scheduledChargingDays & 1) == 1,
            "tuesday": (scheduledChargingDays & 2) == 2,
            "wednesday": (scheduledChargingDays & 4) == 4,
            "thursday": (scheduledChargingDays & 8) == 8,
            "friday": (scheduledChargingDays & 16) == 16,
            "saturday": (scheduledChargingDays & 32) == 32,
            "sunday": (scheduledChargingDays & 64) == 64,
            "flexStartEnabled": self.getScheduledAmpsFlexStart(),
            "flexStartingMinute": int(scheduledFlexTime[0] * 60)
            if scheduledFlexTime[0] >= 0
            else -1,
            "flexEndingMinute": int(scheduledFlexTime[1] * 60)
            if scheduledFlexTime[1] >= 0
            else -1,
            "flexMonday": (scheduledFlexTime[2] & 1) == 1,
            "flexTuesday": (scheduledFlexTime[2] & 2) == 2,
            "flexWednesday": (scheduledFlexTime[2] & 4) == 4,
            "flexThursday": (scheduledFlexTime[2] & 8) == 8,
            "flexFriday": (scheduledFlexTime[2] & 16) == 16,
            "flexSaturday": (scheduledFlexTime[2] & 32) == 32,
            "flexSunday": (scheduledFlexTime[2] & 64) == 64,
            "flexBatterySize": self.getScheduledAmpsBatterySize(),
        }
        return data

    def getSpikeAmps(self):
        return self.spikeAmpsToCancel6ALimit

    def getTWCbyVIN(self, vin):
        twc = None
        for slaveTWC in self.getAllEVSEs():
            if slaveTWC.currentVIN == vin:
                twc = slaveTWC
        return twc

    def getChargerLoad(self):
        # Calculate in watts the load that the charger is generating so
        # that we can exclude it from the consumption if necessary
        amps = self.getTotalAmpsInUse()
        return self.convertAmpsToWatts(amps) * self.getRealPowerFactor(amps)

    def getConsumption(self):
        consumptionVal = 0

        for key in self.consumptionValues:
            consumptionVal += float(self.consumptionValues[key])

        if consumptionVal < 0:
            consumptionVal = 0

        offset = self.getConsumptionOffset()
        if offset > 0:
            consumptionVal += offset

        return float(consumptionVal)

    def getGeneration(self):
        generationVal = 0

        # Currently, our only logic is to add all of the values together
        for key in self.generationValues:
            generationVal += float(self.generationValues[key])

        if generationVal < 0:
            generationVal = 0

        offset = self.getConsumptionOffset()
        if offset < 0:
            generationVal += -1 * offset

        return float(generationVal)

    def getGenerationOffset(self):
        # Returns the number of watts to subtract from the solar generation stats
        # This is consumption + charger load if subtractChargerLoad is enabled
        # Or simply consumption if subtractChargerLoad is disabled
        generationOffset = self.getConsumption()
        if self.subtractChargerLoad:
            generationOffset -= self.getChargerLoad()
        if generationOffset < 0:
            generationOffset = 0
        return float(generationOffset)

    def getHomeLatLon(self):
        # Returns Lat/Lon coordinates to check if car location is
        # at home
        latlon = [10000, 10000]
        latlon[0] = float(self.settings.get("homeLat", 10000))
        latlon[1] = float(self.settings.get("homeLon", 10000))
        return latlon

    def getMaxAmpsToDivideGreenEnergy(self):
        # Calculate our current generation and consumption in watts
        generationW = float(self.getGeneration())
        consumptionW = float(self.getConsumption())

        # Calculate what we should offer to align with green energy
        #
        # The current offered shouldn't increase more than / must
        # decrease at least the current gap between generation and
        # consumption.

        currentOffer = max(
            int(self.getMaxAmpsToDivideAmongSlaves()),
            self.num_cars_charging_now() * self.config["config"]["minAmpsPerTWC"],
        )
        newOffer = currentOffer + self.convertWattsToAmps(generationW - consumptionW)

        # This is the *de novo* calculation of how much we can offer
        #
        # Fetches and uses consumptionW separately
        generationOffset = self.getGenerationOffset()
        solarW = float(generationW - generationOffset)
        solarAmps = self.convertWattsToAmps(solarW)

        # Offer the smaller of the two, but not less than zero.
        amps = max(min(newOffer, solarAmps / self.getRealPowerFactor(solarAmps)), 0)
        return round(amps, 2)

    def getNormalChargeLimit(self, ID):
        if "chargeLimits" in self.settings and str(ID) in self.settings["chargeLimits"]:
            result = self.settings["chargeLimits"][str(ID)]
            if type(result) is int:
                result = (result, 0)
            if result[0] is None:
                result[0] = 0
            if result[1] is None:
                result[1] = 0
            return (True, result[0], result[1])
        return (False, None, None)

    def getTotalAmpsInUse(self):
        # Returns the number of amps currently in use by all TWCs
        totalAmps = 0
        for evse in self.getAllEVSEs():
            totalAmps += evse.currentAmps

        logger.debug("Total amps all slaves are using: " + str(totalAmps))
        return totalAmps

    def getVoltageMeasurement(self):
        evsesWithVoltage = []
        for evse in self.getAllEVSEs():
            voltage = evse.currentVoltage
            if voltage[0] > 0 or voltage[1] > 0 or voltage[2] > 0:
                evsesWithVoltage.append(evse)

        if len(evsesWithVoltage) == 0:
            # No EVSE instances support returning voltage
            return (
                self.config["config"].get("defaultVoltage", 240),
                self.config["config"].get("numberOfPhases", 1),
            )

        total = 0
        phases = 0

        # Detect number of active phases
        for evse in evsesWithVoltage:
            localPhases = 0
            voltage = evse.currentVoltage
            for phase in voltage:
                if phase:
                    localPhases += 1

            if phases:
                if localPhases != phases:
                    logger.info(
                        "FATAL:  Mix of multi-phase TWC configurations not currently supported."
                    )
                    return (
                        self.config["config"].get("defaultVoltage", 240),
                        self.config["config"].get("numberOfPhases", 1),
                    )
            else:
                phases = localPhases

        total = sum(
            [
                (slave.voltsPhaseA + slave.voltsPhaseB + slave.voltsPhaseC)
                for slave in evsesWithVoltage
            ]
        )

        return (total / (phases * len(evsesWithVoltage)), phases)

    def hex_str(self, s: str):
        return " ".join("{:02X}".format(ord(c)) for c in s)

    def hex_str(self, ba: bytearray):
        return " ".join("{:02X}".format(c) for c in ba)

    def loadSettings(self):
        # Loads the volatile application settings (such as charger timings,
        # API credentials, etc) from a JSON file

        # Step 1 - Load settings from JSON file
        if not os.path.exists(self.config["config"]["settingsPath"] + "/settings.json"):
            self.settings = {}
            return

        with open(
            self.config["config"]["settingsPath"] + "/settings.json", "r"
        ) as inconfig:
            try:
                self.settings = json.load(inconfig)
            except Exception as e:
                logger.info(
                    "There was an exception whilst loading settings file "
                    + self.config["config"]["settingsPath"]
                    + "/settings.json"
                )
                logger.info(
                    "Some data may have been loaded. This may be because the file is being created for the first time."
                )
                logger.info(
                    "It may also be because you are upgrading from a TWCManager version prior to v1.1.4, which used the old settings file format."
                )
                logger.info(
                    "If this is the case, you may need to locate the old config file and migrate some settings manually."
                )
                logger.log(logging.DEBUG2, str(e))

        # Step 2 - Send settings to other modules
        carapi = self.getModuleByName("TeslaAPI")
        carapi.setCarApiBearerToken(self.settings.get("carApiBearerToken", ""))
        carapi.setCarApiRefreshToken(self.settings.get("carApiRefreshToken", ""))
        carapi.setCarApiTokenExpireTime(self.settings.get("carApiTokenExpireTime", ""))

        # If particular details are missing from the Settings dict, create them
        if not self.settings.get("VehicleGroups", None):
            self.settings["VehicleGroups"] = {}
        if not self.settings["VehicleGroups"].get("Allow Charging", None):
            self.settings["VehicleGroups"]["Allow Charging"] = {
                "Description": "Built-in Group - Vehicles in this Group can charge on managed TWCs",
                "Built-in": 1,
                "Members": [],
            }
        if not self.settings["VehicleGroups"].get("Deny Charging", None):
            self.settings["VehicleGroups"]["Deny Charging"] = {
                "Description": "Built-in Group - Vehicles in this Group cannot charge on managed TWCs",
                "Built-in": 1,
                "Members": [],
            }
        # Fill in old defaults as bridge
        if not self.settings.get("sunrise", None):
            self.settings["sunrise"] = 6
        if not self.settings.get("sunset", None):
            self.settings["sunset"] = 20

    def num_cars_charging_now(self):

        carsCharging = 0
        for evse in self.getAllEVSEs():
            if evse.isCharging:
                carsCharging += 1
            for module in self.getModulesByType("Status"):
                module["ref"].setStatus(
                    evse.ID,
                    "cars_charging",
                    "carsCharging",
                    evse.isCharging,
                    "",
                )
        logger.debug("Number of cars charging now: " + str(carsCharging))

        if carsCharging == 0:
            self.stopTimeout = datetime.max

        return carsCharging

    def queue_background_task(self, task, delay=0):

        if delay > 0:
            bisect.insort(
                self.backgroundTasksDelayed,
                (datetime.now() + timedelta(seconds=delay), task),
            )
            return

        if task["cmd"] in self.backgroundTasksCmds:
            # Some tasks, like cmd='charge', will be called once per second until
            # a charge starts or we determine the car is done charging.  To avoid
            # wasting memory queing up a bunch of these tasks when we're handling
            # a charge cmd already, don't queue two of the same task.
            self.backgroundTasksCmds[task["cmd"]].update(task)
            return

        # Insert task['cmd'] in backgroundTasksCmds to prevent queuing another
        # task['cmd'] till we've finished handling this one.
        self.backgroundTasksCmds[task["cmd"]] = task

        # Queue the task to be handled by background_tasks_thread.
        self.backgroundTasksQueue.put(task)

    def registerModule(self, module):
        # This function is used during module instantiation to either reference a
        # previously loaded module, or to instantiate a module for the first time
        if not module["ref"] and not module["modulename"]:
            logger.log(
                logging.INFO2,
                "registerModule called for module %s without an existing reference or a module to instantiate.",
                module["name"],
                extra={"colored": "red"},
            )
        elif module["ref"]:
            # If the reference is passed, it means this module has already been
            # instantiated and we should just refer to the existing instance

            # Check this module has not already been instantiated
            if not self.modules.get(module["name"], None):
                if not module["name"] in self.releasedModules:
                    logger.log(
                        logging.INFO7,
                        "Registered module %s",
                        module["name"],
                        extra={"colored": "red"},
                    )
                    self.modules[module["name"]] = {
                        "ref": module["ref"],
                        "type": module["type"],
                    }
            else:
                logger.log(
                    logging.INFO7,
                    "Avoided re-registration of module %s, which has already been loaded",
                    module["name"],
                    extra={"colored": "red"},
                )

    def recordVehicleSessionEnd(self, slaveTWC):
        # This function is called when a vehicle charge session ends.
        # If we have a last vehicle VIN set, close off the charging session
        # for this vehicle and save the settings.
        if not self.settings.get("Vehicles", None):
            self.settings["Vehicles"] = {}
        if self.settings["Vehicles"].get(slaveTWC.lastVIN, None):
            if self.settings["Vehicles"][slaveTWC.lastVIN].get("startkWh", 0) > 0:
                # End current session
                delta = (
                    slaveTWC.lifetimekWh
                    - self.settings["Vehicles"][slaveTWC.lastVIN]["startkWh"]
                )
                self.settings["Vehicles"][slaveTWC.lastVIN]["startkWh"] = 0
                self.settings["Vehicles"][slaveTWC.lastVIN]["totalkWh"] += delta
                self.queue_background_task({"cmd": "saveSettings"})

        # Update Charge Session details in logging modules
        logger.info(
            "Charge Session Stopped for Slave TWC %02X%02X",
            slaveTWC.TWCID[0],
            slaveTWC.TWCID[1],
            extra={
                "logtype": "charge_sessions",
                "chargestate": "stop",
                "TWCID": slaveTWC.TWCID,
                "endkWh": slaveTWC.lifetimekWh,
                "endTime": int(time.time()),
                "endFormat": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def recordVehicleSessionStart(self, slaveTWC):
        # Update Charge Session details in logging modules
        logger.info(
            "Charge Session Started for Slave TWC %02X%02X",
            slaveTWC.TWCID[0],
            slaveTWC.TWCID[1],
            extra={
                "logtype": "charge_sessions",
                "chargestate": "start",
                "TWCID": slaveTWC.TWCID,
                "startkWh": slaveTWC.lifetimekWh,
                "startTime": int(time.time()),
                "startFormat": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def recordVehicleVIN(self, slaveTWC):
        # Record Slave TWC ID as being capable of reporting VINs, if it is not
        # already.
        twcid = "%02X%02X" % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
        if not self.settings.get("SlaveTWCs", None):
            self.settings["SlaveTWCs"] = {}
        if not self.settings["SlaveTWCs"].get(twcid, None):
            self.settings["SlaveTWCs"][twcid] = {}
        if not self.settings["SlaveTWCs"][twcid].get("supportsVINQuery", 0):
            self.settings["SlaveTWCs"][twcid]["supportsVINQuery"] = 1
            self.queue_background_task({"cmd": "saveSettings"})

        # Increment sessions counter for this VIN in persistent settings file
        if not self.settings.get("Vehicles", None):
            self.settings["Vehicles"] = {}
        if not self.settings["Vehicles"].get(slaveTWC.currentVIN, None):
            self.settings["Vehicles"][slaveTWC.currentVIN] = {
                "chargeSessions": 1,
                "startkWh": slaveTWC.lifetimekWh,
                "totalkWh": 0,
            }
        else:
            self.settings["Vehicles"][slaveTWC.currentVIN]["chargeSessions"] += 1
            self.settings["Vehicles"][slaveTWC.currentVIN][
                "startkWh"
            ] = slaveTWC.lifetimekWh
            if not self.settings["Vehicles"][slaveTWC.currentVIN].get("totalkWh", None):
                self.settings["Vehicles"][slaveTWC.currentVIN]["totalkWh"] = 0
        self.queue_background_task({"cmd": "saveSettings"})

        # Update Charge Session details in logging modules
        logger.info(
            "Charge Session updated for Slave TWC %02X%02X",
            slaveTWC.TWCID[0],
            slaveTWC.TWCID[1],
            extra={
                "logtype": "charge_sessions",
                "chargestate": "update",
                "TWCID": slaveTWC.TWCID,
                "vehicleVIN": slaveTWC.currentVIN,
            },
        )

    def releaseBackgroundTasksLock(self):
        self.backgroundTasksLock.release()

    def releaseModule(self, path, module):
        # Removes a module from the modules dict
        # This ensures we do not continue to call the module if it is
        # inoperable
        self.releasedModules.append(module)
        if self.modules.get(module, None):
            del self.modules[module]

        fullname = path + "." + module
        if modules.get(fullname, None):
            del modules[fullname]

        logger.log(
            logging.INFO7, "Released module %s", module, extra={"colored": "red"}
        )

    def removeNormalChargeLimit(self, ID):
        if "chargeLimits" in self.settings and str(ID) in self.settings["chargeLimits"]:
            del self.settings["chargeLimits"][str(ID)]
            self.queue_background_task({"cmd": "saveSettings"})

    def resetChargeNowAmps(self):
        # Sets chargeNowAmps back to zero, so we follow the green energy
        # tracking again
        self.settings["chargeNowAmps"] = 0
        self.settings["chargeNowTimeEnd"] = 0
        self.queue_background_task({"cmd": "saveSettings"})

    def saveNormalChargeLimit(self, ID, outsideLimit, lastApplied):
        if not "chargeLimits" in self.settings:
            self.settings["chargeLimits"] = dict()

        self.settings["chargeLimits"][str(ID)] = (outsideLimit, lastApplied)
        self.queue_background_task({"cmd": "saveSettings"})

    def saveSettings(self):
        # Saves the volatile application settings (such as charger timings,
        # API credentials, etc) to a JSON file
        fileName = self.config["config"]["settingsPath"] + "/settings.json"

        # Step 1 - Merge any config from other modules
        carapi = self.getModuleByName("TeslaAPI")
        self.settings["carApiBearerToken"] = carapi.getCarApiBearerToken()
        self.settings["carApiRefreshToken"] = carapi.getCarApiRefreshToken()
        self.settings["carApiTokenExpireTime"] = carapi.getCarApiTokenExpireTime()

        # Step 2 - Write the settings dict to a JSON file
        try:
            with open(fileName, "w") as outconfig:
                json.dump(self.settings, outconfig)
            self.lastSaveFailed = 0
        except PermissionError as e:
            logger.info(
                "Permission Denied trying to save to settings.json. Please check the permissions of the file and try again."
            )
            self.lastSaveFailed = 1
        except TypeError as e:
            logger.info("Exception raised while attempting to save settings file:")
            logger.info(str(e))
            self.lastSaveFailed = 1

    def sendStopCommand(self, vin):
        evses = self.getAllEVSEs()
        if vin:
            evses = [evse for evse in evses if evse.currentVIN == vin]
        
        for evse in evses:
            evse.stopCharging()

    def sendStartCommand(self):
        for evse in self.getAllEVSEs():
            evse.startCharging()

    def setAllowedFlex(self, amps):
        self.allowedFlex = amps if amps >= 0 else 0

    def setChargeNowAmps(self, amps):
        # Accepts a number of amps to define the amperage at which we
        # should charge
        if amps > self.config["config"]["wiringMaxAmpsAllTWCs"]:
            logger.info(
                "setChargeNowAmps failed because specified amps are above wiringMaxAmpsAllTWCs"
            )
        elif amps < 0:
            logger.info("setChargeNowAmps failed as specified amps is less than 0")
        else:
            self.settings["chargeNowAmps"] = amps

    def setChargeNowTimeEnd(self, timeadd):
        self.settings["chargeNowTimeEnd"] = time.time() + timeadd

    def setConsumption(self, source, value):
        # Accepts consumption values from one or more data sources
        # For now, this gives a sum value of all, but in future we could
        # average across sources perhaps, or do a primary/secondary priority
        self.consumptionValues[source] = value

    def setGeneration(self, source, value):
        self.generationValues[source] = value

    def setHomeLat(self, lat):
        self.settings["homeLat"] = lat

    def setHomeLon(self, lon):
        self.settings["homeLon"] = lon

    def setHourResumeTrackGreenEnergy(self, hour):
        self.settings["hourResumeTrackGreenEnergy"] = hour

    def setkWhDelivered(self, kWh):
        self.settings["kWhDelivered"] = kWh
        return True

    def setMaxAmpsToDivideAmongSlaves(self, amps):

        # Use backgroundTasksLock to prevent changing maxAmpsToDivideAmongSlaves
        # if the main thread is in the middle of examining and later using
        # that value.
        self.getBackgroundTasksLock()

        if amps > self.config["config"]["wiringMaxAmpsAllTWCs"]:
            # Never tell the slaves to draw more amps than the physical charger
            # wiring can handle.
            logger.info(
                "ERROR: specified maxAmpsToDivideAmongSlaves "
                + str(amps)
                + " > wiringMaxAmpsAllTWCs "
                + str(self.config["config"]["wiringMaxAmpsAllTWCs"])
                + ".\nSee notes above wiringMaxAmpsAllTWCs in the 'Configuration parameters' section."
            )
            amps = self.config["config"]["wiringMaxAmpsAllTWCs"]

        self.maxAmpsToDivideAmongSlaves = amps

        self.releaseBackgroundTasksLock()

        # Now that we have updated the maxAmpsToDivideAmongSlaves, send update
        # to console / MQTT / etc
        self.queue_background_task({"cmd": "updateStatus"})

    def setNonScheduledAmpsMax(self, amps):
        self.settings["nonScheduledAmpsMax"] = amps

    def setSendServerTime(self, val):
        self.settings["sendServerTime"] = val

    def setScheduledAmpsDaysBitmap(self, bitmap):
        self.settings["scheduledAmpsDaysBitmap"] = bitmap

    def setScheduledAmpsBatterySize(self, batterySize):
        if batterySize > 40:
            self.settings["scheduledAmpsBatterySize"] = batterySize

    def setScheduledAmpsMax(self, amps):
        self.settings["scheduledAmpsMax"] = amps

    def setScheduledAmpsStartHour(self, hour):
        self.settings["scheduledAmpsStartHour"] = hour

    def setScheduledAmpsEndHour(self, hour):
        self.settings["scheduledAmpsEndHour"] = hour

    def setScheduledAmpsFlexStart(self, enabled):
        self.settings["scheduledAmpsFlexStart"] = enabled

    def setSpikeAmps(self, amps):
        self.spikeAmpsToCancel6ALimit = amps

    def snapHistoryData(self):
        snaptime = self.nextHistorySnap
        avgCurrent = 0

        now = None
        try:
            now = datetime.now().astimezone()
            if now < snaptime:
                return
        except ValueError as e:
            logger.debug(str(e))
            return

        for evse in self.getAllEVSEs():
            avgCurrent += evse.snapHistoryData()
        self.advanceHistorySnap()

        if avgCurrent > 0:
            periodTimestamp = snaptime - timedelta(minutes=5)

            if not "history" in self.settings:
                self.settings["history"] = []

            self.settings["history"].append(
                (
                    periodTimestamp.isoformat(timespec="seconds"),
                    self.convertAmpsToWatts(avgCurrent)
                    * self.getRealPowerFactor(avgCurrent),
                )
            )

            self.settings["history"] = [
                e
                for e in self.settings["history"]
                if datetime.fromisoformat(e[0]) >= (now - timedelta(days=2))
            ]
            self.queue_background_task({"cmd": "saveSettings"})

    def startCarsCharging(self):
        # This function is the opposite functionality to the stopCarsCharging function
        # below
        stopMode = int(self.settings.get("chargeStopMode", 1))
        if stopMode == 1:
            self.queue_background_task({"cmd": "charge", "charge": True})
            self.getModuleByName("Policy").clearOverride()
        elif stopMode == 2:
            self.settings["respondToSlaves"] = 1
        elif stopMode == 3:
            self.queue_background_task({"cmd": "charge", "charge": True})

    def stopCarsCharging(self):
        # This is called by components who want to signal to us to
        # call our configured routine for stopping vehicles from charging.
        # The default setting is to use the Tesla API. Some people may not want to do
        # this, as it only works for Tesla vehicles and requires logging in with your
        # Tesla credentials. The alternate option is to stop responding to slaves

        # 1 = Stop the car(s) charging via the Tesla API
        # 2 = Stop the car(s) charging by refusing to respond to slave TWCs
        # 3 = Send TWC Stop command to each slave
        stopMode = int(self.settings.get("chargeStopMode", 1))
        if stopMode == 1:
            self.queue_background_task({"cmd": "charge", "charge": False})
            if self.stopTimeout == datetime.max:
                self.stopTimeout = datetime.now() + timedelta(seconds=10)
            elif datetime.now() > self.stopTimeout:
                self.getModuleByName("Policy").overrideLimit()
        if stopMode == 2:
            self.settings["respondToSlaves"] = 0
            self.settings["respondToSlavesExpiry"] = time.time() + 60
        if stopMode == 3:
            for module in self.getModulesByType("EVSEController"):
                module["ref"].stopCharging()

    def time_now(self):
        return datetime.now().strftime(
            "%H:%M:%S" + (".%f" if self.config["config"]["displayMilliseconds"] else "")
        )

    def tokenSyncEnabled(self):
        # TODO: Should not be hardcoded
        # Check if any modules are performing token sync from other projects or interfaces
        # if so, we do not prompt locally for authentication and we don't use our own settings
        tokenSync = False

        if self.getModuleByName("TeslaMateVehicle"):
            if self.getModuleByName("TeslaMateVehicle").syncTokens:
                tokenSync = True

        return tokenSync

    def translateModuleNameToConfig(self, modulename):
        # This function takes a module name (eg. EMS.Fronius) and returns a config section (Sources.Fronius)
        # It makes it easier for us to determine where a module's config should be
        configloc = ["", ""]
        if modulename[0] == "Control":
            configloc[0] = "control"
            configloc[1] = str(modulename[1]).replace("Control", "")
        elif modulename[0] == "EMS":
            configloc[0] = "sources"
            configloc[1] = modulename[1]
        elif modulename[0] == "Interface":
            configloc[0] = "interface"
            configloc[1] = modulename[1]
        elif modulename[0] == "Logging":
            configloc[0] = "logging"
            configloc[1] = str(modulename[1]).replace("Logging", "")
        elif modulename[0] == "Status":
            configloc[0] = "status"
            configloc[1] = str(modulename[1]).replace("Status", "")
        else:
            return modulename

        return configloc

    def updateSlaveLifetime(self, sender, kWh, vPA, vPB, vPC):
        for slaveTWC in self.getAllEVSEs():
            if slaveTWC.TWCID == sender:
                slaveTWC.setLifetimekWh(kWh)
                slaveTWC.setVoltage(vPA, vPB, vPC)

    def updateVINStatus(self):
        # update current and last VIN IDs for each Slave to all Status modules
        for slaveTWC in self.getAllEVSEs():
            for module in self.getModulesByType("Status"):
                module["ref"].setStatus(
                    slaveTWC.TWCID,
                    "current_vehicle_vin",
                    "currentVehicleVIN",
                    slaveTWC.currentVIN,
                    "",
                )
            for module in self.getModulesByType("Status"):
                module["ref"].setStatus(
                    slaveTWC.TWCID,
                    "last_vehicle_vin",
                    "lastVehicleVIN",
                    slaveTWC.lastVIN,
                    "",
                )

    def refreshingTotalAmpsInUseStatus(self):
        for module in self.getModulesByType("Status"):
            module["ref"].setStatus(
                bytes("all", "UTF-8"),
                "total_amps_in_use",
                "totalAmpsInUse",
                self.getTotalAmpsInUse(),
                "A",
            )

    def getRealPowerFactor(self, amps):
        realPowerFactorMinAmps = self.config["config"].get("realPowerFactorMinAmps", 1)
        realPowerFactorMaxAmps = self.config["config"].get("realPowerFactorMaxAmps", 1)
        minAmps = self.config["config"]["minAmpsPerTWC"]
        maxAmps = self.config["config"]["wiringMaxAmpsAllTWCs"]
        if minAmps == maxAmps:
            return realPowerFactorMaxAmps
        else:
            return (
                (amps - minAmps)
                / (maxAmps - minAmps)
                * (realPowerFactorMaxAmps - realPowerFactorMinAmps)
            ) + realPowerFactorMinAmps

    def rotl(self, num, bits):
        bit = num & (1 << (bits - 1))
        num <<= 1
        if bit:
            num |= 1
        num &= 2**bits - 1

        return num

    def getAllEVSEs(self):
        return [
            evse
            for controller in self.getModulesByType("EVSEController")
            for evse in controller["ref"].allEVSEs
        ]
