# Tesla API Vehicle Controller
# Exposes EVSEInstances for Gen2 TWCs connected over serial

from TWCManager.EVSEInstance.TeslaAPIEVSE import TeslaAPIEVSE
import logging
import sys

logger = logging.getLogger("\u26FD API")


class TeslaAPIController:

    master = None
    config = {}
    configConfig = {}
    configAPI = {}
    status = False
    stopEvent = None
    thread = None
    name = "TeslaAPIController"

    def __init__(self, master):
        self.master = master

        self.config = master.config
        self.configConfig = self.config.get("config", {})
        self.configAPI = self.config.get("controller", {}).get(
            "TeslaAPIController", {}
        )

        if "enabled" in self.configAPI:
            self.status = self.configAPI["enabled"]
        else:
            # Backward-compatible default; assume enabled if API control is
            # configured for either legacy setting
            self.status = (
                int(self.master.settings.get("chargeStopMode", 1)) == 1
                or int(self.master.settings.get("chargeRateControl", 1)) == 2
            )

        # Unload if this module is disabled or misconfigured
        if not self.status:
            self.master.releaseModule("lib.TWCManager.EVSEController", "TeslaAPIController")
            return None

    def getCarsAtHome(self):
        for vehicle in self.master.getModuleByName("TeslaAPI").getCarApiVehicles():
            vehicle.update_location()
        return [
            vehicle
            for vehicle in self.master.getModuleByName("TeslaAPI").getCarApiVehicles()
            if vehicle.atHome
        ]

    @property
    def allEVSEs(self):
        return [TeslaAPIEVSE(vehicle, self, self.master) for vehicle in self.getCarsAtHome()]

    def startAllCharging(self):
        for evse in self.allEVSEs:
            evse.startCharging()

    def stopAllCharging(self):
        for evse in self.allEVSEs:
            evse.stopCharging()

    @property
    def maxPower(self):
        return sys.maxsize

    def stop(self):
        pass
