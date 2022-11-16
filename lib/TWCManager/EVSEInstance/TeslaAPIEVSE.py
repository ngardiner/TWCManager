class TeslaAPIEVSE:

    master = None
    vehicle = None
    controller = None
    config = None
    configConfig = None

    minAmps = 5

    def __init__(self, vehicle, controller, master):
        self.master = master
        self.vehicle = vehicle
        self.controller = controller

        self.config = master.config
        self.configConfig = self.config.get("config", {})

    @property
    def isReadOnly(self):
        return False

    @property
    def isLocal(self):
        return False

    @property
    def isCharging(self):
        return self.vehicle.chargingState == "Charging"

    @property
    def wantsToCharge(self):
        return self.isCharging or self.vehicle.chargingState == "Stopped"

    def convertAmpsToWatts(self, amps):
        return self.master.convertAmpsToWatts(
            amps, self.currentVoltage
        ) * self.master.getRealPowerFactor(amps)

    def convertWattsToAmps(self, watts):
        return self.master.convertWattsToAmps(watts, self.currentVoltage)

    @property
    def minPower(self):
        return self.convertAmpsToWatts(self.minAmps)

    @property
    def maxPower(self):
        self.vehicle.update_charge()
        return self.convertAmpsToWatts(self.vehicle.availableCurrent)

    @property
    def currentPower(self):
        return self.convertAmpsToWatts(self.currentAmps)

    @property
    def currentAmps(self):
        self.vehicle.update_charge()
        return self.vehicle.actualCurrent

    @property
    def currentVoltage(self):
        self.vehicle.update_charge()
        # Car will report ~2V when charging is not in progress
        voltage = (
            self.vehicle.voltage
            if self.vehicle.voltage > 90
            else self.configConfig.get("defaultVoltage", 240)
        )
        phases = (
            self.vehicle.phases
            if self.vehicle.phases
            else self.configConfig.get("numberOfPhases", 1)
        )

        return [
            voltage,
            voltage if phases > 1 else 0,
            voltage if phases > 2 else 0,
        ]

    @property
    def ID(self):
        return "Tesla-%s" % self.vehicle.ID

    @property
    def currentVIN(self):
        return self.vehicle.VIN

    @property
    def controllers(self):
        return [self.controller.name]

    def startCharging(self):
        self.master.queue_background_task(
            {"cmd": "charge", "charge": True, "vin": self.vehicle.VIN}
        )
        self.master.getModuleByName("Policy").clearOverride()

    def stopCharging(self):
        self.master.queue_background_task(
            {"cmd": "charge", "charge": False, "vin": self.vehicle.VIN}
        )

    def setTargetPower(self, power):
        desiredAmpsOffered = max([self.convertWattsToAmps(power), self.minAmps])

        self.master.getModuleByName("TeslaAPI").setChargeRate(
            int(desiredAmpsOffered), self.vehicle
        )

    def snapHistoryData(self):
        # Not sure this can be reliably implemented without requesting
        # charge data from the Tesla API after the fact.
        return 0

    @property
    def lastPowerChange(self):
        return self.vehicle.lastCurrentChangeTime
