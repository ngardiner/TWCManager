class TeslaAPIEVSE:

    master = None
    vehicle = None

    def __init__(self, vehicle, master):
        self.master = master
        self.vehicle = vehicle

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

    @property
    def minPower(self):
        return self.master.convertAmpsToWatts(5, self.currentVoltage)

    @property
    def maxPower(self):
        return self.master.convertAmpsToWatts(self.currentAmps, self.currentVoltage)

    @property
    def currentPower(self):
        return self.master.convertAmpsToWatts(self.currentAmps, self.currentVoltage)

    @property
    def currentAmps(self):
        return self.vehicle.actualCurrent

    @property
    def currentVoltage(self):
        voltage = self.vehicle.voltage
        phases = self.vehicle.phases if self.vehicle.phases else 1
        
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


