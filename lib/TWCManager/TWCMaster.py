#! /usr/bin/python3

from TWCManager.EVSEController.Gen2TWCs import Gen2TWCs
from TWCManager.EVSEInstance.Gen2TWC import Gen2TWC as TWCSlave
from datetime import datetime, timedelta
import json
import logging
import os.path
import queue
from sys import modules
import threading
import time
import math
import random
import requests
import bisect
import itertools
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Vehicle.Registry import VehicleRegistry
from TWCManager.Vehicle.Registry.VehicleRegistry import isAnonKey
from TWCManager.Vehicle.Providers import ProviderOrder
from TWCManager.Vehicle.Providers.Gen2TWCProvider import Gen2TWCProvider
from TWCManager.Vehicle.Providers.Capability import (
    READ_CHARGE,
    START_CHARGING,
    STOP_CHARGING,
)
from TWCManager.Vehicle.Reconcile.Desired import CHARGING, DesiredStore
from TWCManager.Vehicle.Reconcile.HeldLimits import HeldLimits
from TWCManager.Vehicle.Reconcile.Belief import newerReading
from TWCManager.Vehicle.Reconcile.Reconciler import VehicleReconciler
from TWCManager.Vehicle.Reconcile.Targets import (
    applyPolicyLimit,
    broadcastVehicles,
    targetsFor,
)

logger = LoggerFactory.get_logger("Master", "Master")


class TWCMaster:
    allowed_flex = 0
    backgroundTasksQueue = queue.Queue()
    backgroundTasksCmds = {}
    backgroundTasksLock = threading.Lock()
    backgroundTasksDelayed = []
    # Tie-breaker for delayed tasks due at the same instant: entries never
    # fall through to comparing task dicts.
    _delaySeq = itertools.count()
    config = None
    consumptionValues = {}
    consumptionAmpsValues = {}
    debugOutputToFile = False
    generationValues = {}
    lastkWhMessage = time.time()
    lastkWhPoll = 0
    lastTeslaTokenRefreshCheck = 0
    lastSaveFailed = 0
    lastTWCResponseMsg = None
    lastUpdateCheck = 0
    limitAmpsToDivideAmongSlaves = 0
    masterTWCID = ""
    maxAmpsToDivideAmongSlaves = 0
    modules = {}
    nextHistorySnap = 0
    overrideMasterHeartbeatData = b""
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
    slaveHeartbeatData = bytearray(
        [0x01, 0x0F, 0xA0, 0x0F, 0xA0, 0x00, 0x00, 0x00, 0x00]
    )
    slaveTWCs = {}
    slaveTWCRoundRobin = []
    stopTimeout = datetime.max
    spikeAmpsToCancel6ALimit = 16
    subtractChargerLoad = False
    treatGenerationAsGridDelivery = False
    TWCID = None
    updateVersion = False
    version = "1.4.0"

    # TWCs send a seemingly-random byte after their 2-byte TWC id in a number of
    # messages. I call this byte their "Sign" for lack of a better term. The byte
    # never changes unless the TWC is reset or power cycled. We use hard-coded
    # values for now because I don't know if there are any rules to what values can
    # be chosen. I picked 77 because it's easy to recognize when looking at logs.
    # These shouldn't need to be changed.
    masterSign = bytearray(b"\x77")
    slaveSign = bytearray(b"\x77")

    def __init__(self, TWCID, config):
        self.config = config
        self.debugOutputToFile = config["config"].get("debugOutputToFile", False)
        self.TWCID = TWCID
        self.subtractChargerLoad = config["config"].get("subtractChargerLoad", False)
        self.treatGenerationAsGridDelivery = config["config"].get(
            "treatGenerationAsGridDelivery", False
        )
        # Instance-level mutable state (class-level definitions are defaults only)
        self.modules = {}
        self.slaveTWCRoundRobin = []
        self.slaveTWCs = {}
        # Guards mutation of self.settings from non-serial threads; saves
        # serialise under it and write the file outside it. Lock order:
        # TeslaBLE._bleLock -> settingsLock -> registry/vehicle locks.
        # The serial thread takes it too (recordVehicleVIN), so nothing
        # under it may do I/O, on any thread. TeslaAPI.vehicleListLock ->
        # settingsLock too (a listing resolves legacy held limits): never take
        # vehicleListLock while holding settingsLock.
        self.settingsLock = threading.RLock()
        self.vehicleRegistry = VehicleRegistry(config)
        self.heldLimits = HeldLimits(self)
        self.providerOrder = ProviderOrder(self)
        self.providerOrder.register(Gen2TWCProvider(self))
        self.vehicleRegistry.providerOrder = self.providerOrder
        # Desired vehicle state, and the reconciler that converges cars on
        # it; all on the registry's time.time clock
        self.desired = DesiredStore()
        self.vehicleRegistry.removed = self.desired.forget
        self.reconciler = VehicleReconciler(self)
        # The policy's charge limit; None while it sets none
        self.policyLimit = None
        # Start of the forgetting interval: the first expiry pass runs an
        # hour after startup, once providers have reclaimed their cars, so a
        # persisted car Fleet still lists is not expired and re-created.
        self.lastVehicleExpiry = time.time()
        self.settings = {
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
        self.advanceHistorySnap()
        if config["config"].get("maxAmpsAllowedFromGrid", None) is None:
            self.setLimitAmpsToDivideAmongSlaves(
                self.config["config"]["wiringMaxAmpsAllTWCs"]
            )

        # Register ourself as a module, allows lookups via the Module architecture
        self.registerModule({"name": "master", "ref": self, "type": "Master"})

        # Register the Gen2TWCs EVSEController so power distribution can
        # discover RS485 slaves through the unified EVSEController interface
        self.registerModule(
            {"name": "Gen2TWCs", "ref": Gen2TWCs(self), "type": "EVSEController"}
        )

    def addkWhDelivered(self, kWh):
        self.settings["kWhDelivered"] = self.settings.get("kWhDelivered", 0) + kWh

    def addSlaveTWC(self, slaveTWC):
        # Adds the Slave TWC to the Round Robin list
        return self.slaveTWCRoundRobin.append(slaveTWC)

    def advanceHistorySnap(self):
        try:
            futureSnap = datetime.now().astimezone() + timedelta(minutes=5)
            self.nextHistorySnap = futureSnap.replace(
                minute=math.floor(futureSnap.minute / 5) * 5, second=0, microsecond=0
            )
        except ValueError as e:
            logger.debug("Exception in advanceHistorySnap: " + str(e))

    def cancelStopCarsCharging(self, vin=None):
        # Drops a stop not yet delivered: the desire and any queued attempt.
        # A start is never cancelled. Returns how many desires were cleared
        cleared = 0
        for v in self._chargeTargets(vin, STOP_CHARGING):
            cleared += self.desired.clearIf(v.key, CHARGING, False)
            self.reconciler.cancel(v.key)
        return cleared

    def withdrawStartCharging(self, vin):
        # A start not wanted any more (too little on offer): its desire goes;
        # its record stays for the next start (I4)
        for v in self._chargeTargets(vin, START_CHARGING):
            self.desired.clearIf(v.key, CHARGING, True)

    def _chargeTargets(self, vin, capability):
        # As main: a named car; for an anonymous point, a mode-1 start or
        # stop and a mode-3 start went unaddressed (broadcastVehicles)
        targets = targetsFor(self, vin or None, capability)
        mode = int(self.settings.get("chargeStopMode", 1))
        if (
            len(targets) == 1
            and targets[0].isAnonymous
            and (mode == 1 or mode == 3 and capability == START_CHARGING)
        ):
            # The anonymous car itself has no candidate in these modes
            targets = broadcastVehicles(self, capability)
        return targets

    def _wantCharging(self, vin, value):
        capability = START_CHARGING if value else STOP_CHARGING
        for v in self._chargeTargets(vin, capability):
            # The same value withdrawn earlier keeps its record inside main's
            # 60 s gate, unless the car has reported since its last send
            rec = self.desired.record(v.key, CHARGING)
            keep = rec.lastSentAt > 0 and not newerReading(v, CHARGING, rec.lastSentAt)
            keep = keep and self.desired._clock() - rec.lastSentAt < 60
            self.desired.set(v.key, CHARGING, value, "distributor", keep=keep)

    def checkForUpdates(self):
        # This function is used by the Web UI and later on will be used by the console to detect TWCManager Updates
        # It runs a maximum of once per hour, and queries the current PyPi package version vs the current version
        # If they match, it returns false. If there's a different version available, we alert the user
        if time.time() > self.lastUpdateCheck + (60 * 60):
            self.lastUpdateCheck = time.time()

            # Fetch the JSON data from PyPi for our package
            url = "https://pypi.org/pypi/twcmanager/json"
            req = None
            pkgInfo = None

            try:
                req = requests.get(url)
                logger.log(logging.INFO8, "Requesting PyPi package info " + str(req))
                pkgInfo = json.loads(req.text)
            except requests.exceptions.RequestException:
                logger.info("Failed to fetch package details: " + url)
                if req:
                    logger.log(logging.INFO6, "Response: " + req.text)
                pass
            except json.decoder.JSONDecodeError:
                logger.info("Could not parse JSON result from " + url)
                logger.log(logging.INFO6, "Response: " + req.text)
                pass

            if pkgInfo and pkgInfo.get("info", {}).get("version", None):
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

    def checkVINEntitlement(self, subTWC):
        # When provided with the TWC that has had the VIN reported for a vehicle
        # we check the policy for charging and determine if it is allowed or not

        if not subTWC.currentVIN:
            # No VIN supplied. We can't make any decision other than allow
            return 1

        if str(self.settings.get("chargeAuthorizationMode", "1")) == "1":
            # In this mode, we allow all vehicles to charge unless they
            # are explicitly banned from charging
            if (
                subTWC.currentVIN
                in self.settings["VehicleGroups"]["Deny Charging"]["Members"]
            ):
                return 0
            else:
                return 1

        elif str(self.settings.get("chargeAuthorizationMode", "1")) == "2":
            # In this mode, vehicles may only charge if they are listed
            # in the Allowed VINs list
            if (
                subTWC.currentVIN
                in self.settings["VehicleGroups"]["Allow Charging"]["Members"]
            ):
                return 1
            else:
                return 0

    def convertAmpsToWatts(self, amps):
        voltage, phases = self.getVoltageMeasurement()
        return phases * voltage * amps

    def convertWattsToAmps(self, watts):
        voltage, phases = self.getVoltageMeasurement()
        return watts / (phases * voltage)

    def countSlaveTWC(self):
        return int(len(self.slaveTWCRoundRobin))

    @staticmethod
    def _taskKey(task):
        # Tasks dedupe per cmd, except VIN queries, which dedupe per TWC so
        # one TWC's query never replaces another's, and home checks, per VIN.
        if task["cmd"] == "getVehicleVIN":
            return ("getVehicleVIN", bytes(task.get("slaveTWC") or b""))
        if task["cmd"] == "vehicleIsDefinitelyHome":
            # Per VIN, so two TWCs' cars are both checked.
            return ("vehicleIsDefinitelyHome", task.get("vin"))
        return task["cmd"]

    def doneBackgroundTask(self, task):
        # Delete task['cmd'] from backgroundTasksCmds such that
        # queue_background_task() can queue another task['cmd'] in the future.
        if "cmd" in task:
            del self.backgroundTasksCmds[self._taskKey(task)]

        # task_done() must be called to let the queue know the task is finished.
        # backgroundTasksQueue.join() can then be used to block until all tasks
        # in the queue are done.
        self.backgroundTasksQueue.task_done()

    def getAllowedFlex(self):
        return self.allowed_flex

    def getBackgroundTask(self):
        result = None

        while result is None:
            # Insert any delayed tasks. Taken under the tasks lock (the serial
            # thread adds to the list) and queued after it is released:
            # queue_background_task takes it too.
            due = []
            self.getBackgroundTasksLock()
            try:
                while (
                    self.backgroundTasksDelayed
                    and self.backgroundTasksDelayed[0][0] <= datetime.now()
                ):
                    due.append(self.backgroundTasksDelayed.pop(0)[-1])
            finally:
                self.releaseBackgroundTasksLock()
            for task in due:
                self.queue_background_task(task)

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

    def getHourResumeTrackGreenEnergy(self):
        return self.settings.get("hourResumeTrackGreenEnergy", -1)

    def getMasterTWCID(self):
        # This is called when TWCManager is in Slave mode, to track the
        # master's TWCID
        return self.masterTWCID

    def getkWhDelivered(self):
        return self.settings["kWhDelivered"]

    def getMaxAmpsToDivideAmongSlaves(self):
        if self.maxAmpsToDivideAmongSlaves > self.limitAmpsToDivideAmongSlaves:
            return self.limitAmpsToDivideAmongSlaves
        elif self.maxAmpsToDivideAmongSlaves > 0:
            return self.maxAmpsToDivideAmongSlaves
        else:
            return 0

    def getModuleByName(self, name):
        module = self.modules.get(name)
        if not module:
            return None

        ref = module.get("ref")

        enabled_fn = getattr(ref, "enabled", None)

        if callable(enabled_fn):
            if not enabled_fn():
                return None

        # If no enabled() method, assume module is usable
        return ref

    def updateChargeAtHome(self):
        # checkCharge: each vehicle module with a provider not suppressed
        # for reads refreshes its cars at home, once (TeslaAPI's also loads
        # its vehicle list)
        order, seen = self.providerOrder, set()
        for p in order.providers():
            module = p.module
            if (
                module is None
                or id(module) in seen
                or order.suppressions.suppressed(p, READ_CHARGE)
                or not p.available(READ_CHARGE)
            ):
                continue
            seen.add(id(module))
            try:
                getattr(module, "updateChargeAtHome", lambda: None)()
            except Exception as e:
                logger.warning("%s charge refresh raised %s", p.name, type(e).__name__)

    def getModulesByType(self, type):
        matched = []
        for module in self.modules:
            modinfo = self.modules[module]
            if modinfo["type"] == type:
                matched.append({"name": module, "ref": modinfo["ref"]})
        return matched

    def getAllEVSEs(self) -> list:
        """Return all EVSEInstance objects from every registered EVSEController."""
        evses = []
        for module in self.getModulesByType("EVSEController"):
            evses.extend(module["ref"].allEVSEs)
        return evses

    def chargingPoints(self) -> list:
        """Every endpoint grouped into charging points by the car it serves."""
        from TWCManager.EVSEInstance.ChargingPoint import groupEndpoints

        return groupEndpoints(self, self.getAllEVSEs())

    def distributeEVSEPower(self) -> None:
        """Centralized watt-based power distribution across charging points.

        Replaces the per-slave fair-share amps calculation in
        receive_slave_heartbeat with a controller-agnostic algorithm that
        allocates per charging point: a TWC and the car it serves are one
        point, budgeted once.  After this method runs:
        - Gen2TWC._evseTargetAmps is set; the RS485 heartbeat then sends it.
          With a car member, ChargingPoint splits the rate by
          chargeRateControl and owns the car's amps.
        - Each Gen3 unit's own target is handed to Gen3TWCs.setUnitTarget.
        - A car on no TWC has desired amps (VehicleEndpoint.setTargetPower).

        A point not wanting power (an idle TWC) gets a target of 0 and counts
        nothing against the budget.  On the wire, TWCSlave offers it 0 when no
        car is charging (a Gen2 treats 0 as its hardware minimum) and its
        hardware minimum when another car charges with at least
        minAmpsPerTWC of power per car, so it can see a car plug in.

        Only runs when an EVSEController other than Gen2TWCs is registered;
        pure Gen2 deployments keep the existing per-slave heartbeat
        distribution.

        Ported from ngardiner/TWCManager#483 (MikeBishop).
        """
        controllers = self.getModulesByType("EVSEController")
        if not controllers:
            return

        # Only activate the centralized distributor when VehicleEndpoints is
        # present — otherwise the existing per-slave heartbeat logic is sufficient
        # and more battle-tested for pure Gen2 deployments.
        non_gen2 = [c for c in controllers if c["name"] != "Gen2TWCs"]
        if not non_gen2:
            return

        points = self.chargingPoints()
        wants_power = [p for p in points if p.wantsToCharge and not p.isReadOnly]

        # Total watts available from EMS/policy. Convert with the voltage the
        # TWCs measure (defaultVoltage only when none report it): the policy
        # derived these amps from watts with it, and Gen2TWC.setTargetPower()
        # converts back with it, so the TWC is offered the amps the policy chose.
        available_watts = self.convertAmpsToWatts(self.getMaxAmpsToDivideAmongSlaves())
        remaining_watts = available_watts
        offers = []

        if wants_power:
            # Per-controller remaining power budgets and point counts
            controller_budgets: dict = {}
            controller_counts: dict = {}
            for mod in controllers:
                name = mod["name"]
                controller_budgets[name] = mod["ref"].maxPower
                controller_counts[name] = sum(
                    1 for p in wants_power if name in p.controllers
                )

            # Fair-share distribution — sort ascending by maxPower so the most
            # constrained points are served first, reducing wasted capacity.
            pending = sorted(wants_power, key=lambda p: p.maxPower)

            # Iterate a copy: pending.remove() below would otherwise make the
            # loop skip every other point, leaving it with no target at all.
            for point in list(pending):
                fair_share = remaining_watts / len(pending)

                # Constrain by point capacity
                offer = min(fair_share, point.maxPower)

                # Constrain by controller budgets
                for ctrl_name in point.controllers:
                    count = controller_counts.get(ctrl_name, 0)
                    if count > 0 and ctrl_name in controller_budgets:
                        offer = min(offer, controller_budgets[ctrl_name] / count)

                # Enforce minimum: offer nothing rather than an unusably small
                # amount
                if 0 < offer < point.minPower:
                    offer = 0
                if point.maxPower < point.minPower <= fair_share:
                    self._noteRefusedPoint(point, fair_share)

                point.setTargetPower(offer)
                offers.append((point.ID, offer))

                # Update accounting
                remaining_watts -= offer
                for ctrl_name in point.controllers:
                    if ctrl_name in controller_budgets:
                        controller_budgets[ctrl_name] -= offer
                    if ctrl_name in controller_counts:
                        controller_counts[ctrl_name] = max(
                            0, controller_counts[ctrl_name] - 1
                        )
                pending.remove(point)

        # Points that don't want power get a target of 0. For an idle Gen2 TWC
        # that 0 goes out as its hardware minimum (the TWC treats an offer of 0
        # as its minimum), which it needs to see a car plug in. The minimum is
        # deliberately never counted against or gated on any budget: the
        # configured limits are already derated, and the next cycle rebalances.
        for point in points:
            if point in wants_power or point.isReadOnly:
                continue
            point.setTargetPower(0)
            offers.append((point.ID, 0))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "distributeEVSEPower: %d W over %s",
                available_watts,
                ", ".join("%s=%d W" % (pid, watts) for pid, watts in offers)
                or "no points",
            )

    # When each refused point was last logged
    _refusedPointLogged = {}

    def _noteRefusedPoint(self, point, available: float) -> None:
        # Power was there for the point's minimum, but its own members' limits
        # left it nothing. With a plugged, named car that is a missed charge,
        # so it is said at INFO, at most hourly per point.
        now = time.time()
        if not point.currentVIN or point.occupied is not True:
            return
        if now - self._refusedPointLogged.get(point.ID, 0) < 3600:
            return
        self._refusedPointLogged[point.ID] = now
        logger.info(
            "%s: %s is plugged in, but its limits leave it none of %d W",
            point.ID,
            point.currentVIN,
            available,
        )

    def getInterfaceModule(self):
        return self.getModulesByType("Interface")[0]["ref"]

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

    def getSlaveLifetimekWh(self):
        # This function is called from a Scheduled Task
        # If it's been at least 1 minute, then query all known Slave TWCs
        # to determine their lifetime kWh and per-phase voltages
        now = time.time()
        if now >= self.lastkWhPoll + 60:
            for slaveTWC in self.getSlaveTWCs():
                self.getInterfaceModule().send(
                    bytearray(b"\xfb\xeb")
                    + self.TWCID
                    + slaveTWC.TWCID
                    + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
                )
            self.lastkWhPoll = now

    def getSlaveSign(self):
        return self.slaveSign

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
            "startingMinute": (
                int(data["scheduledChargingStartHour"] * 60)
                if data["scheduledChargingStartHour"] >= 0
                else -1
            ),
            "endingMinute": (
                int(data["scheduledChargingEndHour"] * 60)
                if data["scheduledChargingEndHour"] >= 0
                else -1
            ),
            "monday": (scheduledChargingDays & 1) == 1,
            "tuesday": (scheduledChargingDays & 2) == 2,
            "wednesday": (scheduledChargingDays & 4) == 4,
            "thursday": (scheduledChargingDays & 8) == 8,
            "friday": (scheduledChargingDays & 16) == 16,
            "saturday": (scheduledChargingDays & 32) == 32,
            "sunday": (scheduledChargingDays & 64) == 64,
            "flexStartEnabled": self.getScheduledAmpsFlexStart(),
            "flexStartingMinute": (
                int(scheduledFlexTime[0] * 60) if scheduledFlexTime[0] >= 0 else -1
            ),
            "flexEndingMinute": (
                int(scheduledFlexTime[1] * 60) if scheduledFlexTime[1] >= 0 else -1
            ),
            "flexMonday": (scheduledFlexTime[2] & 1) == 1,
            "flexTuesday": (scheduledFlexTime[2] & 2) == 2,
            "flexWednesday": (scheduledFlexTime[2] & 4) == 4,
            "flexThursday": (scheduledFlexTime[2] & 8) == 8,
            "flexFriday": (scheduledFlexTime[2] & 16) == 16,
            "flexSaturday": (scheduledFlexTime[2] & 32) == 32,
            "flexSunday": (scheduledFlexTime[2] & 64) == 64,
            "flexBatterySize": self.getScheduledAmpsBatterySize(),
        }
        if self.settings["chargeNowTimeEnd"] > 0:
            chargingEndDTM = datetime.fromtimestamp(self.settings["chargeNowTimeEnd"])
            if chargingEndDTM > datetime.now():
                diff = chargingEndDTM - datetime.now()
                diffMinutes = int(round(diff.total_seconds() / 60))
                data["chargeNowTimeEnd"] = datetime.timestamp(chargingEndDTM)
        if self.settings["chargeNowAmps"] > 0:
            data["chargeNowAmps"] = self.settings["chargeNowAmps"]
        return data

    def getSpikeAmps(self):
        return self.spikeAmpsToCancel6ALimit

    def getTimeLastTx(self):
        return self.getInterfaceModule().timeLastTx

    def getTWCbyVIN(self, vin):
        twc = None
        for slaveTWC in self.getSlaveTWCs():
            if slaveTWC.currentVIN == vin:
                twc = slaveTWC
        return twc

    def getVehicleVIN(self, slaveID, part):
        prefixByte = None
        if int(part) == 0:
            prefixByte = bytearray(b"\xfb\xee")
        if int(part) == 1:
            prefixByte = bytearray(b"\xfb\xef")
        if int(part) == 2:
            prefixByte = bytearray(b"\xfb\xf1")

        if prefixByte:
            self.getInterfaceModule().send(
                prefixByte
                + self.TWCID
                + slaveID
                + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
            )

    def deleteSlaveTWC(self, deleteSlaveID):
        for i in range(0, len(self.slaveTWCRoundRobin)):
            if self.slaveTWCRoundRobin[i].TWCID == deleteSlaveID:
                del self.slaveTWCRoundRobin[i]
                break
        try:
            del self.slaveTWCs[deleteSlaveID]
        except KeyError:
            pass
        # A retired TWC's plug session (its anonymous car) ends with it.
        try:
            twcid = "%02X%02X" % (deleteSlaveID[0], deleteSlaveID[1])
            self.vehicleRegistry.endPlugSession(twcid)
        except Exception as e:
            logger.warning("Vehicle registry plug session update failed: %s", e)

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

    def getConsumptionAmps(self):
        consumptionAmpsVal = 0

        for key in self.consumptionAmpsValues:
            consumptionAmpsVal += float(self.consumptionAmpsValues[key])

        if consumptionAmpsVal < 0:
            consumptionAmpsVal = 0

        return float(consumptionAmpsVal)

    def getFakeTWCID(self):
        return self.TWCID

    def getGeneration(self):
        generationVal = 0

        # Currently, our only logic is to add all of the values together
        for key in self.generationValues:
            generationVal += float(self.generationValues[key])

        if generationVal < 0:
            generationVal = 0

        offset = self.getConsumptionOffset()
        if offset < 0:
            generationVal -= offset

        return float(generationVal)

    def getGenerationOffset(self):
        # Returns the number of watts to subtract from the solar generation stats
        # This is consumption + charger load if subtractChargerLoad is enabled
        # Or simply consumption if subtractChargerLoad is disabled
        generationOffset = self.getConsumption()
        if self.subtractChargerLoad:
            generationOffset -= self.getChargerLoad()
        # Allow negative offset when EMS reports grid delivery instead of
        # generation. This means the offset increases the total generation.
        if generationOffset < 0 and not self.treatGenerationAsGridDelivery:
            generationOffset = 0
        return float(generationOffset)

    def getHomeLatLon(self):
        # Returns Lat/Lon coordinates to check if car location is
        # at home
        latlon = [10000, 10000]
        latlon[0] = float(self.settings.get("homeLat", 10000))
        latlon[1] = float(self.settings.get("homeLon", 10000))
        return latlon

    def getMasterHeartbeatOverride(self):
        return self.overrideMasterHeartbeatData

    def getMaxAmpsForTargetGridUsage(self, targetA=0):
        # Calculate our current generation and consumption in amps
        generationA = float(self.convertWattsToAmps(self.getGeneration()))
        consumptionA = float(self.getConsumptionAmps())

        # Use consumption on all phases when the target is 0 or consumptionA
        # is not available
        if targetA == 0 or not consumptionA:
            consumptionA = float(self.convertWattsToAmps(self.getConsumption()))

        # When subtractChargerLoad is enabled, remove the charger's own draw
        # from consumptionA here (incremental step), consistent with what
        # getGenerationOffset() does for the de-novo step below.  Without this,
        # the spike-to-cancel-6A-limit temporarily drives consumptionA far above
        # generationA, making newOffer go deeply negative and clamping the result
        # to 0 A even though net available power is still positive.
        if self.subtractChargerLoad:
            chargerA = float(self.convertWattsToAmps(self.getChargerLoad()))
            consumptionA = max(0.0, consumptionA - chargerA)

        availableA = generationA + targetA - consumptionA

        # Calculate what we should offer to match the target (0 for green)
        #
        # The current offered shouldn't increase more than / must
        # decrease at least the current gap between available power and
        # consumption.

        currentOffer = max(
            int(self.getMaxAmpsToDivideAmongSlaves()),
            self.num_cars_charging_now() * self.config["config"]["minAmpsPerTWC"],
        )
        newOffer = currentOffer + availableA

        # This is the *de novo* calculation of how much we can offer
        generationOffsetA = self.convertWattsToAmps(self.getGenerationOffset())
        availableA = float(generationA + targetA - generationOffsetA)

        # Offer the smaller of the two, but not less than zero.
        amps = max(min(newOffer, availableA / self.getRealPowerFactor(availableA)), 0)
        return round(amps, 2)

    def getSlaveByID(self, twcid):
        return self.slaveTWCs[twcid]

    def getSlaveTWCID(self, twc):
        return self.slaveTWCRoundRobin[twc].TWCID

    def getSlaveTWC(self, id):
        return self.slaveTWCRoundRobin[id]

    def getSlaveTWCs(self):
        # Returns a list of all Slave TWCs
        return self.slaveTWCRoundRobin

    def getTotalAmpsInUse(self):
        # Returns the number of amps currently in use by all TWCs
        totalAmps = 0
        for slaveTWC in self.getSlaveTWCs():
            totalAmps += slaveTWC.reportedAmpsActual

        logger.debug("Total amps all slaves are using: " + str(totalAmps))
        return totalAmps

    def getVoltageMeasurement(self):
        slavesWithVoltage = [
            slave
            for slave in self.getSlaveTWCs()
            if (slave.voltsPhaseA > 0 or slave.voltsPhaseB > 0 or slave.voltsPhaseC > 0)
        ]
        if len(slavesWithVoltage) == 0:
            # No slaves support returning voltage
            return (
                self.config["config"].get("defaultVoltage", 240),
                self.config["config"].get("numberOfPhases", 1),
            )

        total = 0
        phases = 0

        # Detect number of active phases
        for slave in slavesWithVoltage:
            localPhases = 0
            if slave.voltsPhaseA:
                localPhases += 1
            if slave.voltsPhaseB:
                localPhases += 1
            if slave.voltsPhaseC:
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
                for slave in slavesWithVoltage
            ]
        )

        return (total / (phases * len(slavesWithVoltage)), phases)

    def hex_str(self, s: str):
        return " ".join("{:02X}".format(ord(c)) for c in s)

    def hex_str(self, ba: bytearray):
        return " ".join("{:02X}".format(c) for c in ba)

    def loadSettings(self):
        # Loads the volatile application settings (such as charger timings,
        # API credentials, etc) from a JSON file

        # Step 1 - Load settings from JSON file
        fileName = self.config["config"]["settingsPath"] + "/settings.json"
        backupFileName = fileName + ".backup"

        if not os.path.exists(fileName):
            # Initialize with class defaults if file doesn't exist
            with self.settingsLock:
                self.settings = {
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
            return

        # Read and parse the file(s) into a local with no lock held; the
        # result is assigned under settingsLock below.
        loaded = None
        loadSuccess = False
        with open(fileName, "r") as inconfig:
            try:
                loaded = json.load(inconfig)
                loadSuccess = True
            except Exception as e:
                logger.info(
                    "There was an exception whilst loading settings file " + fileName
                )
                logger.log(logging.DEBUG2, str(e))

        # If main file failed to load, try the backup
        if not loadSuccess and os.path.exists(backupFileName):
            logger.info(
                "Attempting to restore settings from backup file: " + backupFileName
            )
            try:
                with open(backupFileName, "r") as inconfig:
                    loaded = json.load(inconfig)
                    loadSuccess = True
                logger.info("Successfully restored settings from backup file")
                # Restore the backup to the main file
                try:
                    import shutil

                    shutil.copy2(backupFileName, fileName)
                    logger.info("Restored backup to main settings file")
                except Exception as restore_error:
                    logger.info(
                        f"Could not restore backup to main file: {restore_error}"
                    )
            except Exception as backup_error:
                logger.info("Failed to load backup settings file: " + str(backup_error))

        # If both files failed, show helpful message
        if not loadSuccess:
            logger.info(
                "Some data may have been loaded. This may be because the file is being created for the first time."
            )
            logger.info(
                "It may also be because you are upgrading from a TWCManager version prior to v1.1.4, which used the old settings file format."
            )
            logger.info(
                "If this is the case, you may need to locate the old config file and migrate some settings manually."
            )

        # Step 1b - Merge loaded settings with defaults to ensure all required keys exist
        defaults = {
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
        with self.settingsLock:
            if loaded is not None:
                self.settings = loaded
            for key, value in defaults.items():
                if key not in self.settings:
                    self.settings[key] = value
            registryBlob = self.settings.get("vehicleRegistry")
        # Migrates the legacy chargeLimits key; dict work under the lock only
        self.heldLimits.load()
        # load() tolerates a missing or corrupt blob
        self.vehicleRegistry.load(registryBlob)

        # Step 2 - Send settings to other modules
        # Migration and restore run whether or not token sync is on. The
        # sync source's startup sync has already run (every module is
        # registered by now); restoreSlot keeps its token over a saved
        # synced one, while a saved pasted or login token replaces it.
        carapi = self.getModuleByName("TeslaAPI")
        # Configured, not tokenSyncEnabled(): sync turns itself off when
        # the TeslaMate database is unreachable at startup, and a token it
        # saved must still count as synced.
        teslamateConfig = self.config.get("vehicle", {}).get("TeslaMate", {}) or {}
        syncConfigured = bool(teslamateConfig.get("syncTokens", False))
        if carapi.migrateSavedTokens(self.settings, syncConfigured):
            # Writes the migrated keys, so a dropped TeslaMate refresh token
            # leaves settings.json too.
            self.queue_background_task({"cmd": "saveSettings"})
        carapi.restoreSlot(
            carapi.fleetSlot,
            self.settings.get("carApiBearerToken", ""),
            self.settings.get("carApiRefreshToken", ""),
            self.settings.get("carApiTokenExpireTime", ""),
            self.settings.get("carApiTokenSource") or "pasted",
        )
        carapi.restoreSlot(
            carapi.ownerSlot,
            self.settings.get("ownerApiBearerToken", ""),
            self.settings.get("ownerApiRefreshToken", ""),
            self.settings.get("ownerApiTokenExpireTime", ""),
            self.settings.get("ownerApiTokenSource") or "pasted",
        )

        # We start with vehicles, but this should be templated in future for all modules to both remove the above logic,
        # and to push settings logic out to the individual modules
        mods = self.getModulesByType("Vehicle")
        for mod in mods:
            # Optional: only TeslaBLE has settings to re-read
            if hasattr(mod["ref"], "updateSettings"):
                mod["ref"].updateSettings()

        # If particular details are missing from the Settings dict, create them
        with self.settingsLock:
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

    def master_id_conflict(self):
        # We're playing fake slave, and we got a message from a master with our TWCID.
        # By convention, as a slave we must change our TWCID because a master will not.
        self.TWCID[0] = random.randint(0, 0xFF)
        self.TWCID[1] = random.randint(0, 0xFF)

        # Real slaves change their sign during a conflict, so we do too.
        self.slaveSign[0] = random.randint(0, 0xFF)

        logger.info(
            "Master's TWCID matches our fake slave's TWCID.  "
            "Picked new random TWCID %02X%02X with sign %02X"
            % (self.TWCID[0], self.TWCID[1], self.slaveSign[0])
        )

    def newSlave(self, newSlaveID, maxAmps):
        try:
            slaveTWC = self.slaveTWCs[newSlaveID]
            # We didn't get KeyError exception, so this slave is already in
            # slaveTWCs and we can simply return it.
            return slaveTWC
        except KeyError:
            pass

        slaveTWC = TWCSlave(newSlaveID, maxAmps, self.config, self)
        self.slaveTWCs[newSlaveID] = slaveTWC
        self.addSlaveTWC(slaveTWC)

        if self.countSlaveTWC() > 3:
            logger.info(
                "WARNING: More than 3 slave TWCs seen on network. Dropping oldest: "
                + self.hex_str(self.getSlaveTWCID(0))
                + "."
            )
            self.deleteSlaveTWC(self.getSlaveTWCID(0))

        return slaveTWC

    def num_cars_charging_now(self):
        carsCharging = 0
        for slaveTWC in self.getSlaveTWCs():
            if slaveTWC.reportedAmpsActual >= 1.0:
                # Amps are flowing — cancel any pending session-end debounce
                slaveTWC.chargingDroppedBelowThresholdTime = 0

                if slaveTWC.isCharging == 0:
                    # We have detected that a vehicle has started charging on this Slave TWC
                    # Attempt to request the vehicle's VIN
                    slaveTWC.isCharging = 1
                    slaveTWC.lastChargingStart = time.time()
                    self.queue_background_task(
                        {
                            "cmd": "getVehicleVIN",
                            "slaveTWC": slaveTWC.TWCID,
                            "vinPart": 0,
                        }
                    )

                    # Record our VIN query timestamp
                    slaveTWC.lastVINQuery = time.time()
                    slaveTWC.vinQueryAttempt = 1

                    # Record start of current charging session
                    self.recordVehicleSessionStart(slaveTWC)
                    self._plugSession(slaveTWC, True)
            else:
                if slaveTWC.isCharging == 1:
                    # Amps have dropped below threshold. Start debounce timer to
                    # avoid bouncing session events during charge negotiation at
                    # startup (car can briefly drop to 0A before settling).
                    if slaveTWC.chargingDroppedBelowThresholdTime == 0:
                        slaveTWC.chargingDroppedBelowThresholdTime = time.time()

                    if time.time() - slaveTWC.chargingDroppedBelowThresholdTime >= 5:
                        # Amps have been below threshold for 5+ seconds — confirmed stop
                        slaveTWC.chargingDroppedBelowThresholdTime = 0

                        # Clear the VIN details for this slave and move the last
                        # vehicle's VIN to lastVIN
                        slaveTWC.VINData = ["", "", ""]
                        if slaveTWC.currentVIN:
                            slaveTWC.lastVIN = slaveTWC.currentVIN
                        slaveTWC.currentVIN = ""
                        self.updateVINStatus()

                        # Stop querying for Vehicle VIN
                        slaveTWC.lastVINQuery = 0
                        slaveTWC.vinQueryAttempt = 0

                        # Close off the current charging session
                        self.recordVehicleSessionEnd(slaveTWC)
                        self._plugSession(slaveTWC, False)
                        # The next session lifts the car's own rate again
                        slaveTWC._resetVehicleRateRaised()
                        slaveTWC.isCharging = 0
                        slaveTWC.lastChargingStart = 0
                elif slaveTWC.reportedState in slaveTWC.PLUGGED_STATES or (
                    slaveTWC.reportedState == 0 and slaveTWC.pluggedCars()[0]
                ):
                    # A car drawing nothing (asleep after a restart) is still
                    # plugged in: unidentified, it is an anonymous session.
                    # At 00 (an empty TWC says it too) only if a free source
                    # shows an at-home car plugged in
                    if slaveTWC.vehicleKey is None:
                        self._plugSession(slaveTWC, True)
                elif slaveTWC.reportedState == 0 and isAnonKey(slaveTWC.vehicleKey):
                    # 00 and no car known plugged in
                    self._plugSession(slaveTWC, False)
            carsCharging += slaveTWC.isCharging
            for module in self.getModulesByType("Status"):
                module["ref"].setStatus(
                    slaveTWC.TWCID,
                    "cars_charging",
                    "carsCharging",
                    slaveTWC.isCharging,
                    "",
                )
        logger.debug("Number of cars charging now: " + str(carsCharging))

        if carsCharging == 0:
            self.stopTimeout = datetime.max

        return carsCharging

    def queue_background_task(self, task, delay=0):
        if delay > 0:
            self.getBackgroundTasksLock()
            try:
                bisect.insort(
                    self.backgroundTasksDelayed,
                    (
                        datetime.now() + timedelta(seconds=delay),
                        next(self._delaySeq),
                        task,
                    ),
                )
            finally:
                self.releaseBackgroundTasksLock()
            return

        self.getBackgroundTasksLock()
        try:
            key = self._taskKey(task)
            if key in self.backgroundTasksCmds:
                # Some tasks are queued every heartbeat. To avoid wasting
                # memory, don't queue two of the same task.
                self.backgroundTasksCmds[key].update(task)
                return

            # Insert task['cmd'] in backgroundTasksCmds to prevent queuing another
            # task['cmd'] till we've finished handling this one.
            self.backgroundTasksCmds[key] = task
        finally:
            self.releaseBackgroundTasksLock()

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

    def _plugSession(self, slaveTWC, begin):
        # Serial thread: registry calls are short dict work; never let one
        # raise into the RS-485 loop.
        try:
            twcid = "%02X%02X" % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
            if begin:
                self.vehicleRegistry.beginPlugSession(twcid)
            else:
                self.vehicleRegistry.endPlugSession(twcid)
        except Exception as e:
            logger.warning("Vehicle registry plug session update failed: %s", e)

    def recordVehicleVIN(self, slaveTWC):
        twcid = "%02X%02X" % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
        # A VIN no known vehicle has is confirmed by a second matching read
        # before it creates a vehicle or a session record.
        try:
            status = self.vehicleRegistry.observeTwcVin(twcid, slaveTWC.currentVIN)
            if status == "pending":
                self.queue_background_task(
                    {"cmd": "getVehicleVIN", "slaveTWC": slaveTWC.TWCID, "vinPart": 0},
                    delay=5,
                )
                # retryVINQuery re-asks on the first read's budget if this
                # confirmation read is lost.
                slaveTWC.lastVINQuery = time.time()
                slaveTWC.vinQueryAttempt = 1
                return
        except Exception as e:
            logger.warning("Vehicle registry VIN update failed: %s", e)
            return
        if status != "confirmed":
            return

        vin = slaveTWC.currentVIN
        with self.settingsLock:
            # Record Slave TWC ID as being capable of reporting VINs, if it is
            # not already.
            if not self.settings.get("SlaveTWCs", None):
                self.settings["SlaveTWCs"] = {}
            if not self.settings["SlaveTWCs"].get(twcid, None):
                self.settings["SlaveTWCs"][twcid] = {}
            if not self.settings["SlaveTWCs"][twcid].get("supportsVINQuery", 0):
                self.settings["SlaveTWCs"][twcid]["supportsVINQuery"] = 1

            # Increment sessions counter for this VIN in persistent settings file
            if not self.settings.get("Vehicles", None):
                self.settings["Vehicles"] = {}
            if not self.settings["Vehicles"].get(vin, None):
                self.settings["Vehicles"][vin] = {
                    "chargeSessions": 1,
                    "startkWh": slaveTWC.lifetimekWh,
                    "totalkWh": 0,
                }
            else:
                entry = self.settings["Vehicles"][vin]
                entry["chargeSessions"] = entry.get("chargeSessions", 0) + 1
                entry["startkWh"] = slaveTWC.lifetimekWh
                entry.setdefault("totalkWh", 0)
        # Saved outside the lock: the save serialises under it itself.
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

        # If vehicle is charging, it's at home no matter what we previously
        # thought. The check reads the vehicle (BLE, HTTP), so it runs on the
        # background thread, not this (serial) one.
        self.queue_background_task({"cmd": "vehicleIsDefinitelyHome", "vin": vin})

    def vehicleIsDefinitelyHome(self, vin):
        # Background thread: may do vehicle I/O.
        tesla_api = self.getModuleByName("TeslaAPI")
        if tesla_api:
            tesla_api.vehicleIsDefinitelyHome(vin)

    def releaseBackgroundTasksLock(self):
        self.backgroundTasksLock.release()

    def releaseModule(self, path, module):
        # Removes a module from the modules dict
        # This ensures we do not continue to call the module if it is
        # inoperable
        deleted = False
        self.releasedModules.append(module)
        if self.modules.get(module, None):
            del self.modules[module]
            deleted = True

        # Remove from python sys.modules as well
        fullname = path + "." + module
        if modules.get(fullname, None):
            del modules[fullname]
            deleted = True

        if deleted:
            logger.log(
                logging.INFO7, "Released module %s", module, extra={"colored": "red"}
            )
        else:
            logger.warning("Tried to released module %s that was not loaded", module)

    def resetChargeNowAmps(self):
        # Sets chargeNowAmps back to zero, so we follow the green energy
        # tracking again
        with self.settingsLock:
            self.settings["chargeNowAmps"] = 0
            self.settings["chargeNowTimeEnd"] = 0
        self.queue_background_task({"cmd": "saveSettings"})

    def retryVINQuery(self):
        # For each Slave TWC, check if it's been more than 60 seconds since the last
        # VIN query without a VIN. If so, query again.
        for slaveTWC in self.getSlaveTWCs():
            if slaveTWC.isCharging == 1:
                if (
                    slaveTWC.lastVINQuery > 0
                    and slaveTWC.vinQueryAttempt < 6
                    and (not slaveTWC.currentVIN or self._vinPending(slaveTWC))
                ):
                    if (time.time() - slaveTWC.lastVINQuery) >= 60:
                        self.queue_background_task(
                            {
                                "cmd": "getVehicleVIN",
                                "slaveTWC": slaveTWC.TWCID,
                                "vinPart": 0,
                            }
                        )
                        slaveTWC.vinQueryAttempt += 1
                        slaveTWC.lastVINQuery = time.time()
            else:
                slaveTWC.lastVINQuery = 0

    def _vinPending(self, slaveTWC):
        try:
            twcid = "%02X%02X" % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
            return self.vehicleRegistry.isPending(twcid)
        except Exception as e:
            logger.warning("Vehicle registry pending check failed: %s", e)
            return False

    def expireVehicles(self):
        # Forget unclaimed, unasserted vehicles unseen past the forget window
        expired = self.vehicleRegistry.expire()
        if expired:
            self.queue_background_task({"cmd": "saveSettings"})
        return expired

    def saveSettings(self):
        # Saves the volatile application settings (such as charger timings,
        # API credentials, etc) to a JSON file
        fileName = self.config["config"]["settingsPath"] + "/settings.json"
        tempFileName = fileName + ".tmp"
        backupFileName = fileName + ".backup"

        # Step 1 - Merge any config from other modules
        carapi = self.getModuleByName("TeslaAPI")
        with self.settingsLock:
            self.settings["carApiBearerToken"] = carapi.fleetSlot.accessToken
            self.settings["carApiRefreshToken"] = carapi.fleetSlot.refreshToken
            self.settings["carApiTokenExpireTime"] = carapi.fleetSlot.expireTime
            self.settings["ownerApiBearerToken"] = carapi.ownerSlot.accessToken
            self.settings["ownerApiRefreshToken"] = carapi.ownerSlot.refreshToken
            self.settings["ownerApiTokenExpireTime"] = carapi.ownerSlot.expireTime
            self.settings["carApiTokenSource"] = carapi.fleetSlot.source
            self.settings["ownerApiTokenSource"] = carapi.ownerSlot.source
            self.settings["vehicleRegistry"] = self.vehicleRegistry.toJSON()
            try:
                # Keep default arguments (C encoder): adding indent/default/cls
                # would reintroduce the concurrent-mutation race.
                payload = json.dumps(self.settings)
            except TypeError as e:
                logger.info("Exception raised while attempting to save settings file:")
                logger.info(str(e))
                self.lastSaveFailed = 1
                return

        # Step 2 - Write the serialised settings to a JSON file atomically,
        # outside the lock so file I/O never blocks settings writers
        # Use temp file + rename to ensure atomic write and prevent corruption
        try:
            # Write to temp file first
            with open(tempFileName, "w") as outconfig:
                outconfig.write(payload)
                outconfig.flush()
                os.fsync(outconfig.fileno())

            # Create backup of existing file if it exists
            if os.path.exists(fileName):
                try:
                    os.replace(fileName, backupFileName)
                except OSError:
                    # If backup creation fails, continue anyway
                    pass

            # Atomically move temp to final location
            os.replace(tempFileName, fileName)

            self.lastSaveFailed = 0
        except PermissionError as e:
            logger.info(
                "Permission Denied trying to save to settings.json. Please check the permissions of the file and try again."
            )
            self.lastSaveFailed = 1
            # Clean up temp file if it exists
            if os.path.exists(tempFileName):
                try:
                    os.remove(tempFileName)
                except:
                    pass
        except (TypeError, OSError, IOError) as e:
            logger.info("Exception raised while attempting to save settings file:")
            logger.info(str(e))
            self.lastSaveFailed = 1
            # Clean up temp file if it exists
            if os.path.exists(tempFileName):
                try:
                    os.remove(tempFileName)
                except:
                    pass

    def send_master_linkready1(self):
        logger.log(logging.INFO8, "Send master linkready1")

        # When master is powered on or reset, it sends 5 to 7 copies of this
        # linkready1 message followed by 5 copies of linkready2 (I've never seen
        # more or less than 5 of linkready2).
        #
        # This linkready1 message advertises master's TWCID to other slaves on the
        # network.
        # If a slave happens to have the same id as master, it will pick a new
        # random TWCID. Other than that, slaves don't seem to respond to linkready1.

        # linkready1 and linkready2 are identical except FC E1 is replaced by FB E2
        # in bytes 2-3. Both messages will cause a slave to pick a new id if the
        # slave's id conflicts with master.
        # If a slave stops sending heartbeats for awhile, master may send a series
        # of linkready1 and linkready2 messages in seemingly random order, which
        # means they don't indicate any sort of startup state.

        # linkready1 is not sent again after boot/reset unless a slave sends its
        # linkready message.
        # At that point, linkready1 message may start sending every 1-5 seconds, or
        # it may not be sent at all.
        # Behaviors I've seen:
        #   Not sent at all as long as slave keeps responding to heartbeat messages
        #   right from the start.
        #   If slave stops responding, then re-appears, linkready1 gets sent
        #   frequently.

        # One other possible purpose of linkready1 and/or linkready2 is to trigger
        # an error condition if two TWCs on the network transmit those messages.
        # That means two TWCs have rotary switches setting them to master mode and
        # they will both flash their red LED 4 times with top green light on if that
        # happens.

        # Also note that linkready1 starts with FC E1 which is similar to the FC D1
        # message that masters send out every 4 hours when idle. Oddly, the FC D1
        # message contains all zeros instead of the master's id, so it seems
        # pointless.

        # I also don't understand the purpose of having both linkready1 and
        # linkready2 since only two or more linkready2 will provoke a response from
        # a slave regardless of whether linkready1 was sent previously. Firmware
        # trace shows that slaves do something somewhat complex when they receive
        # linkready1 but I haven't been curious enough to try to understand what
        # they're doing. Tests show neither linkready1 or 2 are necessary. Slaves
        # send slave linkready every 10 seconds whether or not they got master
        # linkready1/2 and if a master sees slave linkready, it will start sending
        # the slave master heartbeat once per second and the two are then connected.
        self.getInterfaceModule().send(
            bytearray(b"\xfc\xe1")
            + self.TWCID
            + self.masterSign
            + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        )

    def send_master_linkready2(self):
        logger.log(logging.INFO8, "Send master linkready2")

        # This linkready2 message is also sent 5 times when master is booted/reset
        # and then not sent again if no other TWCs are heard from on the network.
        # If the master has ever seen a slave on the network, linkready2 is sent at
        # long intervals.
        # Slaves always ignore the first linkready2, but respond to the second
        # linkready2 around 0.2s later by sending five slave linkready messages.
        #
        # It may be that this linkready2 message that sends FB E2 and the master
        # heartbeat that sends fb e0 message are really the same, (same FB byte
        # which I think is message type) except the E0 version includes the TWC ID
        # of the slave the message is intended for whereas the E2 version has no
        # recipient TWC ID.
        #
        # Once a master starts sending heartbeat messages to a slave, it
        # no longer sends the global linkready2 message (or if it does,
        # they're quite rare so I haven't seen them).
        self.getInterfaceModule().send(
            bytearray(b"\xfb\xe2")
            + self.TWCID
            + self.masterSign
            + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        )

    def send_slave_linkready(self):
        # In the message below, \x1F\x40 (hex 0x1f40 or 8000 in base 10) refers to
        # this being a max 80.00Amp charger model.
        # EU chargers are 32A and send 0x0c80 (3200 in base 10).
        #
        # I accidentally changed \x1f\x40 to \x2e\x69 at one point, which makes the
        # master TWC immediately start blinking its red LED 6 times with top green
        # LED on. Manual says this means "The networked Wall Connectors have
        # different maximum current capabilities".
        msg = (
            bytearray(b"\xfd\xe2")
            + self.TWCID
            + self.slaveSign
            + bytearray(b"\x1f\x40\x00\x00\x00\x00\x00\x00")
        )
        if self.protocolVersion == 2:
            msg += bytearray(b"\x00\x00")

        self.getInterfaceModule().send(msg)

    def sendStartCommand(self):
        # This function will loop through each of the Slave TWCs, and send them the start command.
        for slaveTWC in self.getSlaveTWCs():
            self.getInterfaceModule().send(
                bytearray(b"\xfc\xb1")
                + self.TWCID
                + slaveTWC.TWCID
                + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00\x00")
            )

    def sendStopCommand(self, subTWC=None):
        # This function will loop through each of the Slave TWCs, and send them the stop command.
        # If the subTWC parameter is supplied, we only stop the specified TWC
        for slaveTWC in self.getSlaveTWCs():
            if (not subTWC) or (subTWC == slaveTWC.TWCID):
                self.getInterfaceModule().send(
                    bytearray(b"\xfc\xb2")
                    + self.TWCID
                    + slaveTWC.TWCID
                    + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00\x00")
                )

    def setPolicyLimit(self, limit, transition):
        # transition: a policy change, a decision that ends an adopted value
        self.policyLimit = limit
        applyPolicyLimit(self, limit, decision=transition)

    def setAllowedFlex(self, amps):
        self.allowed_flex = amps if amps >= 0 else 0

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
            with self.settingsLock:
                self.settings["chargeNowAmps"] = amps

    def setChargeNowTimeEnd(self, timeadd):
        with self.settingsLock:
            self.settings["chargeNowTimeEnd"] = time.time() + timeadd

    def setConsumption(self, source, value):
        # Accepts consumption values from one or more data sources
        # For now, this gives a sum value of all, but in future we could
        # average across sources perhaps, or do a primary/secondary priority
        self.consumptionValues[source] = value

    def setConsumptionAmps(self, source, value):
        self.consumptionAmpsValues[source] = value

    def setGeneration(self, source, value):
        # Some EMS modules report generation as a negative value (e.g. -5000W
        # means 5000W generated). Negate such values so the rest of the code
        # always works with positive generation watts (closes #442).
        if value < 0:
            value = -value
        self.generationValues[source] = value

    def setHomeLat(self, lat):
        with self.settingsLock:
            self.settings["homeLat"] = lat

    def setHomeLon(self, lon):
        with self.settingsLock:
            self.settings["homeLon"] = lon

    def setHourResumeTrackGreenEnergy(self, hour):
        with self.settingsLock:
            self.settings["hourResumeTrackGreenEnergy"] = hour

    def setMasterTWCID(self, twcid):
        # This is called when TWCManager is in Slave mode, to track the
        # master's TWCID
        self.masterTWCID = twcid

    def setLimitAmpsToDivideAmongSlaves(self, amps):
        # Use backgroundTasksLock to prevent changing limitAmpsToDivideAmongSlaves
        # if the main thread is in the middle of examining and later using
        # that value.
        self.getBackgroundTasksLock()

        if amps > self.config["config"]["wiringMaxAmpsAllTWCs"]:
            # Never tell the slaves
            # to draw more amps than the physical charger wiring can handle.
            amps = self.config["config"]["wiringMaxAmpsAllTWCs"]

        self.limitAmpsToDivideAmongSlaves = amps

        self.releaseBackgroundTasksLock()

    def setMaxAmpsToDivideAmongSlaves(self, amps):
        # Use backgroundTasksLock to prevent changing maxAmpsToDivideAmongSlaves
        # if the main thread is in the middle of examining and later using
        # that value.
        self.getBackgroundTasksLock()

        if amps > self.config["config"]["wiringMaxAmpsAllTWCs"]:
            # Never tell the slaves to draw more amps than the physical charger
            # wiring can handle.
            logger.error(
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
        with self.settingsLock:
            self.settings["nonScheduledAmpsMax"] = amps

    def setScheduledAmpsDaysBitmap(self, bitmap):
        with self.settingsLock:
            self.settings["scheduledAmpsDaysBitmap"] = bitmap

    def setScheduledAmpsBatterySize(self, batterySize):
        if batterySize > 40:
            with self.settingsLock:
                self.settings["scheduledAmpsBatterySize"] = batterySize

    def setScheduledAmpsMax(self, amps):
        with self.settingsLock:
            self.settings["scheduledAmpsMax"] = amps

    def setScheduledAmpsStartHour(self, hour):
        with self.settingsLock:
            self.settings["scheduledAmpsStartHour"] = hour

    def setScheduledAmpsEndHour(self, hour):
        with self.settingsLock:
            self.settings["scheduledAmpsEndHour"] = hour

    def setScheduledAmpsFlexStart(self, enabled):
        with self.settingsLock:
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

        for slave in self.getSlaveTWCs():
            avgCurrent += slave.historyAvgAmps
            slave.historyAvgAmps = 0
            slave.historyNumSamples = 0
        self.advanceHistorySnap()

        if avgCurrent > 0:
            periodTimestamp = snaptime - timedelta(minutes=5)

            with self.settingsLock:
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

    def startCarsCharging(self, vin=None):
        # Desired state, decided again each cycle: the reconciler sends it
        # once per our own write, a limit first. vin is a registry key (a VIN
        # or an anon: key); None is every present car
        self._wantCharging(vin, True)
        if int(self.settings.get("chargeStopMode", 1)) == 1:
            policy = self.getModuleByName("Policy")
            if policy:
                policy.clearOverride()

    def stopCarsCharging(self, vin=None):
        # The opposite of startCarsCharging. chargeStopMode picks how: 1 the
        # car (BLE, the Tesla API), 2 refusing to answer the slaves, 3 the
        # TWC stop command; 2 and 3 are the Gen2 provider's, which the
        # reconciler reaches once a free stop is not to be had
        self._wantCharging(vin, False)
        if int(self.settings.get("chargeStopMode", 1)) == 1:
            if self.stopTimeout == datetime.max:
                self.stopTimeout = datetime.now() + timedelta(seconds=10)
            elif datetime.now() > self.stopTimeout:
                policy = self.getModuleByName("Policy")
                if policy:
                    policy.overrideLimit()

    def stopRespondingToSlaves(self):
        # Every slave stops charging once we go quiet; resumes after a minute.
        # The expiry first: the serial thread reads both without the lock, and
        # a 0 seen with the old expiry would be reset at once (M1)
        with self.settingsLock:
            self.settings["respondToSlavesExpiry"] = time.time() + 60
            self.settings["respondToSlaves"] = 0

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
        for slaveTWC in self.getSlaveTWCs():
            if slaveTWC.TWCID == sender:
                slaveTWC.setLifetimekWh(kWh)
                slaveTWC.setVoltage(vPA, vPB, vPC)

    def updateVINStatus(self):
        # update current and last VIN IDs for each Slave to all Status modules
        for slaveTWC in self.getSlaveTWCs():
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
