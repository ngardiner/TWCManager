#! /usr/bin/python3

################################################################################
# Code and TWC protocol reverse engineering by Chris Dragon.
#
# Additional logs and hints provided by Teslamotorsclub.com users:
#   TheNoOne, IanAmber, and twc.
# Thank you!
#
# For support and information, please read through this thread:
# https://teslamotorsclub.com/tmc/threads/new-wall-connector-load-sharing-protocol.72830
#
# Report bugs at https://github.com/ngardiner/TWCManager/issues
#
# This software is released under the "Unlicense" model: http://unlicense.org
# This means source code and TWC protocol knowledge are released to the general
# public free for personal or commercial use. I hope the knowledge will be used
# to increase the use of green energy sources by controlling the time and power
# level of car charging.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please visit http://unlicense.org

import importlib
import logging
import os.path
import math
import re
import sys
import time
import traceback
import datetime
import yaml
import threading
from TWCManager.TWCMaster import TWCMaster
import requests


logging.addLevelName(19, "INFO2")
logging.addLevelName(18, "INFO4")
logging.addLevelName(17, "INFO4")
logging.addLevelName(16, "INFO5")
logging.addLevelName(15, "INFO6")
logging.addLevelName(14, "INFO7")
logging.addLevelName(13, "INFO8")
logging.addLevelName(12, "INFO9")
logging.addLevelName(9, "DEBUG2")
logging.INFO2 = 19
logging.INFO3 = 18
logging.INFO4 = 17
logging.INFO5 = 16
logging.INFO6 = 15
logging.INFO7 = 14
logging.INFO8 = 13
logging.INFO9 = 12
logging.DEBUG2 = 9


logger = logging.getLogger("\u26FD Manager")

# Define available modules for the instantiator
# All listed modules will be loaded at boot time
# Logging modules should be the first one to load
modules_available = [
    "Logging.ConsoleLogging",
    "Logging.FileLogging",
    "Logging.SentryLogging",
    "Logging.CSVLogging",
    "Logging.MySQLLogging",
    "Logging.SQLiteLogging",
    "Protocol.TWCProtocol",
    "Interface.Dummy",
    "Interface.RS485",
    "Interface.TCP",
    "Policy.Policy",
    "Vehicle.TeslaAPI",
    "Vehicle.TeslaMateVehicle",
    "Control.WebIPCControl",
    "Control.HTTPControl",
    "Control.MQTTControl",
    #    "Control.OCPPControl",
    "EMS.Efergy",
    "EMS.EmonCMS",
    "EMS.Enphase",
    "EMS.Fronius",
    "EMS.Growatt",
    "EMS.HASS",
    "EMS.IotaWatt",
    "EMS.Kostal",
    "EMS.MQTT",
    "EMS.OpenHab",
    "EMS.OpenWeatherMap",
    "EMS.P1Monitor",
    "EMS.SmartMe",
    "EMS.SmartPi",
    "EMS.SolarEdge",
    "EMS.SolarLog",
    "EMS.TeslaPowerwall2",
    "EMS.TED",
    "EMS.Volkszahler",
    "EMS.URL",
    "Status.HASSStatus",
    "Status.MQTTStatus",
    "EVSEController.Gen2TWCs",
    "EVSEController.TeslaAPIController",
]

# Enable support for Python Visual Studio Debugger
if "DEBUG_SECRET" in os.environ:
    import ptvsd

    ptvsd.enable_attach(os.environ["DEBUG_SECRET"])
    ptvsd.wait_for_attach()

##########################
# Load Configuration File
config = None
jsonconfig = None
if os.path.isfile("/etc/twcmanager/config.json"):
    jsonconfig = open("/etc/twcmanager/config.json")
else:
    if os.path.isfile("config.json"):
        jsonconfig = open("config.json")

if jsonconfig:
    configtext = ""
    for line in jsonconfig:
        if line.lstrip().startswith("//") or line.lstrip().startswith("#"):
            configtext += "\n"
        else:
            configtext += line.replace("\t", " ").split("#")[0]

    config = yaml.safe_load(configtext)
    configtext = None
else:
    logger.error("Unable to find a configuration file.")
    sys.exit()


logLevel = config["config"].get("logLevel")
if logLevel == None:
    debugLevel = config["config"].get("debugLevel", 1)
    debug_to_log = {
        0: 40,
        1: 20,
        2: 19,
        3: 18,
        4: 17,
        5: 16,
        6: 15,
        7: 14,
        8: 13,
        9: 12,
        10: 10,
        11: 9,
    }
    for debug, log in debug_to_log.items():
        if debug >= debugLevel:
            logLevel = log
            break

logging.getLogger().setLevel(logLevel)

# All TWCs ship with a random two-byte TWCID. We default to using 0x7777 as our
# fake TWC ID. There is a 1 in 64535 chance that this ID will match each real
# TWC on the network, in which case you should pick a different random id below.
# This isn't really too important because even if this ID matches another TWC on
# the network, that TWC will pick its own new random ID as soon as it sees ours
# conflicts.
fakeTWCID = bytearray(b"\x77\x77")

#
# End configuration parameters
#
##############################


##############################
#
# Begin functions
#


def time_now():
    global config
    return datetime.datetime.now().strftime(
        "%H:%M:%S" + (".%f" if config["config"]["displayMilliseconds"] else "")
    )


def background_tasks_thread(master):
    carapi = master.getModuleByName("TeslaAPI")

    while True:
        try:
            task = master.getBackgroundTask()

            if "cmd" in task:
                if task["cmd"] == "applyChargeLimit":
                    carapi.applyChargeLimit(limit=task["limit"])
                elif task["cmd"] == "charge":
                    # car_api_charge does nothing if it's been under 60 secs since it
                    # was last used so we shouldn't have to worry about calling this
                    # too frequently.
                    carapi.car_api_charge(task["charge"], task.get("vin", None))
                elif task["cmd"] == "carApiEmailPassword":
                    carapi.resetCarApiLastErrorTime()
                    carapi.car_api_available(task["email"], task["password"])
                elif task["cmd"] == "checkArrival":
                    limit = (
                        carapi.lastChargeLimitApplied
                        if carapi.lastChargeLimitApplied != 0
                        else -1
                    )
                    carapi.applyChargeLimit(limit=limit, checkArrival=True)
                elif task["cmd"] == "checkCharge":
                    carapi.updateChargeAtHome()
                elif task["cmd"] == "checkDeparture":
                    carapi.applyChargeLimit(
                        limit=carapi.lastChargeLimitApplied, checkDeparture=True
                    )
                elif task["cmd"] == "checkGreenEnergy":
                    check_green_energy()
                elif task["cmd"] == "checkVINEntitlement":
                    # The two possible arguments are task["subTWC"] which tells us
                    # which TWC to check, or task["vin"] which tells us which VIN
                    if not task.get("vin", None):
                        task["vin"] = master.getEVSEbyID(task["subTWC"]).currentVIN

                    if task.get("vin", None):
                        if master.checkVINEntitlement(task["vin"]):
                            logger.info(
                                "Vehicle %s is permitted to charge." % (task["vin"],)
                            )
                        else:
                            logger.info(
                                "Vehicle %s is not permitted to charge. Terminating session."
                                % (task["subTWC"].currentVIN,)
                            )
                            master.sendStopCommand(task["vin"])

                elif task["cmd"] == "getLifetimekWh":
                    module = master.getModuleByName("Gen2TWCs")
                    if module:
                        module.getLifetimekWh()
                elif task["cmd"] == "getVehicleVIN":
                    module = master.getModuleByName("Gen2TWCs")
                    if module:
                        module.getVehicleVIN(task["slaveTWC"], task["vinPart"])
                elif task["cmd"] == "snapHistoryData":
                    master.snapHistoryData()
                elif task["cmd"] == "updateStatus":
                    update_statuses()
                elif task["cmd"] == "webhook":
                    if config["config"].get("webhookMethod", "POST") == "GET":
                        requests.get(task["url"])
                    else:
                        body = master.getStatus()
                        requests.post(task["url"], json=body)
                elif task["cmd"] == "saveSettings":
                    master.saveSettings()
                elif task["cmd"] == "sunrise":
                    update_sunrise_sunset()

        except:
            logger.info(
                "%s: "
                + traceback.format_exc()
                + ", occurred when processing background task",
                "BackgroundError",
                extra={"colored": "red"},
            )
            pass

        # task_done() must be called to let the queue know the task is finished.
        # backgroundTasksQueue.join() can then be used to block until all tasks
        # in the queue are done.
        master.doneBackgroundTask(task)


def check_green_energy():
    global config, hass, master

    # Check solar panel generation using an API exposed by
    # the HomeAssistant API.
    #
    # You may need to customize the sensor entity_id values
    # to match those used in your environment. This is configured
    # in the config section at the top of this file.
    #

    # Poll all loaded EMS modules for consumption and generation values
    for module in master.getModulesByType("EMS"):
        master.setConsumption(module["name"], module["ref"].getConsumption())
        master.setGeneration(module["name"], module["ref"].getGeneration())

    # Set max amps iff charge_amps isn't specified on the policy.
    if master.getModuleByName("Policy").policyIsGreen():
        master.setMaxAmpsToDivideAmongSlaves(master.getMaxAmpsToDivideGreenEnergy())


def update_statuses():

    # Print a status update if we are on track green energy showing the
    # generation and consumption figures
    maxamps = master.getMaxAmpsToDivideAmongSlaves()
    maxampsDisplay = f"{maxamps:.2f}A"
    if master.getModuleByName("Policy").policyIsGreen():
        genwatts = master.getGeneration()
        conwatts = master.getConsumption()
        conoffset = master.getConsumptionOffset()
        chgwatts = master.getChargerLoad()
        othwatts = 0

        if config["config"]["subtractChargerLoad"]:
            if conwatts > 0:
                othwatts = conwatts - chgwatts

            if conoffset > 0:
                othwatts -= conoffset

        # Extra parameters to send with logs
        logExtra = {
            "logtype": "green_energy",
            "genWatts": genwatts,
            "conWatts": conwatts,
            "chgWatts": chgwatts,
            "colored": "magenta",
        }

        if (genwatts or conwatts) and (not conoffset and not othwatts):

            logger.info(
                "Green energy Generates %s, Consumption %s (Charger Load %s)",
                f"{genwatts:.0f}W",
                f"{conwatts:.0f}W",
                f"{chgwatts:.0f}W",
                extra=logExtra,
            )

        elif (genwatts or conwatts) and othwatts and not conoffset:

            logger.info(
                "Green energy Generates %s, Consumption %s (Charger Load %s, Other Load %s)",
                f"{genwatts:.0f}W",
                f"{conwatts:.0f}W",
                f"{chgwatts:.0f}W",
                f"{othwatts:.0f}W",
                extra=logExtra,
            )

        elif (genwatts or conwatts) and othwatts and conoffset > 0:

            logger.info(
                "Green energy Generates %s, Consumption %s (Charger Load %s, Other Load %s, Offset %s)",
                f"{genwatts:.0f}W",
                f"{conwatts:.0f}W",
                f"{chgwatts:.0f}W",
                f"{othwatts:.0f}W",
                f"{conoffset:.0f}W",
                extra=logExtra,
            )

        elif (genwatts or conwatts) and othwatts and conoffset < 0:

            logger.info(
                "Green energy Generates %s (Offset %s), Consumption %s (Charger Load %s, Other Load %s)",
                f"{genwatts:.0f}W",
                f"{(-1 * conoffset):.0f}W",
                f"{conwatts:.0f}W",
                f"{chgwatts:.0f}W",
                f"{othwatts:.0f}W",
                extra=logExtra,
            )

        nominalOffer = master.convertWattsToAmps(
            genwatts
            + (
                chgwatts
                if (config["config"]["subtractChargerLoad"] and conwatts == 0)
                else 0
            )
            - (
                conwatts
                - (
                    chgwatts
                    if (config["config"]["subtractChargerLoad"] and conwatts > 0)
                    else 0
                )
            )
        )
        if abs(maxamps - nominalOffer) > 0.005:
            nominalOfferDisplay = f"{nominalOffer:.2f}A"
            logger.debug(
                f"Offering {maxampsDisplay} instead of {nominalOfferDisplay} to compensate for inexact current draw"
            )
            conwatts = genwatts - master.convertAmpsToWatts(maxamps)
        generation = f"{master.convertWattsToAmps(genwatts):.2f}A"
        consumption = f"{master.convertWattsToAmps(conwatts):.2f}A"
        logger.info(
            "Limiting charging to %s - %s = %s.",
            generation,
            consumption,
            maxampsDisplay,
            extra={"colored": "magenta"},
        )

    else:
        # For all other modes, simply show the Amps to charge at
        logger.info(
            "Limiting charging to %s.", maxampsDisplay, extra={"colored": "magenta"}
        )

    # Print minimum charge for all charging policies
    minchg = f"{config['config']['minAmpsPerTWC']}A"
    logger.info(
        "Charge when above %s (minAmpsPerTWC).", minchg, extra={"colored": "magenta"}
    )

    # Update Sensors with min/max amp values
    for module in master.getModulesByType("Status"):
        module["ref"].setStatus(
            bytes("config", "UTF-8"),
            "min_amps_per_twc",
            "minAmpsPerTWC",
            config["config"]["minAmpsPerTWC"],
            "A",
        )
        module["ref"].setStatus(
            bytes("all", "UTF-8"),
            "max_amps_for_slaves",
            "maxAmpsForSlaves",
            master.getMaxAmpsToDivideAmongSlaves(),
            "A",
        )


def update_sunrise_sunset():

    ltNow = time.localtime()
    latlong = master.getHomeLatLon()
    if latlong[0] == 10000:
        # We don't know where home is; keep defaults
        master.settings["sunrise"] = 6
        master.settings["sunset"] = 20
    else:
        sunrise = 6
        sunset = 20
        url = (
            "https://api.sunrise-sunset.org/json?lat="
            + str(latlong[0])
            + "&lng="
            + str(latlong[1])
            + "&formatted=0&date="
            + "-".join([str(ltNow.tm_year), str(ltNow.tm_mon), str(ltNow.tm_mday)])
        )

        r = {}
        try:
            r = requests.get(url).json().get("results")
        except:
            pass

        if r.get("sunrise", None):
            try:
                dtSunrise = datetime.datetime.astimezone(
                    datetime.datetime.fromisoformat(r["sunrise"])
                )
                sunrise = dtSunrise.hour + (1 if dtSunrise.minute >= 30 else 0)
            except:
                pass

        if r.get("sunset", None):
            try:
                dtSunset = datetime.datetime.astimezone(
                    datetime.datetime.fromisoformat(r["sunset"])
                )
                sunset = dtSunset.hour + (1 if dtSunset.minute >= 30 else 0)
            except:
                pass

        master.settings["sunrise"] = sunrise
        master.settings["sunset"] = sunset

    tomorrow = datetime.datetime.combine(
        datetime.datetime.today(), datetime.time(hour=1)
    ) + datetime.timedelta(days=1)
    diff = tomorrow - datetime.datetime.now()
    master.queue_background_task({"cmd": "sunrise"}, diff.total_seconds())


#
# End functions
#
##############################

##############################
#
# Begin global vars
#

webMsgPacked = ""
webMsgMaxSize = 300
webMsgResult = 0

#
# End global vars
#
##############################


##############################
#
# Begin main program
#

# Instantiate necessary classes
master = TWCMaster(fakeTWCID, config)

# Instantiate all modules in the modules_available list automatically
for module in modules_available:
    modulename = []
    if str(module).find(".") != -1:
        modulename = str(module).split(".")

    try:
        # Pre-emptively skip modules that we know are not configured
        configlocation = master.translateModuleNameToConfig(modulename)
        if (
            not config.get(configlocation[0], {})
            .get(configlocation[1], {})
            .get("enabled", 1)
        ):
            # We can see that this module is explicitly disabled in config, skip it
            continue

        moduleref = importlib.import_module("TWCManager." + module)
        modclassref = getattr(moduleref, modulename[1])
        modinstance = modclassref(master)

        # Register the new module with master class, so every other module can
        # interact with it
        master.registerModule(
            {"name": modulename[1], "ref": modinstance, "type": modulename[0]}
        )
    except ImportError as e:
        logger.error(
            "%s: " + str(e) + ", when importing %s, not using %s",
            "ImportError",
            module,
            module,
            extra={"colored": "red"},
        )
    except ModuleNotFoundError as e:
        logger.info(
            "%s: " + str(e) + ", when importing %s, not using %s",
            "ModuleNotFoundError",
            module,
            module,
            extra={"colored": "red"},
        )
    except:
        raise


# Load settings from file
master.loadSettings()

# Create a background thread to handle tasks that take too long on the main
# thread.  For a primer on threads in Python, see:
# http://www.laurentluce.com/posts/python-threads-synchronization-locks-rlocks-semaphores-conditions-events-and-queues/
backgroundTasksThread = threading.Thread(target=background_tasks_thread, args=(master,))
backgroundTasksThread.daemon = True
backgroundTasksThread.start()

master.queue_background_task({"cmd": "sunrise"}, 30)

lastDistributePower = 0
startStopDelay = config["config"].get("startStopDelay", 60)

while True:
    try:
        time.sleep(0.025)

        # See if there's any message from the web interface.
        if master.getModuleByName("WebIPCControl"):
            master.getModuleByName("WebIPCControl").processIPC()

        # TODO - check if Distribute Power needs to run
        if time.time() - lastDistributePower < 5:
            # We don't need to run Distribute Power yet
            continue
        lastDistributePower = time.time()

        # Determine our charging policy. This is the policy engine of the
        # TWCManager application. Using defined rules, we can determine how we
        # charge.
        #
        # Note that policy may re-evaluate less often than Distribute Power
        # runs.
        master.getModuleByName("Policy").setChargingPerPolicy()

        # Distribute power to the EVSEs
        allEVSEs = master.getDedupedEVSEs()

        # If we have no EVSEs, we can't distribute power
        if len(allEVSEs) == 0:
            continue

        # First, determine the ideal power distribution
        #
        # This is available power distributed evenly between all EVSEs which
        # are or would like to draw power.

        maxPower = master.getMaxPowerToDivideAmongSlaves()
        availableFlex = master.getAllowedFlex()
        useFlexToStart = config["config"].get("useFlexAmpsToStartCharge", False)

        EVSEsCharging = [evse for evse in allEVSEs if evse.isCharging]
        EVSEsChargingOrWantToCharge = [
            evse for evse in allEVSEs if evse.isCharging or evse.wantsToCharge
        ]
        if len(EVSEsChargingOrWantToCharge) == 0:
            # No EVSEs want power, so have it ready in case that changes.
            EVSEsChargingOrWantToCharge = allEVSEs

        # If we can afford to give all EVSEs minimum power, keep the ones that
        # want to charge
        EVSEsGetPower = []
        if maxPower + (availableFlex if useFlexToStart else 0) >= sum(
            [evse.minPower for evse in EVSEsChargingOrWantToCharge]
        ):
            EVSEsGetPower = EVSEsChargingOrWantToCharge
        else:
            EVSEsGetPower = EVSEsCharging

        # If we can't afford to give all EVSEs minimum power, we have to ignore
        # some even though they'd like to charge.
        while (
            len(EVSEsGetPower) > 0
            and sum(evse.minPower for evse in EVSEsGetPower) > maxPower
        ):
            if sum(evse.minPower for evse in EVSEsGetPower) - availableFlex <= maxPower:
                # We can afford to give remaining EVSEs minimum power with flex
                maxPower = sum(evse.minPower for evse in EVSEsGetPower)
                availableFlex = 0
            else:
                EVSEsGetPower.pop(-1)

        countPowerRecipients = len(EVSEsGetPower)

        # Find controller limits
        modules = master.getModulesByType("EVSEController")
        moduleLimits = {}
        moduleCounts = {}
        for module in modules:
            moduleLimits[module["name"]] = module["ref"].maxPower
            moduleCounts[module["name"]] = sum(
                1 for evse in EVSEsGetPower if module["name"] in evse.controllers
            )

        # Now, distribute power to EVSEs
        currentPower = sum(evse.currentPower for evse in allEVSEs)
        for evse in sorted(
            allEVSEs, key=lambda evse: (evse.maxPower, evse.currentPower)
        ):
            instantOffer = 0
            if evse in EVSEsGetPower:
                # This EVSE gets power, so give it the target power modulo some
                # safety adjustments
                limits = [
                    # A fair share of the remaining power
                    maxPower / countPowerRecipients,
                    # ...unless that's more than it can use...
                    evse.maxPower,
                ]
                for controller in evse.controllers:
                    # ...or more than the controller can handle.
                    limits.append(moduleLimits[controller] / moduleCounts[controller])

                amountToOffer = min(limits)
                # Ensure we wait for other EVSEs to back off before increasing
                # this one too much.
                instantOffer = max(
                    [
                        0,
                        min(
                            [
                                amountToOffer,
                                maxPower - currentPower + evse.currentPower,
                            ]
                        ),
                    ]
                )

                if evse.isReadOnly:
                    # This EVSE is read-only, so we can't change its power
                    # settings.  We can only respond to what it's doing.
                    amountToOffer = evse.currentPower

                # If this couldn't take the full amount, others can get more.
                maxPower -= amountToOffer
                for controller in evse.controllers:
                    moduleLimits[controller] -= amountToOffer
                    moduleCounts[controller] -= 1
                countPowerRecipients -= 1

            if not evse.isReadOnly:
                # If EVSE has changed recently, don't cut it off yet.
                #
                # This both allows for the possibility that power might become
                # available, and also allows the VIN query to detect that we
                # can use the API instead of disconnecting power abruptly.
                minPower = (
                    evse.minPower
                    if evse.currentPower > 0
                    and time.time() - evse.lastPowerChange < startStopDelay
                    else 0
                )
                instantOffer = max([minPower, instantOffer])
                evse.setTargetPower(instantOffer)
                if instantOffer > 0 and evse.currentPower == 0:
                    # Notify the EVSE to start charging
                    evse.startCharging()
                if instantOffer == 0 and evse.currentPower > 0:
                    # Notify the EVSE to stop charging
                    evse.stopCharging()

    except KeyboardInterrupt:
        logger.info("Exiting after background tasks complete...")
        break

# Make sure any volatile data is written to disk before exiting
master.queue_background_task({"cmd": "saveSettings"})

# Wait for background tasks thread to finish all tasks.
# Note that there is no such thing as backgroundTasksThread.stop(). Because we
# set the thread type to daemon, it will be automatically killed when we exit
# this program.
master.backgroundTasksQueue.join()

for module in master.getModulesByType("EVSEController"):
    module["ref"].stop()

#
# End main program
#
##############################
